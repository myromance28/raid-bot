import discord
from discord.ext import commands, tasks
import os
import psycopg2
from psycopg2 import pool
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread

# =========================
# 🔹 기본 설정
# =========================
KST = timezone(timedelta(hours=9))

BOSS_CHANNEL_ID = 1503420212794622073
LOG_CHANNEL_ID = 1495580902787514508

BOT_ADMIN_IDS = [
    1295279721050935306,
    1469924619170349169,
    1330608030844321884,
    1476159593330511954,
    344403970426535937
]

DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# =========================
# 🔹 PostgreSQL 연결
# =========================
try:
    if DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace(
            "postgresql://",
            "postgres://",
            1
        )

    db_pool = psycopg2.pool.ThreadedConnectionPool(
        1,
        10,
        DATABASE_URL
    )

except Exception as e:
    print(f"❌ PostgreSQL 연결 실패: {e}")
    exit()

def get_db_connection():
    return db_pool.getconn()

def release_db_connection(conn):
    db_pool.putconn(conn)

# =========================
# 🔹 DB 초기화
# =========================
def init_db():

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id SERIAL PRIMARY KEY,
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

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS drops (
                id SERIAL PRIMARY KEY,
                item_name TEXT,
                winner TEXT,
                date TEXT,
                boss_name TEXT
            )
            """)

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
# 🔹 Flask Keep Alive
# =========================
app = Flask('')

@app.route('/')
def home():
    return "Bot Online"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# =========================
# 🔹 디스코드 설정
# =========================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# =========================
# 🔹 관리자 체크
# =========================
def is_bot_admin():

    async def predicate(ctx):

        if ctx.author.id in BOT_ADMIN_IDS:
            return True

        await ctx.send("❌ 관리자 전용 명령어입니다.")
        return False

    return commands.check(predicate)

async def check_admin_interaction(interaction):

    if interaction.user.id in BOT_ADMIN_IDS:
        return True

    await interaction.response.send_message(
        "❌ 관리자만 가능합니다.",
        ephemeral=True
    )

    return False

# =========================
# 🔹 로그 출력
# =========================
async def send_log(message):

    channel = bot.get_channel(LOG_CHANNEL_ID)

    if channel:
        await channel.send(message)

# =========================
# 🔹 출석 등록 (관리자 전용)
# 사용:
# !출석 닉네임
# =========================
@bot.command()
@is_bot_admin()
async def 출석(ctx, 이름: str):

    today = datetime.now(KST).strftime("%Y-%m-%d")
    now_time = datetime.now(KST).strftime("%H:%M")

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
            SELECT *
            FROM attendance
            WHERE date=%s
            AND name=%s
            """, (
                today,
                이름
            ))

            exists = cursor.fetchone()

            if exists:
                return await ctx.send(
                    f"⚠️ {이름}님은 이미 출석 처리되었습니다."
                )

            cursor.execute("""
            INSERT INTO attendance (
                date,
                time_slot,
                name
            )
            VALUES (%s, %s, %s)
            """, (
                today,
                now_time,
                이름
            ))

            cursor.execute("""
            INSERT INTO members (
                name,
                total
            )
            VALUES (%s, 1)
            ON CONFLICT (name)
            DO UPDATE SET total = members.total + 1
            """, (이름,))

            conn.commit()

        await ctx.send(
            f"✅ {이름} 출석 완료"
        )

        await send_log(
            f"📌 출석 등록 | {이름}"
        )

    finally:
        release_db_connection(conn)

# =========================
# 🔹 출석 현황
# =========================
@bot.command()
async def 출석현황(ctx):

    today = datetime.now(KST).strftime("%Y-%m-%d")

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
            SELECT name, time_slot
            FROM attendance
            WHERE date=%s
            ORDER BY time_slot ASC
            """, (today,))

            rows = cursor.fetchall()

        if not rows:
            return await ctx.send("📭 오늘 출석 없음")

        msg = f"📅 {today} 출석 현황\n\n"

        for idx, row in enumerate(rows, start=1):
            msg += f"{idx}. {row[0]} ({row[1]})\n"

        await ctx.send(msg)

    finally:
        release_db_connection(conn)

# =========================
# 🔹 출석 순위
# =========================
@bot.command()
async def 출석순위(ctx):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
            SELECT name, total
            FROM members
            ORDER BY total DESC
            LIMIT 50
            """)

            rows = cursor.fetchall()

        if not rows:
            return await ctx.send("📭 데이터 없음")

        msg = "🏆 출석 순위\n\n"

        for idx, row in enumerate(rows, start=1):
            msg += f"{idx}. {row[0]} - {row[1]}점\n"

        await ctx.send(msg)

    finally:
        release_db_connection(conn)

# =========================
# 🔹 출석 점수 수정
# 사용:
# !출석수정 이름 점수
# =========================
@bot.command()
@is_bot_admin()
async def 출석수정(ctx, 이름: str, 점수: int):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
            INSERT INTO members (
                name,
                total
            )
            VALUES (%s, %s)
            ON CONFLICT (name)
            DO UPDATE SET total=%s
            """, (
                이름,
                점수,
                점수
            ))

            conn.commit()

        await ctx.send(
            f"✅ {이름} 점수가 "
            f"{점수}점으로 변경되었습니다."
        )

        await send_log(
            f"🛠️ 출석수정 | {이름} → {점수}"
        )

    finally:
        release_db_connection(conn)

# =========================
# 🔹 기간 조회
# 사용:
# !기간조회 2026-05-01 2026-05-15
# =========================
@bot.command()
async def 기간조회(ctx, 시작일: str, 종료일: str):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
            SELECT name, COUNT(*)
            FROM attendance
            WHERE date BETWEEN %s AND %s
            GROUP BY name
            ORDER BY COUNT(*) DESC
            """, (
                시작일,
                종료일
            ))

            rows = cursor.fetchall()

        if not rows:
            return await ctx.send(
                "📭 해당 기간 기록 없음"
            )

        msg = (
            f"📅 기간조회\n"
            f"{시작일} ~ {종료일}\n\n"
        )

        rank = 1

        for row in rows:

            msg += (
                f"{rank}. "
                f"{row[0]} - "
                f"{row[1]}점\n"
            )

            rank += 1

        await ctx.send(msg)

    finally:
        release_db_connection(conn)

# =========================
# 🔹 보스 목록 로드
# =========================
def load_bosses():

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
            SELECT boss_name
            FROM boss_list
            ORDER BY boss_name
            """)

            rows = cursor.fetchall()

        return [r[0] for r in rows]

    finally:
        release_db_connection(conn)

# =========================
# 🔹 보스 추가
# =========================
@bot.command()
@is_bot_admin()
async def 보스추가(ctx, *, boss_name):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
            INSERT INTO boss_list (boss_name)
            VALUES (%s)
            ON CONFLICT DO NOTHING
            """, (boss_name,))

            conn.commit()

        await ctx.send(
            f"✅ 보스 추가 완료: {boss_name}"
        )

    finally:
        release_db_connection(conn)

# =========================
# 🔹 보스 삭제
# =========================
@bot.command()
@is_bot_admin()
async def 보스삭제(ctx, *, boss_name):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
            DELETE FROM boss_list
            WHERE boss_name=%s
            """, (boss_name,))

            conn.commit()

        await ctx.send(
            f"🗑️ 보스 삭제 완료: {boss_name}"
        )

    finally:
        release_db_connection(conn)

# =========================
# 🔹 득템 수정 모달
# =========================
class EditDropModal(discord.ui.Modal):

    def __init__(self, drop_id, old_boss, old_winner, old_item):

        super().__init__(title="💎 득템 수정")

        self.drop_id = drop_id

        self.boss_input = discord.ui.TextInput(
            label="보스명",
            default=old_boss
        )

        self.winner_input = discord.ui.TextInput(
            label="획득자",
            default=old_winner
        )

        self.item_input = discord.ui.TextInput(
            label="아이템",
            default=old_item
        )

        self.add_item(self.boss_input)
        self.add_item(self.winner_input)
        self.add_item(self.item_input)

    async def on_submit(self, interaction):

        conn = get_db_connection()

        try:
            with conn.cursor() as cursor:

                cursor.execute("""
                UPDATE drops
                SET boss_name=%s,
                    winner=%s,
                    item_name=%s
                WHERE id=%s
                """, (
                    self.boss_input.value,
                    self.winner_input.value,
                    self.item_input.value,
                    self.drop_id
                ))

                conn.commit()

            await interaction.response.send_message(
                f"✅ 수정 완료 (ID:{self.drop_id})",
                ephemeral=True
            )

        finally:
            release_db_connection(conn)

# =========================
# 🔹 득템 추가 모달
# =========================
class AddDropModal(discord.ui.Modal, title="➕ 득템 추가"):

    boss = discord.ui.TextInput(label="보스명")
    winner = discord.ui.TextInput(label="획득자")
    item = discord.ui.TextInput(label="아이템")

    async def on_submit(self, interaction):

        conn = get_db_connection()

        try:
            date_str = datetime.now(KST).strftime("%m-%d %H:%M")

            with conn.cursor() as cursor:

                cursor.execute("""
                INSERT INTO drops (
                    boss_name,
                    winner,
                    item_name,
                    date
                )
                VALUES (%s, %s, %s, %s)
                """, (
                    self.boss.value,
                    self.winner.value,
                    self.item.value,
                    date_str
                ))

                conn.commit()

            await interaction.response.send_message(
                f"✅ [{self.boss.value}] 추가 완료",
                ephemeral=True
            )

        finally:
            release_db_connection(conn)

# =========================
# 🔹 득템 목록 View
# =========================
class DropListView(discord.ui.View):

    def __init__(self, drops, page=0):

        super().__init__(timeout=None)

        self.drops = drops
        self.page = page
        self.per_page = 10

        self.total_pages = (
            (len(drops) - 1) // self.per_page
        ) + 1

    def make_text(self):

        start = self.page * self.per_page
        end = start + self.per_page

        current = self.drops[start:end]

        text = (
            f"💎 득템 현황 "
            f"({self.page+1}/{self.total_pages})\n```"
        )

        for d in current:

            text += (
                f"ID:{d[0]} | "
                f"{d[1]} | "
                f"{d[2]} | "
                f"{d[3]} ({d[4]})\n"
            )

        text += "```"

        return text

    @discord.ui.button(
        label="이전",
        style=discord.ButtonStyle.gray
    )
    async def prev(self, interaction, button):

        if self.page > 0:

            self.page -= 1

            await interaction.response.edit_message(
                content=self.make_text(),
                view=self
            )

    @discord.ui.button(
        label="다음",
        style=discord.ButtonStyle.gray
    )
    async def next(self, interaction, button):

        if self.page < self.total_pages - 1:

            self.page += 1

            await interaction.response.edit_message(
                content=self.make_text(),
                view=self
            )

    @discord.ui.button(
        label="기록 추가",
        style=discord.ButtonStyle.success
    )
    async def add_btn(self, interaction, button):

        if not await check_admin_interaction(interaction):
            return

        await interaction.response.send_modal(
            AddDropModal()
        )

# =========================
# 🔹 득템 현황
# =========================
@bot.command()
async def 득템현황(ctx):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
            SELECT id,
                   date,
                   boss_name,
                   winner,
                   item_name
            FROM drops
            ORDER BY id DESC
            """)

            rows = cursor.fetchall()

        if not rows:
            return await ctx.send("📭 득템 기록 없음")

        view = DropListView(rows)

        await ctx.send(
            view.make_text(),
            view=view
        )

    finally:
        release_db_connection(conn)

# =========================
# 🔹 득템 수정
# =========================
@bot.command()
@is_bot_admin()
async def 득템수정(ctx, drop_id: int):

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute("""
            SELECT boss_name,
                   winner,
                   item_name
            FROM drops
            WHERE id=%s
            """, (drop_id,))

            row = cursor.fetchone()

        if not row:
            return await ctx.send("❌ 기록 없음")

        view = discord.ui.View()

        edit_btn = discord.ui.Button(
            label="수정",
            style=discord.ButtonStyle.primary
        )

        async def edit_callback(interaction):

            if not await check_admin_interaction(interaction):
                return

            await interaction.response.send_modal(
                EditDropModal(
                    drop_id,
                    row[0],
                    row[1],
                    row[2]
                )
            )

        edit_btn.callback = edit_callback

        delete_btn = discord.ui.Button(
            label="삭제",
            style=discord.ButtonStyle.danger
        )

        async def delete_callback(interaction):

            if not await check_admin_interaction(interaction):
                return

            conn2 = get_db_connection()

            try:
                with conn2.cursor() as cur2:

                    cur2.execute("""
                    DELETE FROM drops
                    WHERE id=%s
                    """, (drop_id,))

                    conn2.commit()

                await interaction.response.send_message(
                    f"🗑️ 삭제 완료 ({drop_id})",
                    ephemeral=True
                )

            finally:
                release_db_connection(conn2)

        delete_btn.callback = delete_callback

        view.add_item(edit_btn)
        view.add_item(delete_btn)

        await ctx.send(
            f"🛠️ ID {drop_id} 관리",
            view=view
        )

    finally:
        release_db_connection(conn)

# =========================
# 🔹 보스 컷 모달
# =========================
class DropModal(discord.ui.Modal, title="💎 득템 기록"):

    item_input = discord.ui.TextInput(
        label="아이템 이름"
    )

    winner_input = discord.ui.TextInput(
        label="획득자"
    )

    def __init__(self, boss_name, view):

        super().__init__()

        self.boss_name = boss_name
        self.view_ref = view

    async def on_submit(self, interaction):

        conn = get_db_connection()

        try:
            item = self.item_input.value
            winner = self.winner_input.value

            date_str = datetime.now(KST).strftime(
                "%m-%d %H:%M"
            )

            with conn.cursor() as cursor:

                cursor.execute("""
                INSERT INTO drops (
                    item_name,
                    winner,
                    date,
                    boss_name
                )
                VALUES (%s, %s, %s, %s)
                """, (
                    item,
                    winner,
                    date_str,
                    self.boss_name
                ))

                conn.commit()

            self.view_ref.boss_status[
                self.boss_name
            ] = f"✅ 컷 ({winner})"

            await interaction.response.send_message(
                f"✅ [{self.boss_name}] "
                f"{winner}님 {item} 획득!",
                ephemeral=False
            )

            await send_log(
                f"💎 [{self.boss_name}] "
                f"{winner} - {item}"
            )

        finally:
            release_db_connection(conn)

# =========================
# 🔹 보스 패널
# =========================
class BossView(discord.ui.View):

    def __init__(self):

        super().__init__(timeout=None)

        self.boss_status = {}

        bosses = load_bosses()

        for boss in bosses:

            self.boss_status[boss] = "대기"

            btn = discord.ui.Button(
                label=boss,
                style=discord.ButtonStyle.primary
            )

            async def callback(
                interaction,
                boss_name=boss
            ):

                await interaction.response.send_modal(
                    DropModal(
                        boss_name,
                        self
                    )
                )

            btn.callback = callback

            self.add_item(btn)

# =========================
# 🔹 자동 보스 패널
# =========================
@tasks.loop(hours=6)
async def auto_boss_panel():

    channel = bot.get_channel(
        BOSS_CHANNEL_ID
    )

    if channel:

        view = BossView()

        await channel.send(
            "👑 보스 선택 패널",
            view=view
        )

# =========================
# 🔹 도움말
# =========================
@bot.command()
async def 도움말(ctx):

    msg = """
📌 일반 명령어

!출석현황
!출석순위
!기간조회 시작일 종료일
!득템현황

예시:
!기간조회 2026-05-01 2026-05-15

👑 관리자 명령어

!출석 이름
!출석수정 이름 점수
!보스추가 보스명
!보스삭제 보스명
!득템수정 ID
"""

    await ctx.send(msg)

# =========================
# 🔹 봇 시작
# =========================
@bot.event
async def on_ready():

    if not auto_boss_panel.is_running():
        auto_boss_panel.start()

    print(f"✅ {bot.user} 온라인")

keep_alive()
bot.run(DISCORD_TOKEN)