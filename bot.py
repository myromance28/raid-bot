import discord
from discord.ext import commands, tasks
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread
import threading

# =========================
# 🔹 설정 및 초기화
# =========================
KST = timezone(timedelta(hours=9))

BOSS_CHANNEL_ID = 1503420212794622073
LOG_CHANNEL_ID = 1495580902787514508

conn = sqlite3.connect("data.db", check_same_thread=False)
cursor = conn.cursor()
db_lock = threading.Lock()

# =========================
# 🔹 DB 생성
# =========================
with db_lock:
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        date TEXT,
        time_slot TEXT,
        name TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS members (
        name TEXT PRIMARY KEY,
        total INTEGER DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS drops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_name TEXT,
        winner TEXT,
        date TEXT,
        boss_name TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS boss_list (
        boss_name TEXT PRIMARY KEY
    )
    """)

    conn.commit()

# =========================
# 🔹 Flask Keep Alive
# =========================
app = Flask(__name__)

@app.route("/")
def home():
    return "OK"

def run():
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )

def keep_alive():
    Thread(target=run).start()

# =========================
# 🔹 공통 함수
# =========================
def get_slot():
    hour = datetime.now(KST).hour

    if 0 <= hour < 6:
        return "03"
    elif 6 <= hour < 12:
        return "09"
    elif 12 <= hour < 18:
        return "15"
    else:
        return "21"

def is_attended(name, date, slot):
    with db_lock:
        cursor.execute(
            """
            SELECT 1
            FROM attendance
            WHERE date=? AND time_slot=? AND name=?
            """,
            (date, slot, name)
        )

        res = cursor.fetchone()

    return res is not None

# =========================
# 🔹 득템 수정 모달
# =========================
class EditDropModal(discord.ui.Modal, title="✏️ 득템 수정"):

    item_input = discord.ui.TextInput(label="아이템 이름")
    winner_input = discord.ui.TextInput(label="획득자")

    def __init__(self, drop_id, old_item, old_winner):
        super().__init__()

        self.drop_id = drop_id

        self.item_input.default = old_item
        self.winner_input.default = old_winner

    async def on_submit(self, interaction: discord.Interaction):

        with db_lock:
            cursor.execute(
                """
                UPDATE drops
                SET item_name=?, winner=?
                WHERE id=?
                """,
                (
                    self.item_input.value,
                    self.winner_input.value,
                    self.drop_id
                )
            )

            conn.commit()

        await interaction.response.send_message(
            "✏️ 득템 수정 완료!",
            ephemeral=True
        )

# =========================
# 🔹 득템 선택 메뉴
# =========================
class DropManageSelect(discord.ui.Select):

    def __init__(self, rows):

        self.rows = rows

        options = []

        for r in rows[:25]:

            options.append(
                discord.SelectOption(
                    label=f"{r[1]} - {r[2]}",
                    description=f"{r[3]} / {r[4]}",
                    value=str(r[0])
                )
            )

        super().__init__(
            placeholder="수정/삭제할 득템 선택",
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        drop_id = int(self.values[0])

        row = next(r for r in self.rows if r[0] == drop_id)

        view = discord.ui.View(timeout=60)

        # 수정 버튼
        edit_btn = discord.ui.Button(
            label="✏️ 수정",
            style=discord.ButtonStyle.primary
        )

        # 삭제 버튼
        delete_btn = discord.ui.Button(
            label="🗑️ 삭제",
            style=discord.ButtonStyle.danger
        )

        async def edit_cb(i):

            await i.response.send_modal(
                EditDropModal(
                    drop_id,
                    row[1],
                    row[2]
                )
            )

        async def delete_cb(i):

            with db_lock:
                cursor.execute(
                    "DELETE FROM drops WHERE id=?",
                    (drop_id,)
                )

                conn.commit()

            await i.response.send_message(
                "🗑️ 삭제 완료",
                ephemeral=True
            )

        edit_btn.callback = edit_cb
        delete_btn.callback = delete_cb

        view.add_item(edit_btn)
        view.add_item(delete_btn)

        await interaction.response.send_message(
            f"선택됨:\n보스: {row[3]}\n획득자: {row[2]}\n아이템: {row[1]}",
            view=view,
            ephemeral=True
        )

# =========================
# 🔹 득템 관리 View
# =========================
class DropManageView(discord.ui.View):

    def __init__(self, rows):
        super().__init__(timeout=120)

        self.add_item(DropManageSelect(rows))

# =========================
# 🔹 보스 득템 입력
# =========================
class DropModal(discord.ui.Modal, title="💎 보스 득템 기록"):

    item_input = discord.ui.TextInput(
        label="아이템 이름",
        placeholder="예: 영웅 비기"
    )

    winner_input = discord.ui.TextInput(
        label="획득자 이름",
        placeholder="예: 홍길동"
    )

    def __init__(self, boss_name, view):

        super().__init__()

        self.boss_name = boss_name
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):

        item = self.item_input.value
        winner = self.winner_input.value

        date = datetime.now(KST).strftime("%m-%d %H:%M")

        with db_lock:
            cursor.execute(
                """
                INSERT INTO drops
                (item_name, winner, date, boss_name)
                VALUES (?, ?, ?, ?)
                """,
                (
                    item,
                    winner,
                    date,
                    self.boss_name
                )
            )

            conn.commit()

        self.view.boss_status[self.boss_name] = (
            f"✅ 컷 ({winner} - {item})"
        )

        await interaction.response.send_message(
            f"✅ [{self.boss_name}] 컷!\n{winner}님 - {item} 획득!",
            ephemeral=False
        )

# =========================
# 🔹 보스 선택
# =========================
class BossActionSelect(discord.ui.Select):

    def __init__(self, bosses, parent_view):

        self.parent_view = parent_view

        options = []

        for b in bosses:

            options.append(
                discord.SelectOption(
                    label=f"{b} 멍",
                    emoji="💤",
                    value=f"mung_{b}"
                )
            )

            options.append(
                discord.SelectOption(
                    label=f"{b} 컷",
                    emoji="⚔️",
                    value=f"cut_{b}"
                )
            )

        super().__init__(
            placeholder="보스 멍/컷 선택...",
            options=options[:25],
            row=4
        )

    async def callback(self, interaction: discord.Interaction):

        action, boss_name = self.values[0].split("_", 1)

        if action == "mung":

            self.parent_view.boss_status[boss_name] = "💤 멍"

            await interaction.response.send_message(
                f"💤 [{boss_name}] 멍입니다.",
                ephemeral=False
            )

        else:

            await interaction.response.send_modal(
                DropModal(
                    boss_name,
                    self.parent_view
                )
            )

# =========================
# 🔹 출석 버튼
# =========================
class ToggleAttendButton(discord.ui.Button):

    def __init__(self, name, target_date, target_slot):

        self.member_name = name
        self.target_date = target_date
        self.target_slot = target_slot

        done = is_attended(
            name,
            target_date,
            target_slot
        )

        super().__init__(
            label=name,
            style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.green
        )

    async def callback(self, interaction: discord.Interaction):

        with db_lock:

            cursor.execute(
                """
                SELECT 1
                FROM attendance
                WHERE date=? AND time_slot=? AND name=?
                """,
                (
                    self.target_date,
                    self.target_slot,
                    self.member_name
                )
            )

            already_done = cursor.fetchone() is not None

            if already_done:

                cursor.execute(
                    """
                    DELETE FROM attendance
                    WHERE date=? AND time_slot=? AND name=?
                    """,
                    (
                        self.target_date,
                        self.target_slot,
                        self.member_name
                    )
                )

                cursor.execute(
                    """
                    UPDATE members
                    SET total =
                    CASE
                        WHEN total > 0
                        THEN total - 1
                        ELSE 0
                    END
                    WHERE name=?
                    """,
                    (self.member_name,)
                )

                self.style = discord.ButtonStyle.green

            else:

                cursor.execute(
                    """
                    INSERT INTO attendance
                    VALUES (?, ?, ?)
                    """,
                    (
                        self.target_date,
                        self.target_slot,
                        self.member_name
                    )
                )

                cursor.execute(
                    """
                    INSERT INTO members(name, total)
                    VALUES(?, 1)
                    ON CONFLICT(name)
                    DO UPDATE SET total = total + 1
                    """,
                    (self.member_name,)
                )

                self.style = discord.ButtonStyle.secondary

            conn.commit()

        await interaction.response.edit_message(view=self.view)

# =========================
# 🔹 출석 View
# =========================
class ToggleAttendanceView(discord.ui.View):

    def __init__(
        self,
        members,
        target_date,
        target_slot,
        bosses,
        per_page=15
    ):

        super().__init__(timeout=None)

        self.members = members
        self.target_date = target_date
        self.target_slot = target_slot
        self.bosses = bosses

        self.current_page = 0

        self.boss_status = {
            b: "미확인"
            for b in bosses
        }

        self.total_pages = max(
            1,
            (len(members) + per_page - 1) // per_page
        )

        self.build_page()

    def build_page(self):

        self.clear_items()

        start = self.current_page * 15

        for name in self.members[start:start + 15]:

            self.add_item(
                ToggleAttendButton(
                    name,
                    self.target_date,
                    self.target_slot
                )
            )

        if self.total_pages > 1:

            prev_btn = discord.ui.Button(
                label="◀",
                style=discord.ButtonStyle.gray,
                row=3
            )

            async def prev_cb(i):

                self.current_page = (
                    self.current_page - 1
                ) % self.total_pages

                self.build_page()

                await i.response.edit_message(view=self)

            prev_btn.callback = prev_cb

            self.add_item(prev_btn)

            self.add_item(
                discord.ui.Button(
                    label=f"{self.current_page + 1}/{self.total_pages}",
                    style=discord.ButtonStyle.blurple,
                    disabled=True,
                    row=3
                )
            )

            next_btn = discord.ui.Button(
                label="▶",
                style=discord.ButtonStyle.gray,
                row=3
            )

            async def next_cb(i):

                self.current_page = (
                    self.current_page + 1
                ) % self.total_pages

                self.build_page()

                await i.response.edit_message(view=self)

            next_btn.callback = next_cb

            self.add_item(next_btn)

        if self.bosses:
            self.add_item(
                BossActionSelect(
                    self.bosses,
                    self
                )
            )

        # 정산 버튼
        send_btn = discord.ui.Button(
            label="📊 결과 전송 (정산)",
            style=discord.ButtonStyle.danger,
            row=3
        )

        async def send_cb(i):

            await i.response.defer(ephemeral=True)

            log_ch = i.client.get_channel(LOG_CHANNEL_ID)

            if not log_ch:
                return await i.followup.send(
                    "❌ 채널 없음",
                    ephemeral=True
                )

            with db_lock:

                cursor.execute(
                    """
                    SELECT name
                    FROM attendance
                    WHERE date=? AND time_slot=?
                    """,
                    (
                        self.target_date,
                        self.target_slot
                    )
                )

                attended = [
                    r[0]
                    for r in cursor.fetchall()
                ]

            embed = discord.Embed(
                title=f"📊 {self.target_date} [{self.target_slot}:00] 정산",
                color=0x3498db
            )

            embed.add_field(
                name=f"👥 출석 ({len(attended)}명)",
                value="\n".join(
                    [f"• {n} (1점)" for n in attended]
                ) if attended else "없음",
                inline=False
            )

            embed.add_field(
                name="⚔️ 보스 현황",
                value="\n".join(
                    [
                        f"**{b}**: {s}"
                        for b, s in self.boss_status.items()
                    ]
                ) if self.bosses else "기록 없음",
                inline=False
            )

            await log_ch.send(embed=embed)

            await i.followup.send(
                "🚀 전송 완료!",
                ephemeral=True
            )

        send_btn.callback = send_cb

        self.add_item(send_btn)

# =========================
# 🔹 Bot 설정
# =========================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# =========================
# 🔹 명령어
# =========================
@bot.command()
async def 출석(ctx):

    with db_lock:

        cursor.execute(
            """
            SELECT name
            FROM members
            ORDER BY name ASC
            """
        )

        m_list = [
            r[0]
            for r in cursor.fetchall()
        ]

        cursor.execute(
            """
            SELECT boss_name
            FROM boss_list
            ORDER BY boss_name ASC
            """
        )

        b_list = [
            r[0]
            for r in cursor.fetchall()
        ]

    if not m_list:
        return await ctx.send(
            "❌ 등록된 인원이 없습니다."
        )

    now = datetime.now(KST)

    t_date = now.strftime("%Y-%m-%d")
    t_slot = get_slot()

    await ctx.send(
        f"⚔️ {t_date} [{t_slot}:00] 보스타임 패널",
        view=ToggleAttendanceView(
            m_list,
            t_date,
            t_slot,
            b_list
        )
    )

@bot.command()
async def 추가(ctx, *, names: str):

    for name in names.replace(" ", "").split(","):

        with db_lock:

            cursor.execute(
                """
                INSERT OR IGNORE
                INTO members(name, total)
                VALUES(?, 0)
                """,
                (name,)
            )

            conn.commit()

    await ctx.send(f"✅ {names} 추가 완료")

@bot.command()
async def 삭제(ctx, name: str):

    with db_lock:

        cursor.execute(
            "DELETE FROM members WHERE name=?",
            (name,)
        )

        conn.commit()

    await ctx.send(f"✅ {name} 삭제 완료")

@bot.command()
async def 보스추가(ctx, name: str):

    with db_lock:

        cursor.execute(
            """
            INSERT OR IGNORE
            INTO boss_list
            VALUES (?)
            """,
            (name,)
        )

        conn.commit()

    await ctx.send(f"📌 보스 [{name}] 추가")

@bot.command()
async def 보스삭제(ctx, name: str):

    with db_lock:

        cursor.execute(
            """
            DELETE FROM boss_list
            WHERE boss_name=?
            """,
            (name,)
        )

        conn.commit()

    await ctx.send(f"🗑️ 보스 [{name}] 삭제")

@bot.command()
async def 명단(ctx):

    with db_lock:

        cursor.execute(
            """
            SELECT name
            FROM members
            ORDER BY name ASC
            """
        )

        members = [
            r[0]
            for r in cursor.fetchall()
        ]

    await ctx.send(
        "📋 명단\n" + "\n".join(members)
        if members else "없음"
    )

@bot.command()
async def 득템현황(ctx):

    with db_lock:

        cursor.execute("""
            SELECT id, item_name, winner, boss_name, date
            FROM drops
            ORDER BY id DESC
            LIMIT 15
        """)

        rows = cursor.fetchall()

    if not rows:
        return await ctx.send("기록 없음")

    text = "\n".join([
        f"• [{r[4]}] {r[3]} : {r[2]} ({r[1]})"
        for r in rows
    ])

    await ctx.send(
        "💎 최근 득템 현황\n" + text,
        view=DropManageView(rows)
    )

@bot.command()
async def 득템초기화(ctx):

    with db_lock:

        cursor.execute("DELETE FROM drops")

        conn.commit()

    await ctx.send("💎 전체 득템 기록 초기화 완료.")

@bot.command()
async def 최근득템삭제(ctx, 개수: int):

    with db_lock:

        cursor.execute("""
            DELETE FROM drops
            WHERE id IN (
                SELECT id
                FROM drops
                ORDER BY id DESC
                LIMIT ?
            )
        """, (개수,))

        conn.commit()

    await ctx.send(
        f"🗑️ 최근 득템 {개수}개 삭제 완료."
    )

# =========================
# 🔹 자동 보스 패널
# =========================
@tasks.loop(minutes=1)
async def auto_boss_panel():

    now = datetime.now(KST)

    if now.minute == 50 and now.hour in [2, 8, 14, 20]:

        channel = bot.get_channel(
            BOSS_CHANNEL_ID
        )

        if not channel:
            return

        with db_lock:

            cursor.execute("""
                SELECT name
                FROM members
                ORDER BY name ASC
            """)

            m_list = [
                r[0]
                for r in cursor.fetchall()
            ]

            cursor.execute("""
                SELECT boss_name
                FROM boss_list
                ORDER BY boss_name ASC
            """)

            b_list = [
                r[0]
                for r in cursor.fetchall()
            ]

        if not m_list:
            return

        t_date = now.strftime("%Y-%m-%d")
        t_slot = f"{(now.hour + 1) % 24:02d}"

        await channel.send(
            f"⚔️ {t_date} [{t_slot}:00] 보스타임 패널",
            view=ToggleAttendanceView(
                m_list,
                t_date,
                t_slot,
                b_list
            )
        )

# =========================
# 🔹 이벤트
# =========================
@bot.event
async def on_ready():

    if not auto_boss_panel.is_running():
        auto_boss_panel.start()

    print(f"로그인 완료: {bot.user}")

# =========================
# 🔹 실행
# =========================
keep_alive()

bot.run(os.getenv("DISCORD_TOKEN"))