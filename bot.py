import discord
from discord.ext import commands
import os
import sqlite3
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread
import asyncio

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

# =========================
# 🔹 현재 시간 슬롯 계산
# =========================
def get_slot():
    hour = datetime.now().hour

    if hour < 6:
        return "21"
    elif hour < 12:
        return "03"
    elif hour < 18:
        return "09"
    else:
        return "15"

# =========================
# 🔹 출석 / 취소
# =========================
def attend(name):
    date = datetime.now().strftime("%Y-%m-%d")
    slot = get_slot()

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


def cancel(name):
    date = datetime.now().strftime("%Y-%m-%d")
    slot = get_slot()

    cursor.execute(
        "SELECT * FROM attendance WHERE date=? AND time_slot=? AND name=?",
        (date, slot, name)
    )

    if not cursor.fetchone():
        return "none"

    cursor.execute(
        "DELETE FROM attendance WHERE date=? AND time_slot=? AND name=?",
        (date, slot, name)
    )

    conn.commit()
    return "ok"

# =========================
# 🔹 주간 데이터
# =========================
def weekly_data():
    start = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")

    cursor.execute("""
        SELECT name, COUNT(*)
        FROM attendance
        WHERE date >= ?
        GROUP BY name
        ORDER BY COUNT(*) DESC
    """, (start,))

    return cursor.fetchall()

# =========================
# 🔹 Discord
# =========================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# 🔹 버튼 UI (출석 + 취소)
# =========================
class AttendanceView(discord.ui.View):

    @discord.ui.button(label="출석", style=discord.ButtonStyle.green)
    async def attend_btn(self, interaction, button):
        name = interaction.user.display_name

        result = attend(name)

        if result == "already":
            await interaction.response.send_message("이미 이 시간대 출석 완료", ephemeral=True)
        else:
            await interaction.response.send_message("출석 완료", ephemeral=True)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.red)
    async def cancel_btn(self, interaction, button):
        name = interaction.user.display_name

        result = cancel(name)

        if result == "none":
            await interaction.response.send_message("취소할 기록 없음", ephemeral=True)
        else:
            await interaction.response.send_message("출석 취소 완료", ephemeral=True)

# =========================
# 🔹 명령어
# =========================

@bot.command()
async def 출석(ctx):
    await ctx.send("📌 출석 체크", view=AttendanceView())


@bot.command()
async def 주간(ctx):
    data = weekly_data()

    if not data:
        await ctx.send("이번주 데이터 없음")
        return

    text = "📊 주간 출석 현황\n\n"
    for i, (name, count) in enumerate(data, 1):
        text += f"{i}. {name} - {count}회\n"

    await ctx.send(text)


@bot.command()
async def 랭킹(ctx):
    cursor.execute("SELECT name, total FROM members ORDER BY total DESC")

    rows = cursor.fetchall()

    if not rows:
        await ctx.send("데이터 없음")
        return

    text = "🏆 전체 출석 랭킹\n\n"
    for i, r in enumerate(rows, 1):
        text += f"{i}. {r[0]} - {r[1]}회\n"

    await ctx.send(text)

# =========================
# 🔹 주간 자동 리포트
# =========================
async def weekly_task():
    await bot.wait_until_ready()

    while not bot.is_closed():
        now = datetime.now()

        if now.weekday() == 6 and now.hour == 0 and now.minute == 0:
            channel = discord.utils.get(bot.get_all_channels(), name="출석")

            if channel:
                data = weekly_data()

                text = "📢 주간 정산\n\n"
                for i, (name, count) in enumerate(data, 1):
                    text += f"{i}. {name} - {count}회\n"

                await channel.send(text)

        await asyncio.sleep(60)

# =========================
# 🔹 실행
# =========================
keep_alive()
bot.loop.create_task(weekly_task())
bot.run(os.getenv("DISCORD_TOKEN"))