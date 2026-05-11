import discord
from discord.ext import commands
import os
import sqlite3
from datetime import datetime
from flask import Flask
from threading import Thread

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
    t = Thread(target=run)
    t.start()

# =========================
# 🔹 SQLite DB
# =========================
conn = sqlite3.connect("data.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS attendance (
    date TEXT,
    name TEXT
)
""")
conn.commit()

# =========================
# 🔹 출석 / 취소 / 조회
# =========================
def attend(name):
    date = datetime.now().strftime("%Y-%m-%d")

    cursor.execute(
        "SELECT * FROM attendance WHERE date=? AND name=?",
        (date, name)
    )

    if cursor.fetchone():
        return "already"

    cursor.execute(
        "INSERT INTO attendance VALUES (?, ?)",
        (date, name)
    )

    conn.commit()
    return "ok"


def cancel(name):
    date = datetime.now().strftime("%Y-%m-%d")

    cursor.execute(
        "DELETE FROM attendance WHERE date=? AND name=?",
        (date, name)
    )

    conn.commit()
    return "cancel"


def get_today_list():
    date = datetime.now().strftime("%Y-%m-%d")

    cursor.execute(
        "SELECT name FROM attendance WHERE date=?",
        (date,)
    )

    return [row[0] for row in cursor.fetchall()]

# =========================
# 🔹 Discord Bot
# =========================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# 🔹 버튼 UI
# =========================
class RaidView(discord.ui.View):

    @discord.ui.button(label="출석", style=discord.ButtonStyle.green)
    async def attend_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        name = interaction.user.display_name

        result = attend(name)

        if result == "already":
            await interaction.response.send_message("이미 출석 완료됨", ephemeral=True)
        else:
            await interaction.response.send_message("출석 완료 +1", ephemeral=True)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.red)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        name = interaction.user.display_name

        cancel(name)

        await interaction.response.send_message("출석 취소 완료 -1", ephemeral=True)

# =========================
# 🔹 명령어들
# =========================

# 출석 패널 생성
@bot.command()
async def 출석(ctx):
    await ctx.send("📌 출석 버튼", view=RaidView())

# 오늘 출석 리스트 (세로 출력)
@bot.command()
async def 현황(ctx):
    members = get_today_list()

    if not members:
        await ctx.send("출석 인원 없음")
        return

    text = "\n".join(members)
    await ctx.send(f"📋 오늘 출석자\n\n{text}")

# 수동 추가
@bot.command()
async def 추가(ctx, name: str):
    date = datetime.now().strftime("%Y-%m-%d")

    cursor.execute(
        "SELECT * FROM attendance WHERE date=? AND name=?",
        (date, name)
    )

    if cursor.fetchone():
        await ctx.send("이미 있음")
        return

    cursor.execute(
        "INSERT INTO attendance VALUES (?, ?)",
        (date, name)
    )
    conn.commit()

    await ctx.send(f"{name} 추가 완료")

# =========================
# 🔹 실행
# =========================
keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))