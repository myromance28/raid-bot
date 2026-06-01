# =====================================================
# RAID BOT - FULL FINAL VERSION
# PART 1 / 4
# =====================================================

import discord
from discord.ext import commands, tasks
import os
import psycopg2
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread
import asyncio

# =====================================================
# 🔹 설정
# =====================================================
KST = timezone(timedelta(hours=9))

BOSS_CHANNEL_ID = 1510737312189911280
LOG_CHANNEL_ID = 1495580902787514508

ADMIN_IDS = {
    1330608030844321884,
    1476159593330511954,
    344403970426535937,
    339072410139754496,
    1469924619170349169,
    1295279721050935306,
    354586137286672384
}

# =====================================================
# 🔹 관리자 체크
# =====================================================
def is_admin(ctx):
    return ctx.author.id in ADMIN_IDS

# =====================================================
# 🔹 PostgreSQL
# =====================================================
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise Exception("DATABASE_URL 환경변수 없음")

def get_db_connection():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require"
    )

def release_db_connection(conn):
    try:
        conn.close()
    except:
        pass

# =====================================================
# 🔹 메모리 캐시
# =====================================================
attendance_add_cache = set()
attendance_remove_cache = set()
attendance_state_cache = {}

# 자동 패널 중복 방지
last_auto_panel_key = None

cache_lock = asyncio.Lock()

# =====================================================
# 🔹 DB 생성
# =====================================================
conn = get_db_connection()

try:
    with conn.cursor() as cursor:

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            date TEXT,
            time_slot TEXT,
            name TEXT
        )
        """)

        cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS attendance_unique_idx
        ON attendance(date, time_slot, name)
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS members (
            name TEXT PRIMARY KEY,
            total INTEGER DEFAULT 0
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS drops (
            id SERIAL PRIMARY KEY,
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

finally:
    release_db_connection(conn)

# =====================================================
# 🔹 Flask KeepAlive
# =====================================================
app = Flask(__name__)

@app.route("/")
def home():
    return "OK"

def run():
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        threaded=True
    )

def keep_alive():
    Thread(target=run).start()

# =====================================================
# 🔹 공통 함수
# =====================================================
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

    return (
        date,
        slot,
        name
    ) in attendance_state_cache

# =====================================================
# 🔹 득템 수정 모달
# =====================================================
class EditDropModal(discord.ui.Modal, title="✏️ 득템 수정"):

    item_input = discord.ui.TextInput(label="아이템 이름")
    winner_input = discord.ui.TextInput(label="획득자")

    def __init__(self, drop_id, old_item, old_winner):

        super().__init__()

        self.drop_id = drop_id

        self.item_input.default = old_item
        self.winner_input.default = old_winner

    async def on_submit(self, interaction: discord.Interaction):

        conn = get_db_connection()

        try:
            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    UPDATE drops
                    SET item_name=%s, winner=%s
                    WHERE id=%s
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

        finally:
            release_db_connection(conn)

# =====================================================
# RAID BOT - FULL FINAL VERSION
# PART 2 / 4
# =====================================================

# =====================================================
# 🔹 득템 선택 메뉴
# =====================================================
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

        if interaction.user.id not in ADMIN_IDS:
            return await interaction.response.send_message(
                "❌ 관리자만 사용 가능합니다.",
                ephemeral=True
            )

        drop_id = int(self.values[0])

        row = next(r for r in self.rows if r[0] == drop_id)

        view = discord.ui.View(timeout=60)

        edit_btn = discord.ui.Button(
            label="✏️ 수정",
            style=discord.ButtonStyle.primary
        )

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

            conn = get_db_connection()

            try:
                with conn.cursor() as cursor:

                    cursor.execute(
                        "DELETE FROM drops WHERE id=%s",
                        (drop_id,)
                    )

                    conn.commit()

                await i.response.send_message(
                    "🗑️ 삭제 완료",
                    ephemeral=True
                )

            finally:
                release_db_connection(conn)

        edit_btn.callback = edit_cb
        delete_btn.callback = delete_cb

        view.add_item(edit_btn)
        view.add_item(delete_btn)

        await interaction.response.send_message(
            f"선택됨:\n보스: {row[3]}\n획득자: {row[2]}\n아이템: {row[1]}",
            view=view,
            ephemeral=True
        )

# =====================================================
# 🔹 득템 관리 View
# =====================================================
class DropManageView(discord.ui.View):

    def __init__(self, rows):

        super().__init__(timeout=120)

        self.add_item(DropManageSelect(rows))

# =====================================================
# 🔹 보스 득템 입력
# =====================================================
class DropModal(discord.ui.Modal, title="💎 보스 득템 기록"):

    item_input = discord.ui.TextInput(
        label="아이템 이름"
    )

    winner_input = discord.ui.TextInput(
        label="획득자 이름"
    )

    def __init__(self, boss_name, view, popup_view):

        super().__init__()

        self.boss_name = boss_name
        self.view = view
        self.popup_view = popup_view

    async def on_submit(self, interaction: discord.Interaction):

        item = self.item_input.value
        winner = self.winner_input.value

        date = datetime.now(KST).strftime("%m-%d %H:%M")

        conn = get_db_connection()

        try:
            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    INSERT INTO drops
                    (item_name, winner, date, boss_name)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        item,
                        winner,
                        date,
                        self.boss_name
                    )
                )

                conn.commit()

            self.view.boss_status[
                self.boss_name
            ] = f"✅ 득템 ({winner} - {item})"

            self.popup_view.drop_btn.disabled = True
            self.popup_view.nodrop_btn.disabled = True

            await interaction.response.edit_message(
                content=(
                    f"✅ [{self.boss_name}] 득템 완료!\n"
                    f"{winner}님 - {item}"
                ),
                view=self.popup_view
            )

        finally:
            release_db_connection(conn)

# =====================================================
# 🔹 보스 처리 버튼 View
# =====================================================
class BossActionView(discord.ui.View):

    def __init__(self, boss_name, parent_view):

        super().__init__(timeout=60)

        self.boss_name = boss_name
        self.parent_view = parent_view

        self.drop_btn = discord.ui.Button(
            label="💎 득템",
            style=discord.ButtonStyle.success
        )

        self.nodrop_btn = discord.ui.Button(
            label="❌ 노득",
            style=discord.ButtonStyle.danger
        )

        self.drop_btn.callback = self.drop_cb
        self.nodrop_btn.callback = self.nodrop_cb

        self.add_item(self.drop_btn)
        self.add_item(self.nodrop_btn)

    async def drop_cb(self, interaction: discord.Interaction):

        await interaction.response.send_modal(
            DropModal(
                self.boss_name,
                self.parent_view,
                self
            )
        )

    async def nodrop_cb(self, interaction: discord.Interaction):

        self.parent_view.boss_status[
            self.boss_name
        ] = "❌ 노득"

        self.drop_btn.disabled = True
        self.nodrop_btn.disabled = True

        await interaction.response.edit_message(
            content=f"❌ [{self.boss_name}] 노득 처리 완료.",
            view=self
        )

# =====================================================
# 🔹 보스 선택
# =====================================================
class BossActionSelect(discord.ui.Select):

    def __init__(self, bosses, parent_view):

        self.parent_view = parent_view

        options = []

        for b in bosses:

            options.append(
                discord.SelectOption(
                    label=f"[{b}] 컷",
                    emoji="⚔️",
                    value=b
                )
            )

        super().__init__(
            placeholder="보스 컷 선택...",
            options=options[:25],
            row=4
        )

    async def callback(self, interaction: discord.Interaction):

        boss_name = self.values[0]

        await interaction.response.send_message(
            f"⚔️ [{boss_name}] 결과 선택",
            view=BossActionView(
                boss_name,
                self.parent_view
            ),
            ephemeral=True
        )

# =====================================================
# RAID BOT - FULL FINAL VERSION
# PART 3 / 4
# =====================================================

# =====================================================
# 🔹 출석 버튼
# =====================================================
class ToggleAttendButton(discord.ui.Button):

    def __init__(self, name, target_date, target_slot):

        self.member_name = name
        self.target_date = target_date
        self.target_slot = target_slot

        self.lock = asyncio.Lock()

        done = is_attended(
            name,
            target_date,
            target_slot
        )

        super().__init__(
            label=name,
            style=discord.ButtonStyle.green
            if done
            else discord.ButtonStyle.secondary
        )

    async def callback(self, interaction: discord.Interaction):

        async with self.lock:

            try:

                await interaction.response.defer(
                    thinking=False
                )

                key = (
                    self.target_date,
                    self.target_slot,
                    self.member_name
                )

                async with cache_lock:

                    if key in attendance_state_cache:

                        attendance_state_cache.pop(
                            key,
                            None
                        )

                        attendance_add_cache.discard(key)
                        attendance_remove_cache.add(key)

                        self.style = discord.ButtonStyle.secondary

                    else:

                        attendance_state_cache[key] = True

                        attendance_add_cache.add(key)
                        attendance_remove_cache.discard(key)

                        self.style = discord.ButtonStyle.green

                await interaction.message.edit(
                    view=self.view
                )

            except Exception as e:
                print("출석 버튼 오류:", e)

# =====================================================
# 🔹 출석 View
# =====================================================
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

        # =========================
        # 페이지 버튼
        # =========================
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

                await i.response.edit_message(
                    view=self
                )

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

                await i.response.edit_message(
                    view=self
                )

            next_btn.callback = next_cb

            self.add_item(next_btn)

        # =========================
        # 보스 선택
        # =========================
        if self.bosses:

            self.add_item(
                BossActionSelect(
                    self.bosses,
                    self
                )
            )

        # =========================
        # 결과 전송 버튼
        # =========================
        send_btn = discord.ui.Button(
            label="📊 결과 전송 (정산)",
            style=discord.ButtonStyle.danger,
            row=3
        )

        async def send_cb(i):

            if i.user.id not in ADMIN_IDS:
                return await i.response.send_message(
                    "❌ 관리자만 사용 가능합니다.",
                    ephemeral=True
                )

            await i.response.defer(
                ephemeral=True
            )

            log_ch = i.client.get_channel(
                LOG_CHANNEL_ID
            )

            if not log_ch:
                return await i.followup.send(
                    "❌ 로그 채널 없음",
                    ephemeral=True
                )

            attended = []

            async with cache_lock:

                for key in attendance_state_cache.keys():

                    d, s, n = key

                    if (
                        d == self.target_date
                        and s == self.target_slot
                    ):
                        attended.append(n)

            embed = discord.Embed(
                title=(
                    f"📊 {self.target_date} "
                    f"[{self.target_slot}:00] 정산"
                ),
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
                        f"**{b}** : {s}"
                        for b, s
                        in self.boss_status.items()
                    ]
                ) if self.bosses else "기록 없음",
                inline=False
            )

            await log_ch.send(embed=embed)

            await i.followup.send(
                "🚀 정산 전송 완료!",
                ephemeral=True
            )

        send_btn.callback = send_cb

        self.add_item(send_btn)

# =====================================================
# 🔹 캐시 → DB 저장
# =====================================================
@tasks.loop(seconds=5)
async def flush_attendance_cache():

    global attendance_add_cache
    global attendance_remove_cache

    if (
        not attendance_add_cache
        and not attendance_remove_cache
    ):
        return

    async with cache_lock:

        save_list = list(attendance_add_cache)
        remove_list = list(attendance_remove_cache)

        attendance_add_cache.clear()
        attendance_remove_cache.clear()

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            # =========================
            # 저장
            # =========================
            for d, s, n in save_list:

                cursor.execute("""
                    INSERT INTO attendance
                    (date, time_slot, name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (
                    d,
                    s,
                    n
                ))

                cursor.execute("""
                    INSERT INTO members(name, total)
                    VALUES(%s, 1)
                    ON CONFLICT(name)
                    DO UPDATE
                    SET total = members.total + 1
                """, (n,))

            # =========================
            # 삭제
            # =========================
            for d, s, n in remove_list:

                cursor.execute("""
                    DELETE FROM attendance
                    WHERE date=%s
                    AND time_slot=%s
                    AND name=%s
                """, (
                    d,
                    s,
                    n
                ))

                cursor.execute("""
                    UPDATE members
                    SET total =
                    CASE
                        WHEN total > 0
                        THEN total - 1
                        ELSE 0
                    END
                    WHERE name=%s
                """, (n,))

            conn.commit()

            print(
                f"[CACHE SAVE] 저장:{len(save_list)} "
                f"삭제:{len(remove_list)}"
            )

    except Exception as e:

        conn.rollback()
        print("캐시 저장 오류:", e)

    finally:
        release_db_connection(conn)

# =====================================================
# RAID BOT - FULL FINAL VERSION
# PART 4 / 4
# =====================================================

# =====================================================
# 🔹 Bot 설정
# =====================================================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# =====================================================
# 🔹 출석 명령어
# =====================================================
@bot.command()
@commands.check(is_admin)
async def 출석(ctx):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

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

            t_date = datetime.now(KST).strftime("%Y-%m-%d")
            t_slot = get_slot()

            cursor.execute("""
                SELECT name
                FROM attendance
                WHERE date=%s
                AND time_slot=%s
            """, (
                t_date,
                t_slot
            ))

            attended_rows = cursor.fetchall()

        attendance_state_cache.clear()

        for r in attended_rows:

            attendance_state_cache[
                (
                    t_date,
                    t_slot,
                    r[0]
                )
            ] = True

        if not m_list:
            return await ctx.send(
                "❌ 등록된 인원이 없습니다."
            )

        await ctx.send(
            f"⚔️ {t_date} [{t_slot}:00] 보스타임 패널",
            view=ToggleAttendanceView(
                m_list,
                t_date,
                t_slot,
                b_list
            )
        )

    finally:
        release_db_connection(conn)

# =====================================================
# 🔹 인원 추가
# =====================================================
@bot.command()
@commands.check(is_admin)
async def 추가(ctx, *, names: str):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            for name in names.replace(" ", "").split(","):

                cursor.execute(
                    """
                    INSERT INTO members(name, total)
                    VALUES(%s, 0)
                    ON CONFLICT(name)
                    DO NOTHING
                    """,
                    (name,)
                )

            conn.commit()

        await ctx.send(
            f"✅ {names} 추가 완료"
        )

    finally:
        release_db_connection(conn)

# =====================================================
# 🔹 인원 삭제
# =====================================================
@bot.command()
@commands.check(is_admin)
async def 삭제(ctx, name: str):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute(
                """
                DELETE FROM members
                WHERE name=%s
                """,
                (name,)
            )

            conn.commit()

        await ctx.send(
            f"🗑️ {name} 삭제 완료"
        )

    finally:
        release_db_connection(conn)

# =====================================================
# 🔹 명단
# =====================================================
@bot.command()
async def 명단(ctx):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
                SELECT name
                FROM members
                ORDER BY name ASC
            """)

            rows = cursor.fetchall()

        if not rows:
            return await ctx.send(
                "❌ 명단 없음"
            )

        text = "\n".join(
            [f"• {r[0]}" for r in rows]
        )

        await ctx.send(
            f"📋 명단\n{text}"
        )

    finally:
        release_db_connection(conn)

# =====================================================
# 🔹 보스 추가
# =====================================================
@bot.command()
@commands.check(is_admin)
async def 보스추가(ctx, *, boss_name: str):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
                INSERT INTO boss_list(boss_name)
                VALUES(%s)
                ON CONFLICT DO NOTHING
            """, (boss_name,))

            conn.commit()

        await ctx.send(
            f"⚔️ 보스 추가 완료: {boss_name}"
        )

    finally:
        release_db_connection(conn)

# =====================================================
# 🔹 보스 삭제
# =====================================================
@bot.command()
@commands.check(is_admin)
async def 보스삭제(ctx, *, boss_name: str):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
                DELETE FROM boss_list
                WHERE boss_name=%s
            """, (boss_name,))

            conn.commit()

        await ctx.send(
            f"🗑️ 보스 삭제 완료: {boss_name}"
        )

    finally:
        release_db_connection(conn)

# =====================================================
# 🔹 득템 조회
# =====================================================
@bot.command(name="득템")
async def all_drops(ctx):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
                SELECT id, item_name, winner,
                       boss_name, date
                FROM drops
                ORDER BY id DESC
                LIMIT 50
            """)

            rows = cursor.fetchall()

        if not rows:
            return await ctx.send(
                "💎 득템 기록 없음"
            )

        text = "\n".join([
            f"• [{r[4]}] "
            f"{r[3]} : {r[2]} ({r[1]})"
            for r in rows
        ])

        await ctx.send(
            "💎 전체 득템 현황\n" + text,
            view=DropManageView(rows)
        )

    finally:
        release_db_connection(conn)

# =====================================================
# 🔹 최근 7일 점수
# =====================================================
@bot.command(name="주간")
async def weekly_score(ctx):

    conn = get_db_connection()

    try:
        start_date = (
            datetime.now(KST)
            - timedelta(days=7)
        ).strftime("%Y-%m-%d")

        end_date = datetime.now(KST).strftime(
            "%Y-%m-%d"
        )

        with conn.cursor() as cursor:

            cursor.execute("""
                SELECT name,
                       COUNT(*) as total
                FROM attendance
                WHERE date BETWEEN %s AND %s
                GROUP BY name
                ORDER BY total DESC,
                         name ASC
            """, (
                start_date,
                end_date
            ))

            rows = cursor.fetchall()

        if not rows:
            return await ctx.send(
                "📊 최근 7일 기록 없음"
            )

        text = "\n".join([
            f"• {r[0]} : {r[1]}점"
            for r in rows
        ])

        await ctx.send(
            f"📊 최근 7일 점수\n"
            f"({start_date} ~ {end_date})\n\n"
            f"{text}"
        )

    finally:
        release_db_connection(conn)

# =====================================================
# 🔹 기간 조회
# =====================================================
@bot.command(name="기간조회")
async def range_score(
    ctx,
    start_date: str,
    end_date: str
):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
                SELECT name,
                       COUNT(*) as total
                FROM attendance
                WHERE date BETWEEN %s AND %s
                GROUP BY name
                ORDER BY total DESC,
                         name ASC
            """, (
                start_date,
                end_date
            ))

            rows = cursor.fetchall()

        if not rows:
            return await ctx.send(
                "📊 해당 기간 기록 없음"
            )

        text = "\n".join([
            f"• {r[0]} : {r[1]}점"
            for r in rows
        ])

        await ctx.send(
            f"📊 기간 점수 조회\n"
            f"({start_date} ~ {end_date})\n\n"
            f"{text}"
        )

    finally:
        release_db_connection(conn)

# =====================================================
# 🔹 득템 초기화
# =====================================================
@bot.command()
@commands.check(is_admin)
async def 득템초기화(ctx):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute(
                "DELETE FROM drops"
            )

            conn.commit()

        await ctx.send(
            "💎 전체 득템 기록 초기화 완료"
        )

    finally:
        release_db_connection(conn)

# =====================================================
# 🔹 최근 득템 삭제
# =====================================================
@bot.command()
@commands.check(is_admin)
async def 최근득템삭제(ctx, 개수: int):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
                DELETE FROM drops
                WHERE id IN (
                    SELECT id
                    FROM drops
                    ORDER BY id DESC
                    LIMIT %s
                )
            """, (개수,))

            conn.commit()

        await ctx.send(
            f"🗑️ 최근 득템 {개수}개 삭제 완료"
        )

    finally:
        release_db_connection(conn)

# =====================================================
# 🔹 점수 수정
# =====================================================
@bot.command()
@commands.check(is_admin)
async def 점수수정(
    ctx,
    name: str,
    score: int
):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
                UPDATE members
                SET total=%s
                WHERE name=%s
            """, (
                score,
                name
            ))

            conn.commit()

        await ctx.send(
            f"✅ {name} 점수 수정 완료"
        )

    finally:
        release_db_connection(conn)

# =====================================================
# 🔹 자동 보스 패널
# =====================================================
@tasks.loop(minutes=1)
async def auto_boss_panel():

    global last_auto_panel_key

    now = datetime.now(KST)

    if True:

        t_date = now.strftime("%Y-%m-%d")
        t_slot = f"{(now.hour + 1) % 24:02d}"

        panel_key = f"{t_date}_{t_slot}"

        # 이미 생성한 패널이면 종료
        if last_auto_panel_key == panel_key:
            return

        channel = bot.get_channel(
            BOSS_CHANNEL_ID
        )

        if not channel:
            print("자동패널 실패: 채널 없음")
            return

        conn = get_db_connection()

        try:
            with conn.cursor() as cursor:

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
                print("자동패널 실패: 인원 없음")
                return

            attendance_state_cache.clear()

            await channel.send(
                f"⚔️ {t_date} [{t_slot}:00] 보스타임 패널",
                view=ToggleAttendanceView(
                    m_list,
                    t_date,
                    t_slot,
                    b_list
                )
            )

            # 전송 성공 후 기록
            last_auto_panel_key = panel_key

            print(
                f"[AUTO PANEL SUCCESS] "
                f"{t_date} {t_slot}:00"
            )

        except Exception as e:

            print(
                f"[AUTO PANEL ERROR] {e}"
            )

        finally:
            release_db_connection(conn)

# =====================================================
# 🔹 출석 채널 자동 정리
# =====================================================
@tasks.loop(minutes=1)
async def clear_old_panels():

    now = datetime.now(KST)

    if (
        (now.hour == 6 and now.minute == 0)
        or
        (now.hour == 12 and now.minute == 0)
        or
        (now.hour == 18 and now.minute == 0)
        or
        (now.hour == 0 and now.minute == 0)
    ):

        channel = bot.get_channel(
            BOSS_CHANNEL_ID
        )

        if not channel:
            return

        try:

            deleted = await channel.purge(limit=200)

            print(
                f"[채널정리] {len(deleted)}개 삭제"
            )

        except Exception as e:

            print(
                f"[채널정리 오류] {e}"
            )

# =====================================================
# 🔹 이벤트
# =====================================================
@bot.event
async def on_ready():

    if not auto_boss_panel.is_running():
        auto_boss_panel.start()

    if not flush_attendance_cache.is_running():
        flush_attendance_cache.start()

    if not clear_old_panels.is_running():
        clear_old_panels.start()

    print(f"로그인 완료: {bot.user}")

# =====================================================
# 🔹 실행
# =====================================================
keep_alive()

bot.run(
    os.getenv("DISCORD_TOKEN")
)