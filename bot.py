import discord
from discord.ext import commands, tasks
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread
import threading

print("Token loaded:", os.getenv("DISCORD_TOKEN") is not None)

# =========================
# 🔹 KST
# =========================
KST = timezone(timedelta(hours=9))

# =========================
# 🔹 Flask (Render 유지)
# =========================
app = Flask(__name__)

@app.route("/")
def home():
    return "OK"

def run():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    Thread(target=run).start()

# =========================
# 🔹 보스 출석 채널
# =========================
BOSS_CHANNEL_ID = 1503420212794622073

# =========================
# 🔹 DB
# =========================
conn = sqlite3.connect("data.db", check_same_thread=False)
cursor = conn.cursor()

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

conn.commit()
db_lock = threading.Lock()

# =========================
# 🔹 슬롯
# =========================
def get_slot():
    hour = datetime.now(KST).hour

    if 0 <= hour < 6:
        return "21"
    elif 6 <= hour < 12:
        return "03"
    elif 12 <= hour < 18:
        return "09"
    else:
        return "15"

# =========================
# 🔹 이름 관리
# =========================
def add_member(name):
    with db_lock:
        cursor.execute(
            "INSERT OR IGNORE INTO members(name, total) VALUES(?, 0)",
            (name,)
        )
        conn.commit()

def remove_member(name):
    with db_lock:
        cursor.execute(
            "DELETE FROM members WHERE name=?",
            (name,)
        )
        conn.commit()

def get_members():
    with db_lock:
        cursor.execute(
            "SELECT name FROM members ORDER BY name ASC"
        )
        return [r[0] for r in cursor.fetchall()]

# =========================
# 🔹 출석
# =========================
def attend(name):

    date = datetime.now(KST).strftime("%Y-%m-%d")
    slot = get_slot()

    with db_lock:

        cursor.execute(
            "SELECT 1 FROM attendance WHERE date=? AND time_slot=? AND name=?",
            (date, slot, name)
        )

        if cursor.fetchone():
            return "already"

        cursor.execute(
            "INSERT INTO attendance VALUES (?, ?, ?)",
            (date, slot, name)
        )

        cursor.execute("""
            INSERT INTO members(name, total)
            VALUES(?, 1)
            ON CONFLICT(name)
            DO UPDATE SET total = total + 1
        """, (name,))

        conn.commit()

    return "ok"

def cancel_attend(name):

    date = datetime.now(KST).strftime("%Y-%m-%d")
    slot = get_slot()

    with db_lock:

        cursor.execute(
            "DELETE FROM attendance WHERE date=? AND time_slot=? AND name=?",
            (date, slot, name)
        )

        cursor.execute("""
            UPDATE members
            SET total = CASE
                WHEN total > 0 THEN total - 1
                ELSE 0
            END
            WHERE name=?
        """, (name,))

        conn.commit()

def is_attended(name):

    date = datetime.now(KST).strftime("%Y-%m-%d")
    slot = get_slot()

    with db_lock:

        cursor.execute(
            "SELECT 1 FROM attendance WHERE date=? AND time_slot=? AND name=?",
            (date, slot, name)
        )

        return cursor.fetchone() is not None

# =========================
# 🔹 다음 보스타임 계산
# =========================
BOSS_TIMES = [3, 9, 15, 21]

def get_next_boss_time():

    now = datetime.now(KST)

    today = now.date()

    candidates = []

    for h in BOSS_TIMES:

        candidates.append(
            datetime.combine(
                today,
                datetime.min.time(),
                tzinfo=KST
            ).replace(
                hour=h,
                minute=0,
                second=0
            )
        )

    tomorrow = today + timedelta(days=1)

    candidates.append(
        datetime.combine(
            tomorrow,
            datetime.min.time(),
            tzinfo=KST
        ).replace(
            hour=3,
            minute=0,
            second=0
        )
    )

    for t in candidates:
        if t > now:
            return t

# =========================
# 🔹 봇
# =========================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# =========================
# 🔹 출석 버튼
# =========================
class ToggleAttendButton(discord.ui.Button):

    def __init__(self, name):

        self.member_name = name

        done = is_attended(name)

        super().__init__(
            label=name,
            style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.green,
            row=None
        )

    async def callback(self, interaction: discord.Interaction):

        if is_attended(self.member_name):

            cancel_attend(self.member_name)

            self.style = discord.ButtonStyle.green

        else:

            attend(self.member_name)

            self.style = discord.ButtonStyle.secondary

        await interaction.response.edit_message(
            content=interaction.message.content,
            view=self.view
        )

# =========================
# 🔹 페이지 View
# =========================
class ToggleAttendanceView(discord.ui.View):

    def __init__(self, members, per_page=20):

        super().__init__(timeout=None)

        self.members = members
        self.per_page = per_page

        self.current_page = 0

        self.total_pages = max(
            1,
            (len(self.members) + self.per_page - 1) // self.per_page
        )

        self.build_page()

    def build_page(self):

        self.clear_items()

        start = self.current_page * self.per_page
        end = start + self.per_page

        page_members = self.members[start:end]

        # 사람 버튼
        for name in page_members:
            self.add_item(ToggleAttendButton(name))

        # 페이지 버튼
        if self.total_pages > 1:

            prev_btn = discord.ui.Button(
                label="◀ 이전",
                style=discord.ButtonStyle.gray,
                row=4
            )

            async def prev_callback(interaction: discord.Interaction):

                self.current_page -= 1

                if self.current_page < 0:
                    self.current_page = self.total_pages - 1

                self.build_page()

                await interaction.response.edit_message(
                    content=interaction.message.content,
                    view=self
                )

            prev_btn.callback = prev_callback

            self.add_item(prev_btn)

            page_btn = discord.ui.Button(
                label=f"{self.current_page + 1}/{self.total_pages}",
                style=discord.ButtonStyle.blurple,
                disabled=True,
                row=4
            )

            self.add_item(page_btn)

            next_btn = discord.ui.Button(
                label="다음 ▶",
                style=discord.ButtonStyle.gray,
                row=4
            )

            async def next_callback(interaction: discord.Interaction):

                self.current_page += 1

                if self.current_page >= self.total_pages:
                    self.current_page = 0

                self.build_page()

                await interaction.response.edit_message(
                    content=interaction.message.content,
                    view=self
                )

            next_btn.callback = next_callback

            self.add_item(next_btn)

# =========================
# 🔹 자동 보스 출석패널
# =========================
@tasks.loop(minutes=1)
async def auto_boss_panel():

    now = datetime.now(KST)

    next_boss = get_next_boss_time()

    target = next_boss - timedelta(minutes=10)

    if (
        now.year == target.year and
        now.month == target.month and
        now.day == target.day and
        now.hour == target.hour and
        now.minute == target.minute
    ):

        channel = bot.get_channel(BOSS_CHANNEL_ID)

        if channel is None:
            return

        members = get_members()

        if not members:
            return

        view = ToggleAttendanceView(members)

        title = (
            f"{next_boss.month}월{next_boss.day}일_"
            f"{next_boss.hour}:00 보스타임 출석패널"
        )

        await channel.send(
            title,
            view=view
        )

# =========================
# 🔹 명령어
# =========================
@bot.command()
async def 출석(ctx):

    members = get_members()

    if not members:
        await ctx.send("등록된 인원 없음")
        return

    view = ToggleAttendanceView(members)

    await ctx.send(
        f"📌 출석 패널 (1/{view.total_pages})",
        view=view
    )

@bot.command()
async def 추가(ctx, *, names: str):

    for name in names.replace(" ", "").split(","):
        add_member(name)

    await ctx.send(f"{names} 추가 완료")

@bot.command()
async def 삭제(ctx, name: str):

    remove_member(name)

    await ctx.send(f"{name} 삭제 완료")

@bot.command()
async def 명단(ctx):

    members = get_members()

    if not members:
        await ctx.send("등록된 인원 없음")
        return

    await ctx.send(
        "📋 등록된 명단\n" + "\n".join(members)
    )

# =========================
# 🔹 주간 집계
# =========================
@bot.command()
async def 주간(ctx):

    now = datetime.now(KST)

    # 이번주 월요일 00:00
    monday = now - timedelta(days=now.weekday())

    week_start = monday.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    # 이번주 일요일 23:59:59
    week_end = week_start + timedelta(
        days=6,
        hours=23,
        minutes=59,
        seconds=59
    )

    cursor.execute("""
        SELECT name, COUNT(*)
        FROM attendance
        WHERE date BETWEEN ? AND ?
        GROUP BY name
        ORDER BY COUNT(*) DESC
    """, (
        week_start.strftime("%Y-%m-%d"),
        week_end.strftime("%Y-%m-%d")
    ))

    rows = cursor.fetchall()

    if not rows:
        await ctx.send("데이터 없음")
        return

    text = (
        f"📊 주간 출석 점수\n"
        f"({week_start.strftime('%m/%d')} ~ "
        f"{week_end.strftime('%m/%d')})\n\n"
    )

    for i, r in enumerate(rows, 1):
        text += f"{i}. {r[0]} - {r[1]}점\n"

    await ctx.send(text)

# =========================
# 🔹 준비 완료
# =========================
@bot.event
async def on_ready():

    print(f"로그인 완료 : {bot.user}")

    if not auto_boss_panel.is_running():
        auto_boss_panel.start()

# =========================
# 🔹 실행
# =========================
keep_alive()

bot.run(os.getenv("DISCORD_TOKEN"))