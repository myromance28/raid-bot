import discord
from discord.ext import commands, tasks
from discord.ext.commands import has_permissions
import os
import psycopg2
from psycopg2 import pool
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread

# =========================
# 🔹 설정 및 시간대
# =========================
KST = timezone(timedelta(hours=9))

# 채널 ID (본인의 채널 ID로 수정하세요)
BOSS_CHANNEL_ID = 1503420212794622073
LOG_CHANNEL_ID = 1495580902787514508

# 환경변수 로드
DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# =========================
# 🔹 PostgreSQL 연결 풀
# =========================
try:
    # URL 형식이 postgresql:// 인 경우 postgres:// 로 변환 (호환성)
    if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgres://", 1)
    
    db_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
except Exception as e:
    print(f"❌ DB 연결 실패: {e}")
    exit()

def get_db_connection():
    return db_pool.getconn()

def release_db_connection(conn):
    db_pool.putconn(conn)

# =========================
# 🔹 DB 초기화 (테이블 생성)
# =========================
def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # 출석 기록
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                date TEXT,
                time_slot TEXT,
                name TEXT
            )
            """)
            # 멤버 명단 및 점수
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS members (
                name TEXT PRIMARY KEY,
                total INTEGER DEFAULT 0
            )
            """)
            # 득템 기록
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS drops (
                id SERIAL PRIMARY KEY,
                item_name TEXT,
                winner TEXT,
                date TEXT,
                boss_name TEXT
            )
            """)
            # 보스 목록
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS boss_list (
                boss_name TEXT PRIMARY KEY
            )
            """)
            conn.commit()
    finally:
        release_db_connection(conn)

init_db()

# =========================
# 🔹 Flask (24시간 유지용)
# =========================
app = Flask(__name__)
@app.route("/")
def home(): return "Bot is Alive"

def run():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

def keep_alive():
    Thread(target=run).start()

# =========================
# 🔹 헬퍼 함수
# =========================
def get_slot():
    hour = datetime.now(KST).hour
    if 0 <= hour < 6: return "03"
    elif 6 <= hour < 12: return "09"
    elif 12 <= hour < 18: return "15"
    else: return "21"

def is_attended(name, date, slot):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM attendance WHERE date=%s AND time_slot=%s AND name=%s", (date, slot, name))
            return cursor.fetchone() is not None
    finally: release_db_connection(conn)

# =========================
# 🔹 UI 컴포넌트 (버튼, 모달)
# =========================
class DropModal(discord.ui.Modal, title="💎 득템 기록"):
    item_input = discord.ui.TextInput(label="아이템 이름")
    winner_input = discord.ui.TextInput(label="획득자 이름")
    def __init__(self, boss_name, view):
        super().__init__()
        self.boss_name, self.view = boss_name, view
    async def on_submit(self, interaction: discord.Interaction):
        conn = get_db_connection()
        try:
            item, winner = self.item_input.value, self.winner_input.value
            date_str = datetime.now(KST).strftime("%m-%d %H:%M")
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO drops (item_name, winner, date, boss_name) VALUES (%s, %s, %s, %s)", 
                               (item, winner, date_str, self.boss_name))
                conn.commit()
            self.view.boss_status[self.boss_name] = f"✅ 컷 ({winner} - {item})"
            await interaction.response.send_message(f"✅ [{self.boss_name}] {winner}님 {item} 획득!", ephemeral=False)
        finally: release_db_connection(conn)

class DropChoiceView(discord.ui.View):
    def __init__(self, boss_name, parent_view):
        super().__init__(timeout=60)
        self.boss_name, self.parent_view = boss_name, parent_view
    @discord.ui.button(label="노득", style=discord.ButtonStyle.secondary)
    async def nodrop(self, i, b):
        self.parent_view.boss_status[self.boss_name] = "✅ 컷 (노득)"
        await i.response.send_message(f"✅ [{self.boss_name}] 컷 - 노득", ephemeral=False)
    @discord.ui.button(label="득템", style=discord.ButtonStyle.green)
    async def drop(self, i, b):
        await i.response.send_modal(DropModal(self.boss_name, self.parent_view))

class BossActionSelect(discord.ui.Select):
    def __init__(self, bosses, parent_view):
        self.parent_view = parent_view
        options = [discord.SelectOption(label=f"{b} 컷", emoji="⚔️", value=b) for b in bosses[:25]]
        super().__init__(placeholder="보스 컷 처리...", options=options, row=4)
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"[{self.values[0]}] 결과 선택", view=DropChoiceView(self.values[0], self.parent_view), ephemeral=True)

class ToggleAttendButton(discord.ui.Button):
    def __init__(self, name, target_date, target_slot):
        self.member_name, self.target_date, self.target_slot = name, target_date, target_slot
        done = is_attended(name, target_date, target_slot)
        super().__init__(label=name, style=discord.ButtonStyle.green if done else discord.ButtonStyle.secondary)
    async def callback(self, interaction: discord.Interaction):
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 FROM attendance WHERE date=%s AND time_slot=%s AND name=%s", (self.target_date, self.target_slot, self.member_name))
                if cursor.fetchone():
                    cursor.execute("DELETE FROM attendance WHERE date=%s AND time_slot=%s AND name=%s", (self.target_date, self.target_slot, self.member_name))
                    cursor.execute("UPDATE members SET total = GREATEST(0, total - 1) WHERE name=%s", (self.member_name,))
                    self.style = discord.ButtonStyle.secondary
                else:
                    cursor.execute("INSERT INTO attendance VALUES (%s, %s, %s)", (self.target_date, self.target_slot, self.member_name))
                    cursor.execute("UPDATE members SET total = total + 1 WHERE name=%s", (self.member_name,))
                    conn.commit()
                    self.style = discord.ButtonStyle.green
                conn.commit()
            await interaction.response.edit_message(view=self.view)
        finally: release_db_connection(conn)

class ToggleAttendanceView(discord.ui.View):
    def __init__(self, members, target_date, target_slot, bosses):
        super().__init__(timeout=None)
        self.members, self.target_date, self.target_slot, self.bosses = members, target_date, target_slot, bosses
        self.boss_status = {b: "미확인" for b in bosses}
        for name in members[:20]: # 최대 20명까지 표시 (Discord 제한)
            self.add_item(ToggleAttendButton(name, target_date, target_slot))
        if bosses: self.add_item(BossActionSelect(bosses, self))
        
        btn_send = discord.ui.Button(label="📊 정산 결과 전송", style=discord.ButtonStyle.danger, row=3)
        btn_send.callback = self.send_result
        self.add_item(btn_send)

    async def send_result(self, i):
        log_ch = i.client.get_channel(LOG_CHANNEL_ID)
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT name FROM attendance WHERE date=%s AND time_slot=%s", (self.target_date, self.target_slot))
                attended = [r[0] for r in cursor.fetchall()]
            embed = discord.Embed(title=f"📊 {self.target_date} [{self.target_slot}:00] 정산 결과", color=0x3498db)
            embed.add_field(name=f"👥 출석 ({len(attended)}명)", value=", ".join(attended) if attended else "없음", inline=False)
            boss_text = "\n".join([f"**{b}**: {s}" for b, s in self.boss_status.items()])
            embed.add_field(name="⚔️ 보스 현황", value=boss_text if boss_text else "없음", inline=False)
            await log_ch.send(embed=embed)
            await i.response.send_message("🚀 정산이 로그 채널로 전송되었습니다.", ephemeral=True)
        finally: release_db_connection(conn)

# =========================
# 🔹 봇 명령어
# =========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    if not auto_boss_panel.is_running(): auto_boss_panel.start()
    print(f"✅ 로그인 완료: {bot.user}")

@bot.command()
async def 출석(ctx):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT name FROM members ORDER BY name ASC")
            m_list = [r[0] for r in cursor.fetchall()]
            cursor.execute("SELECT boss_name FROM boss_list ORDER BY boss_name ASC")
            b_list = [r[0] for r in cursor.fetchall()]
        if not m_list: return await ctx.send("❌ 등록된 인원이 없습니다. !추가 명령어로 인원을 등록하세요.")
        now = datetime.now(KST)
        await ctx.send(f"⚔️ {now.strftime('%Y-%m-%d')} [{get_slot()}:00] 보스타임 패널", 
                       view=ToggleAttendanceView(m_list, now.strftime('%Y-%m-%d'), get_slot(), b_list))
    finally: release_db_connection(conn)

@bot.command()
@has_permissions(administrator=True)
async def 추가(ctx, *, names: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            for name in names.replace(" ", "").split(","):
                cursor.execute("INSERT INTO members(name, total) VALUES(%s, 0) ON CONFLICT (name) DO NOTHING", (name,))
            conn.commit()
        await ctx.send(f"✅ {names} 등록 완료")
    finally: release_db_connection(conn)

@bot.command()
@has_permissions(administrator=True)
async def 보스추가(ctx, name: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO boss_list VALUES (%s) ON CONFLICT DO NOTHING", (name,))
            conn.commit()
        await ctx.send(f"📌 보스 [{name}] 추가 완료")
    finally: release_db_connection(conn)

@bot.command()
async def 조회(ctx, 시작일: str, 종료일: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT name, COUNT(*) FROM attendance WHERE date BETWEEN %s AND %s GROUP BY name ORDER BY COUNT(*) DESC", (시작일, 종료일))
            rows = cursor.fetchall()
        if not rows: return await ctx.send("📅 해당 기간에 기록이 없습니다.")
        text = "\n".join([f"{n}: {c}회" for n, c in rows])
        await ctx.send(f"📊 출석 통계 ({시작일} ~ {종료일})\n\n{text}")
    finally: release_db_connection(conn)

@bot.command()
async def 득템현황(ctx):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT date, boss_name, winner, item_name FROM drops ORDER BY id DESC LIMIT 10")
            rows = cursor.fetchall()
        if not rows: return await ctx.send("💎 아직 득템 기록이 없습니다.")
        text = "\n".join([f"• [{r[0]}] {r[1]} : {r[2]} ({r[3]})" for r in rows])
        await ctx.send("💎 최근 득템 현황 (최대 10개)\n" + text)
    finally: release_db_connection(conn)

@tasks.loop(minutes=1)
async def auto_boss_panel():
    now = datetime.now(KST)
    if now.minute == 50 and now.hour in [2, 8, 14, 20]:
        channel = bot.get_channel(BOSS_CHANNEL_ID)
        if not channel: return
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT name FROM members ORDER BY name ASC")
                m_list = [r[0] for r in cursor.fetchall()]
                cursor.execute("SELECT boss_name FROM boss_list ORDER BY boss_name ASC")
                b_list = [r[0] for r in cursor.fetchall()]
            if m_list:
                t_date = now.strftime("%Y-%m-%d")
                t_slot = f"{(now.hour + 1) % 24:02d}"
                await channel.send(f"⚔️ {t_date} [{t_slot}:00] 정기 보스타임 패널", view=ToggleAttendanceView(m_list, t_date, t_slot, b_list))
        finally: release_db_connection(conn)

keep_alive()
bot.run(DISCORD_TOKEN)