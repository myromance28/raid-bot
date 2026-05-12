import discord
from discord.ext import commands
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
        cursor.execute("SELECT 1 FROM attendance WHERE date=? AND time_slot=? AND name=?", (date, slot, name))
        if cursor.fetchone():
            return "already"
        cursor.execute("INSERT INTO attendance VALUES (?, ?, ?)", (date, slot, name))
        cursor.execute("""
            INSERT INTO members(name, total)
            VALUES(?, 1)
            ON CONFLICT(name)
            DO UPDATE SET total = total + 1
        """, (name,))
        conn.commit()
    return "ok"

def is_attended(name):
    date = datetime.now(KST).strftime("%Y-%m-%d")
    slot = get_slot()
    cursor.execute("SELECT 1 FROM attendance WHERE date=? AND time_slot=? AND name=?", (date, slot, name))
    return cursor.fetchone() is not None

# =========================
# 🔹 봇
# =========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# 🔹 페이지형 버튼 (취소 버튼 제거)
# =========================
class ToggleAttendButton(discord.ui.Button):
    def __init__(self, name):
        self.name = name
        done = is_attended(name)
        super().__init__(
            label=name,
            style=discord.ButtonStyle.green if not done else discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction):
        if is_attended(self.name):
            # 이미 출석 → 취소
            date = datetime.now(KST).strftime("%Y-%m-%d")
            slot = get_slot()
            with db_lock:
                cursor.execute("DELETE FROM attendance WHERE date=? AND time_slot=? AND name=?", (date, slot, self.name))
                conn.commit()
            self.style = discord.ButtonStyle.green
        else:
            attend(self.name)
            self.style = discord.ButtonStyle.secondary
        await interaction.response.edit_message(content="📌 출석 패널", view=self.view)

class ToggleAttendanceView(discord.ui.View):
    def __init__(self, members, per_page=20):  # 한 페이지 20명
        super().__init__(timeout=None)
        self.members = members  # 모든 멤버 표시
        self.per_page = per_page
        self.current_page = 0
        self.total_pages = (len(self.members) + per_page - 1) // per_page
        self.build_page(self.current_page)

    def build_page(self, page):
        self.clear_items()
        start = page * self.per_page
        end = min(start + self.per_page, len(self.members))
        page_members = self.members[start:end]

        for name in page_members:
            self.add_item(ToggleAttendButton(name))

        if self.total_pages > 1:
            self.add_item(self.PreviousButton(self))
            self.add_item(self.NextButton(self))

    class PreviousButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="◀ 이전", style=discord.ButtonStyle.gray)
            self.attendance_view = parent_view

        async def callback(self, interaction: discord.Interaction):
            self.attendance_view.current_page = (self.attendance_view.current_page - 1) % self.attendance_view.total_pages
            self.attendance_view.build_page(self.attendance_view.current_page)
            await interaction.response.edit_message(content="📌 출석 패널", view=self.attendance_view)

    class NextButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="다음 ▶", style=discord.ButtonStyle.gray)
            self.attendance_view = parent_view

        async def callback(self, interaction: discord.Interaction):
            self.attendance_view.current_page = (self.attendance_view.current_page + 1) % self.attendance_view.total_pages
            self.attendance_view.build_page(self.attendance_view.current_page)
            await interaction.response.edit_message(content="📌 출석 패널", view=self.attendance_view)

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
    await ctx.send("📌 출석 패널", view=view)

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
    await ctx.send("📋 등록된 명단\n" + "\n".join(members))

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
# 🔹 실행
# =========================
keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))