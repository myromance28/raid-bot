import discord
from discord.ext import commands
import os
import sqlite3
from datetime import datetime
from flask import Flask
from threading import Thread
import asyncio
import threading

# =========================
# 🔹 Flask (Render 유지용)
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

# 🔥 DB 락 (추가)
db_lock = threading.Lock()

# =========================
# 🔹 시간 슬롯 (3/9/15/21)
# =========================
def get_slot():
    hour = datetime.now().hour

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
    date = datetime.now().strftime("%Y-%m-%d")
    slot = get_slot()

    with db_lock:
        cursor.execute(
            "SELECT * FROM attendance WHERE date=? AND time_slot=? AND name=?",
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

# =========================
# 🔹 취소
# =========================
def cancel(name):
    date = datetime.now().strftime("%Y-%m-%d")
    slot = get_slot()

    with db_lock:
        cursor.execute(
            "DELETE FROM attendance WHERE date=? AND time_slot=? AND name=?",
            (date, slot, name)
        )
        conn.commit()

    return "ok"

# =========================
# 🔹 Discord
# =========================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# 🔹 버튼 UI
# =========================
class AttendanceView(discord.ui.View):

    @discord.ui.button(label="출석", style=discord.ButtonStyle.green)
    async def attend_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        name = interaction.user.display_name
        result = attend(name)

        if result == "already":
            await interaction.response.send_message("이미 출석 완료", ephemeral=True)
        else:
            await interaction.response.send_message("출석 완료", ephemeral=True)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.red)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancel(interaction.user.display_name)
        await interaction.response.send_message("취소 완료", ephemeral=True)

# =========================
# 🔹 명령어
# =========================

@bot.command()
async def 출석(ctx):
    members = get_members()

    if not members:
        await ctx.send("등록된 인원 없음")
        return

    text = "📌 출석 명단\n\n" + "\n".join(members)
    await ctx.send(text, view=AttendanceView())

@bot.command()
async def 추가(ctx, name: str):
    add_member(name)
    await ctx.send(f"{name} 추가 완료")

@bot.command()
async def 삭제(ctx, name: str):
    remove_member(name)
    await ctx.send(f"{name} 삭제 완료")

@bot.command()
async def 랭킹(ctx):
    cursor.execute("SELECT name, total FROM members ORDER BY total DESC")
    rows = cursor.fetchall()

    text = "🏆 랭킹\n\n"
    for i, r in enumerate(rows, 1):
        text += f"{i}. {r[0]} - {r[1]}회\n"

    await ctx.send(text)

# =========================
# 🔥 자동 10분 전 패널 (추가)
# =========================
async def auto_panel_10min():
    await bot.wait_until_ready()

    notified = set()

    while not bot.is_closed():
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        schedule = {
            3: "03",
            9: "09",
            15: "15",
            21: "21"
        }

        for hour, slot in schedule.items():

            if now.hour == hour and now.minute == 50:

                key = f"{today}-{slot}"

                if key in notified:
                    continue

                channel = discord.utils.get(bot.get_all_channels(), name="출석")

                if channel:
                    members = get_members()

                    if members:
                        text = f"⏰ {slot}시 출석 10분 전\n\n"
                        text += "\n".join(members)

                        await channel.send(text, view=AttendanceView())

                notified.add(key)

        if now.hour == 0 and now.minute == 0:
            notified.clear()

        await asyncio.sleep(30)

# =========================
# 🧪 테스트 명령어
# =========================
@bot.command()
async def 테스트출석(ctx):
    members = get_members()

    if not members:
        await ctx.send("등록된 인원 없음")
        return

    text = "🧪 테스트 출석 UI\n\n" + "\n".join(members)
    await ctx.send(text, view=AttendanceView())

# =========================
# 🔹 실행
# =========================
keep_alive()

bot.loop.create_task(auto_panel_10min())

bot.run(os.getenv("DISCORD_TOKEN"))