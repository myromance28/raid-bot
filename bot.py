import discord
from discord.ext import commands
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread
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

def cancel(name):
    date = datetime.now(KST).strftime("%Y-%m-%d")
    slot = get_slot()
    with db_lock:
        cursor.execute("DELETE FROM attendance WHERE date=? AND time_slot=? AND name=?", (date, slot, name))
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
# 🔥 버튼 (row 제거)
# =========================
class AttendButton(discord.ui.Button):
    def __init__(self, name, done=False):
        self.name = name
        super().__init__(
            label=name,
            style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.green,
            disabled=done
        )

    async def callback(self, interaction: discord.Interaction):
        attend(self.name)
        members = get_members()
        await interaction.response.edit_message(
            content="📌 출석 패널 (페이지 {}/{})".format(
                getattr(self.view, "page", 0)+1,
                getattr(self.view, "max_page", 0)+1
            ),
            view=PaginatedAttendanceView(members, getattr(self.view, "page", 0))
        )

class CancelButton(discord.ui.Button):
    def __init__(self, name):
        super().__init__(label="취소", style=discord.ButtonStyle.red)
        self.name = name

    async def callback(self, interaction: discord.Interaction):
        cancel(self.name)
        members = get_members()
        await interaction.response.edit_message(
            content="📌 출석 패널 (페이지 {}/{})".format(
                getattr(self.view, "page", 0)+1,
                getattr(self.view, "max_page", 0)+1
            ),
            view=PaginatedAttendanceView(members, getattr(self.view, "page", 0))
        )

# =========================
# 🔹 페이지형 출석 패널
# =========================
class PaginatedAttendanceView(discord.ui.View):
    def __init__(self, members, page=0):
        super().__init__(timeout=None)
        self.members = members
        self.page = page
        self.per_page = 10  # 페이지당 최대 10명
        self.max_page = (len(members) - 1) // self.per_page
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        start = self.page * self.per_page
        end = start + self.per_page
        page_members = self.members[start:end]
        attended_map = {name: is_attended(name) for name in page_members}

        for name in page_members:
            self.add_item(AttendButton(name, done=attended_map[name]))
            self.add_item(CancelButton(name))

        # 페이지 이동 버튼
        if self.max_page > 0:
            if self.page > 0:
                self.add_item(PageButton("◀ 이전", self, self.page - 1))
            if self.page < self.max_page:
                self.add_item(PageButton("다음 ▶", self, self.page + 1))

class PageButton(discord.ui.Button):
    def __init__(self, label, view, target_page):
        super().__init__(label=label, style=discord.ButtonStyle.blurple)
        self.view_ref = view
        self.target_page = target_page

    async def callback(self, interaction: discord.Interaction):
        self.view_ref.page = self.target_page
        self.view_ref.update_buttons()
        await interaction.response.edit_message(
            content="📌 출석 패널 (페이지 {}/{})".format(
                self.view_ref.page + 1, self.view_ref.max_page + 1
            ),
            view=self.view_ref
        )

# =========================
# 🔥 명령어
# =========================
@bot.command()
async def 출석(ctx):
    members = get_members()
    if not members:
        await ctx.send("등록된 인원 없음")
        return
    view = PaginatedAttendanceView(members)
    await ctx.send(f"📌 출석 패널 (페이지 1/{max((len(members)-1)//10 +1,1)})", view=view)

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
# 🔥 실행
# =========================
keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))