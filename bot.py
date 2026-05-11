import discord
from discord.ext import commands
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread
import asyncio
import threading

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
        cursor.execute("SELECT name FROM members ORDER BY name ASC")
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

def cancel(name):
    date = datetime.now(KST).strftime("%Y-%m-%d")
    slot = get_slot()

    with db_lock:
        cursor.execute(
            "DELETE FROM attendance WHERE date=? AND time_slot=? AND name=?",
            (date, slot, name)
        )
        conn.commit()

    return "ok"

# =========================
# 🔹 상태 체크
# =========================
def is_attended(name):
    date = datetime.now(KST).strftime("%Y-%m-%d")
    slot = get_slot()

    cursor.execute(
        "SELECT 1 FROM attendance WHERE date=? AND time_slot=? AND name=?",
        (date, slot, name)
    )

    return cursor.fetchone() is not None

# =========================
# 🔹 Discord Bot
# =========================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# 🔥 버튼
# =========================
class AttendButton(discord.ui.Button):
    def __init__(self, name, row):
        self.member_name = name

        done = is_attended(name)

        super().__init__(
            label=name,
            style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.green,
            row=row,
            disabled=done
        )

    async def callback(self, interaction: discord.Interaction):
        result = attend(self.member_name)

        if result == "already":
            await interaction.response.send_message("이미 출석됨", ephemeral=True)
            return

        members = get_members()

        await interaction.response.edit_message(
            content="📌 출석 패널",
            view=AttendanceView(members)
        )

class CancelButton(discord.ui.Button):
    def __init__(self, name, row):
        self.member_name = name

        super().__init__(
            label="취소",
            style=discord.ButtonStyle.red,
            row=row
        )

    async def callback(self, interaction: discord.Interaction):
        cancel(self.member_name)

        members = get_members()

        await interaction.response.edit_message(
            content="📌 출석 패널",
            view=AttendanceView(members)
        )

# =========================
# 🔥 View (30명 대응 핵심 수정)
# =========================
class AttendanceView(discord.ui.View):
    def __init__(self, members):
        super().__init__(timeout=None)

        for i, name in enumerate(members):
            row_num = i % 5  # 0~4 반복 (디스코드 row 제한 대응)

            self.add_item(AttendButton(name, row=row_num))
            self.add_item(CancelButton(name, row=row_num))

# =========================
# 🔥 명령어
# =========================

@bot.command()
async def 출석(ctx):
    members = get_members()

    if not members:
        await ctx.send("등록된 인원 없음")
        return

    view = AttendanceView(members)

    await ctx.channel.send(
        content="📌 출석 패널",
        view=view
    )

@bot.command()
async def 추가(ctx, *, names):
    name_list = [n.strip() for n in names.split(",")]

    added = []

    for name in name_list:
        if name:
            add_member(name)
            added.append(name)

    await ctx.send(f"추가 완료: {', '.join(added)}")

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

    text = "📋 등록 명단\n\n"

    for i, name in enumerate(members, 1):
        text += f"{i}. {name}\n"

    await ctx.send(text)

@bot.command()
async def 주간(ctx):
    start = (datetime.now(KST) - timedelta(days=6)).strftime("%Y-%m-%d")

    cursor.execute("""
        SELECT name, COUNT(*)
        FROM attendance
        WHERE date >= ?
        GROUP BY name
        ORDER BY COUNT(*) DESC
    """, (start,))

    rows = cursor.fetchall()

    if not rows:
        await ctx.send("데이터 없음")
        return

    text = "📊 주간 출석 점수\n\n"

    for i, r in enumerate(rows, 1):
        text += f"{i}. {r[0]} - {r[1]}점\n"

    await ctx.send(text)

# =========================
# 🔥 자동 출석 패널
# =========================
async def auto_attendance_panel():
    await bot.wait_until_ready()

    sent_times = set()
    weekly_sent = False

    while not bot.is_closed():
        now = datetime.now(KST)
        current = now.strftime("%H:%M")

        # 🔥 10분 전 패널 (03/09/15/21)
        target_times = ["02:50", "08:50", "14:50", "20:50"]

        if current in target_times and current not in sent_times:
            members = get_members()

            if members:
                for guild in bot.guilds:
                    for channel in guild.text_channels:
                        try:
                            await channel.send(
                                "📌 자동 출석 패널",
                                view=AttendanceView(members)
                            )
                            break
                        except:
                            pass

            sent_times.add(current)

        # 🔥 일요일 21:00 주간 정산
        if now.weekday() == 6 and current == "21:00" and not weekly_sent:

            today = datetime.now(KST)
            monday = today - timedelta(days=today.weekday())
            start = monday.strftime("%Y-%m-%d")

            cursor.execute("""
                SELECT name, COUNT(*)
                FROM attendance
                WHERE date >= ?
                GROUP BY name
                ORDER BY COUNT(*) DESC
            """, (start,))

            rows = cursor.fetchall()

            text = "🏆 주간 최종 출석 점수\n\n"

            for i, r in enumerate(rows, 1):
                text += f"{i}. {r[0]} - {r[1]}점\n"

            for guild in bot.guilds:
                for channel in guild.text_channels:
                    try:
                        await channel.send(text)
                        break
                    except:
                        pass

            weekly_sent = True

        # 🔥 월요일 초기화
        if now.weekday() == 0 and current == "00:00":
            weekly_sent = False
            sent_times.clear()

        await asyncio.sleep(30)

# =========================
# 🔥 실행
# =========================
@bot.event
async def on_ready():
    bot.loop.create_task(auto_attendance_panel())
    print(f"{bot.user} 로그인 완료")

keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))