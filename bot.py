import discord
from discord.ext import commands, tasks
import os
import sqlite3
from datetime import datetime
from flask import Flask
from threading import Thread

# =========================
# 🔹 Render 유지용 Flask
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
# 🔹 DB 설정
# =========================
conn = sqlite3.connect("data.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS attendance (
    date TEXT,
    name TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS members (
    name TEXT
)
""")

conn.commit()

# =========================
# 🔹 길드원 관리
# =========================
def add_member(name):
    cursor.execute("SELECT * FROM members WHERE name=?", (name,))
    if cursor.fetchone():
        return False
    cursor.execute("INSERT INTO members VALUES (?)", (name,))
    conn.commit()
    return True

def remove_member(name):
    cursor.execute("DELETE FROM members WHERE name=?", (name,))
    conn.commit()

def get_members():
    cursor.execute("SELECT name FROM members")
    return [x[0] for x in cursor.fetchall()]

# =========================
# 🔹 출석
# =========================
def attend(name):
    date = datetime.now().strftime("%Y-%m-%d")

    cursor.execute("SELECT * FROM attendance WHERE date=? AND name=?", (date, name))
    if cursor.fetchone():
        return False

    cursor.execute("INSERT INTO attendance VALUES (?, ?)", (date, name))
    conn.commit()
    return True

# =========================
# 🔹 Discord 설정
# =========================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# 🔹 출석 버튼 UI
# =========================
class AttendanceView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="출석", style=discord.ButtonStyle.green)
    async def attend_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        name = interaction.user.display_name

        if attend(name):
            await interaction.response.send_message("출석 완료", ephemeral=True)
        else:
            await interaction.response.send_message("이미 출석됨", ephemeral=True)

# =========================
# 🔹 출석 패널 생성
# =========================
async def send_panel():
    channel = discord.utils.get(bot.get_all_channels(), name="출석")
    if not channel:
        return

    members = get_members()

    if not members:
        await channel.send("길드원이 없습니다")
        return

    text = "\n".join([f"{m} [출석]" for m in members])

    await channel.send(
        f"📌 출석 체크 시간\n\n{text}",
        view=AttendanceView()
    )

# =========================
# 🔹 스케줄 (핵심)
# =========================
@tasks.loop(minutes=1)
async def scheduler():
    now = datetime.now().strftime("%H:%M")

    times = ["02:50", "08:50", "14:50", "20:50"]

    if now in times:
        await send_panel()

# =========================
# 🔹 명령어
# =========================

@bot.command()
async def 추가(ctx, name: str):
    if add_member(name):
        await ctx.send(f"{name} 추가 완료")
    else:
        await ctx.send("이미 존재")

@bot.command()
async def 삭제(ctx, name: str):
    remove_member(name)
    await ctx.send(f"{name} 삭제 완료")

@bot.command()
async def 명단(ctx):
    members = get_members()

    if not members:
        await ctx.send("없음")
        return

    await ctx.send("\n".join(members))

@bot.command()
async def 출석(ctx):
    await send_panel()

# =========================
# 🔹 실행
# =========================
@bot.event
async def on_ready():
    print("Bot is ready")
    scheduler.start()

keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))