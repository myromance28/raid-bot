import discord
from discord.ext import commands, tasks
import os
import psycopg2
from psycopg2 import pool
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread

# =========================
# 🔹 설정 및 관리자 명단
# =========================
KST = timezone(timedelta(hours=9))
BOSS_CHANNEL_ID = 1503420212794622073
LOG_CHANNEL_ID = 1495580902787514508

BOT_ADMIN_IDS = [1295279721050935306, 1469924619170349169, 1330608030844321884, 1476159593330511954, 344403970426535937]

DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# =========================
# 🔹 PostgreSQL 연결 및 초기화
# =========================
try:
    if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgres://", 1)
    db_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
except Exception as e:
    print(f"❌ DB 연결 실패: {e}"); exit()

def get_db_connection(): return db_pool.getconn()
def release_db_connection(conn): db_pool.putconn(conn)

def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS attendance (date TEXT, time_slot TEXT, name TEXT)")
            cursor.execute("CREATE TABLE IF NOT EXISTS members (name TEXT PRIMARY KEY, total INTEGER DEFAULT 0)")
            cursor.execute("CREATE TABLE IF NOT EXISTS drops (id SERIAL PRIMARY KEY, item_name TEXT, winner TEXT, date TEXT, boss_name TEXT)")
            cursor.execute("CREATE TABLE IF NOT EXISTS boss_list (boss_name TEXT PRIMARY KEY)")
            conn.commit()
    finally: release_db_connection(conn)

init_db()

# =========================
# 🔹 권한 체크 및 헬퍼
# =========================
def is_bot_admin():
    async def predicate(ctx):
        if ctx.author.id in BOT_ADMIN_IDS: return True
        await ctx.send("❌ 이 명령어를 사용할 권한이 없습니다. (관리자 전용)")
        return False
    return commands.check(predicate)

async def check_admin_interaction(interaction: discord.Interaction):
    if interaction.user.id in BOT_ADMIN_IDS: return True
    await interaction.response.send_message("❌ 관리자만 수정 가능합니다.", ephemeral=True)
    return False

# =========================
# 🔹 득템 관리 전용 UI
# =========================

# 1. 수정 모달
class EditDropModal(discord.ui.Modal):
    def __init__(self, drop_id, old_boss, old_winner, old_item):
        super().__init__(title="💎 득템 기록 수정")
        self.drop_id = drop_id
        self.boss_input = discord.ui.TextInput(label="보스명", default=old_boss)
        self.winner_input = discord.ui.TextInput(label="획득자", default=old_winner)
        self.item_input = discord.ui.TextInput(label="아이템", default=old_item)
        self.add_item(self.boss_input)
        self.add_item(self.winner_input)
        self.add_item(self.item_input)

    async def on_submit(self, interaction: discord.Interaction):
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE drops SET boss_name=%s, winner=%s, item_name=%s WHERE id=%s",
                               (self.boss_input.value, self.winner_input.value, self.item_input.value, self.drop_id))
                conn.commit()
            await interaction.response.send_message(f"✅ 기록(ID: {self.drop_id})이 수정되었습니다.", ephemeral=True)
        finally: release_db_connection(conn)

# 2. 추가 모달 (기존 양식 활용)
class AddDropModal(discord.ui.Modal, title="➕ 득템 수동 추가"):
    boss = discord.ui.TextInput(label="보스명")
    winner = discord.ui.TextInput(label="획득자")
    item = discord.ui.TextInput(label="아이템")
    def async on_submit(self, interaction: discord.Interaction):
        conn = get_db_connection()
        try:
            date_str = datetime.now(KST).strftime("%m-%d %H:%M")
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO drops (boss_name, winner, item_name, date) VALUES (%s, %s, %s, %s)",
                               (self.boss.value, self.winner.value, self.item.value, date_str))
                conn.commit()
            await interaction.response.send_message(f"✅ [{self.boss.value}] 기록이 추가되었습니다.", ephemeral=True)
        finally: release_db_connection(conn)

# 3. 득템현황 뷰 (페이지네이션 + 편집 버튼)
class DropListView(discord.ui.View):
    def __init__(self, drops, page=0):
        super().__init__(timeout=None)
        self.drops = drops
        self.page = page
        self.per_page = 10
        self.total_pages = (len(drops) - 1) // self.per_page + 1

    @discord.ui.button(label="이전", style=discord.ButtonStyle.gray)
    async def prev(self, i, b):
        if self.page > 0:
            self.page -= 1
            await i.response.edit_message(content=self.make_text(), view=self)

    @discord.ui.button(label="다음", style=discord.ButtonStyle.gray)
    async def next(self, i, b):
        if self.page < self.total_pages - 1:
            self.page += 1
            await i.response.edit_message(content=self.make_text(), view=self)

    @discord.ui.button(label="기록 수정/삭제", style=discord.ButtonStyle.primary)
    async def edit_btn(self, i, b):
        if not await check_admin_interaction(i): return
        await i.response.send_message("수정할 기록의 **ID 번호**를 입력해주세요. (예: `!득템수정 ID`)", ephemeral=True)

    @discord.ui.button(label="기록 추가", style=discord.ButtonStyle.success)
    async def add_btn(self, i, b):
        if not await check_admin_interaction(i): return
        await i.response.send_modal(AddDropModal())

    def make_text(self):
        start = self.page * self.per_page
        end = start + self.per_page
        current_drops = self.drops[start:end]
        text = f"💎 **득템 현황 전체 목록** (페이지 {self.page+1}/{self.total_pages})\n"
        text += "```" + "\n".join([f"ID:{d[0]} | {d[1]} | {d[2]} : {r[3]} ({r[4]})" for d in current_drops]) + "
```"
        return text

# =========================
# 🔹 봇 명령어
# =========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ... (기존 출석 관련 UI 클래스 유지됨) ...

@bot.command()
async def 득템현황(ctx):
    """모든 득템 현황 조회 (관리자 편집 기능 포함)"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, date, boss_name, winner, item_name FROM drops ORDER BY id DESC")
            rows = cursor.fetchall()
        if not rows: return await ctx.send("📝 기록된 득템 현황이 없습니다.")
        view = DropListView(rows)
        await ctx.send(view.make_text(), view=view)
    finally: release_db_connection(conn)

@bot.command()
@is_bot_admin()
async def 득템수정(ctx, drop_id: int):
    """특정 ID의 득템 기록 수정"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT boss_name, winner, item_name FROM drops WHERE id=%s", (drop_id,))
            row = cursor.fetchone()
        if not row: return await ctx.send(f"❌ ID {drop_id}번 기록을 찾을 수 없습니다.")
        
        # 수정 창 띄우기 (실제로는 버튼 클릭 유도를 위해 안내)
        view = discord.ui.View()
        edit_btn = discord.ui.Button(label=f"{drop_id}번 수정하기", style=discord.ButtonStyle.danger)
        async def edit_callback(i):
            if not await check_admin_interaction(i): return
            await i.response.send_modal(EditDropModal(drop_id, row[0], row[1], row[2]))
        edit_btn.callback = edit_callback
        view.add_item(edit_btn)
        
        delete_btn = discord.ui.Button(label=f"{drop_id}번 삭제", style=discord.ButtonStyle.secondary)
        async def delete_callback(i):
            if not await check_admin_interaction(i): return
            conn2 = get_db_connection()
            with conn2.cursor() as cur2:
                cur2.execute("DELETE FROM drops WHERE id=%s", (drop_id,))
                conn2.commit()
            release_db_connection(conn2)
            await i.response.send_message(f"✅ {drop_id}번 기록이 삭제되었습니다.", ephemeral=True)
        delete_btn.callback = delete_callback
        view.add_item(delete_btn)
        
        await ctx.send(f"🛠️ ID {drop_id}번에 대한 관리 도구:", view=view)
    finally: release_db_connection(conn)

# ... (기존 출석, 추가, 삭제, 조회, 기간조회 명령어 유지) ...

# =========================
# 🔹 보스 컷 관련 기존 로직 (수정 사항 반영)
# =========================
class DropModal(discord.ui.Modal, title="💎 득템 기록"):
    item_input = discord.ui.TextInput(label="아이템 이름")
    winner_input = discord.ui.TextInput(label="획득자 이름")
    def __init__(self, boss_name, view, orig_i):
        super().__init__()
        self.boss_name, self.view, self.orig_i = boss_name, view, orig_i
    async def on_submit(self, interaction: discord.Interaction):
        conn = get_db_connection()
        try:
            item, winner = self.item_input.value, self.winner_input.value
            date_str = datetime.now(KST).strftime("%m-%d %H:%M")
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO drops (item_name, winner, date, boss_name) VALUES (%s, %s, %s, %s)", (item, winner, date_str, self.boss_name))
                conn.commit()
            self.view.boss_status[self.boss_name] = f"✅ 컷 ({winner})"
            await self.orig_i.delete_original_response() # 선택창 삭제
            await interaction.response.send_message(f"✅ [{self.boss_name}] {winner}님 {item} 획득!", ephemeral=False)
        finally: release_db_connection(conn)

# ... (이하 생략 - 이전 버전의 나머지 모든 기능 포함) ...

@bot.event
async def on_ready():
    if not auto_boss_panel.is_running(): auto_boss_panel.start()
    print(f"✅ {bot.user} 온라인! 득템 관리 시스템 로드 완료")

keep_alive()
bot.run(DISCORD_TOKEN)