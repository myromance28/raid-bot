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
# 🔹 Flask
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
# 🔹 멤버
# =========================
def add_member(name):
    with db_lock:
        cursor.execute("INSERT OR IGNORE INTO members(name, total) VALUES(?, 0)", (name,))
        conn.commit()

def remove_member(name):
    with db_lock:
        cursor.execute("DELETE FROM members WHERE name=?", (name,))
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

def is_attended(name):
    date = datetime.now(KST).strftime("%Y-%m-%d")
    slot = get_slot()

    cursor.execute(
        "SELECT 1 FROM attendance WHERE date=? AND time_slot=? AND name=?",
        (date, slot, name)
    )

    return cursor.fetchone() is not None

# =========================
# 🔹 봇
# =========================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# 🔥 안정형 페이지 View (핵심)
# =========================
class AttendanceView(discord.ui.View):
    def __init__(self, members, page=0):
        super().__init__(timeout=None)
        self.members = members
        self.page = page

        self.max_page = max(0, (len(members) - 1) // 10)

        self.build()

    def build(self):
        self.clear_items()

        start = self.page * 10
        end = start + 10

        page_members = self.members[start:end]

        for name in page_members:
            self.add_item(AttendButton(name))
            self.add_item(CancelButton(name))

        self.add_item(PrevButton())
        self.add_item(NextButton())

# =========================
# 🔥 버튼들
# =========================
class AttendButton(discord.ui.Button):
    def __init__(self, name):
        self.member_name = name

        done = is_attended(name)

        super().__init__(
            label=name,
            style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.green,
            disabled=done
        )

    async def callback(self, interaction: discord.Interaction):
        attend(self.member_name)

        members = get_members()
        view = AttendanceView(members, 0)

        await interaction.response.edit_message(
            content="📌 출석 패널",
            view=view
        )

class CancelButton(discord.ui.Button):
    def __init__(self, name):
        self.member_name = name

        super().__init__(
            label="취소",
            style=discord.ButtonStyle.red
        )

    async def callback(self, interaction: discord.Interaction):
        cancel(self.member_name)

        members = get_members()
        view = AttendanceView(members, 0)

        await interaction.response.edit_message(
            content="📌 출석 패널",
            view=view
        )

# =========================
# 🔥 페이지 버튼 (상태 유지 핵심)
# =========================
class PrevButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="◀", style=discord.ButtonStyle.gray)

    async def callback(self, interaction: discord.Interaction):
        view = interaction.message.view
        members = get_members()

        new_page = max(0, view.page - 1)

        new_view = AttendanceView(members, new_page)

        await interaction.response.edit_message(
            content="📌 출석 패널",
            view=new_view
        )

class NextButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="▶", style=discord.ButtonStyle.gray)

    async def callback(self, interaction: discord.Interaction):
        view = interaction.message.view
        members = get_members()

        max_page = max(0, (len(members) - 1) // 10)

        new_page = min(max_page, view.page + 1)

        new_view = AttendanceView(members, new_page)

        await interaction.response.edit_message(
            content="📌 출석 패널",
            view=new_view
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

    view = AttendanceView(members, 0)

    await ctx.send(
        "📌 출석 패널",
        view=view
    )

@bot.command()
async def 추가(ctx, *, names):
    for n in names.split(","):
        add_member(n.strip())

    await ctx.send("추가 완료")

@bot.command()
async def 삭제(ctx, name: str):
    remove_member(name)
    await ctx.send("삭제 완료")

@bot.command()
async def 명단(ctx):
    members = get_members()
    await ctx.send("\n".join(members) if members else "없음")

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

    text = "📊 주간 점수\n\n"
    for i, r in enumerate(rows, 1):
        text += f"{i}. {r[0]} - {r[1]}점\n"

    await ctx.send(text)

# =========================
# 🔹 실행
# =========================
keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))