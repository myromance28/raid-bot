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

# =========================
# 🔹 봇
# =========================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

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
# 🔥 버튼 (핵심 수정 완료)
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

    async def callback(self, interaction):
        result = attend(self.member_name)

        if result == "already":
            await interaction.response.send_message("이미 출석됨", ephemeral=True)
            return

        # 🔥 출석 후 UI 즉시 갱신
        members = get_members()
        await interaction.response.edit_message(
            content="📌 출석 패널 (업데이트됨)",
            view=AttendanceView(members)
        )

class CancelButton(discord.ui.Button):
    def __init__(self, name, row):
        super().__init__(
            label="취소",
            style=discord.ButtonStyle.red,
            row=row
        )
        self.member_name = name

    async def callback(self, interaction):
        cancel(self.member_name)
        await interaction.response.send_message(f"{self.member_name} 취소 완료", ephemeral=True)

# =========================
# 🔥 View (세로 정렬)
# =========================
class AttendanceView(discord.ui.View):
    def __init__(self, members):
        super().__init__(timeout=None)

        for i, name in enumerate(members):
            self.add_item(AttendButton(name, row=i))
            self.add_item(CancelButton(name, row=i))

# =========================
# 🔥 명령어
# =========================
@bot.command()
async def 출석(ctx):
    members = get_members()

    if not members:
        await ctx.send("등록된 인원 없음")
        return

    await ctx.send("📌 출석 패널", view=AttendanceView(members))

@bot.command()
async def 추가(ctx, name: str):
    add_member(name)
    await ctx.send(f"{name} 추가 완료")

@bot.command()
async def 삭제(ctx, name: str):
    remove_member(name)
    await ctx.send(f"{name} 삭제 완료")

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
# 🔥 실행
# =========================
keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))