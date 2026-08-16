# -*- coding: utf-8 -*-
# =====================================================
# RAID BOT - 출석 전용 개편 버전
# =====================================================

import os
import asyncio
import random
import string
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
import psycopg2
from flask import Flask
from threading import Thread

# =====================================================
# 🔹 설정
# =====================================================
KST = timezone(timedelta(hours=9))

# 일반 혈맹 출석창
ATTENDANCE_CHANNEL_ID = 1538446431772217405

# 관리자 전용 명령어/비밀번호 방
ADMIN_CHANNEL_ID = 1538446527968706691

# 출석 패널 / 비밀번호 생성 시간
ATTENDANCE_HOURS = {3, 9, 15, 21}

# =====================================================
# 🔹 관리자 체크
# =====================================================
# 기존 ADMIN_IDS는 완전히 제거했습니다.
# 이제 "관리자 전용 채널"에 들어갈 수 있는 사람이
# 관리자 기능을 사용할 수 있습니다.
def is_admin_channel(ctx):
    return ctx.channel.id == ADMIN_CHANNEL_ID


# =====================================================
# 🔹 PostgreSQL
# =====================================================
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise Exception("DATABASE_URL 환경변수 없음")


def get_db_connection():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require"
    )


def release_db_connection(conn):
    try:
        conn.close()
    except Exception:
        pass


# =====================================================
# 🔹 Flask KeepAlive
# =====================================================
app = Flask(__name__)


@app.route("/")
def home():
    return "OK"


def run():
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        threaded=True
    )


def keep_alive():
    Thread(target=run, daemon=True).start()


# =====================================================
# 🔹 현재 출석 세션 / 비밀번호
# =====================================================
current_password = None
current_date = None
current_slot = None
last_panel_key = None


def get_current_slot(hour=None):
    if hour is None:
        hour = datetime.now(KST).hour

    return f"{hour:02d}"


def generate_password():
    return "".join(random.choices(string.digits, k=4))


def get_current_session():
    now = datetime.now(KST)
    return (
        now.strftime("%Y-%m-%d"),
        get_current_slot(now.hour)
    )


# =====================================================
# 🔹 DB 테이블 생성 / 기존 데이터 구조 정리
# =====================================================
def init_database():
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:

            # -------------------------------------------------
            # 새 출석 전용 테이블
            #
            # user_id  : 실제 Discord 사용자 ID
            # username : 당시 표시 이름
            # points   : 기본 1점
            # -------------------------------------------------
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS attendance_v2 (
                    id SERIAL PRIMARY KEY,
                    date TEXT NOT NULL,
                    time_slot TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT NOT NULL,
                    points INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS attendance_v2_unique_idx
                ON attendance_v2(date, time_slot, user_id)
            """)

            # 현재 출석 세션/비밀번호 저장
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS attendance_sessions (
                    date TEXT NOT NULL,
                    time_slot TEXT NOT NULL,
                    password TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (date, time_slot)
                )
            """)

            conn.commit()

    finally:
        release_db_connection(conn)




# =====================================================
# 🔹 출석 세션 저장 / 복구
# =====================================================
def save_session(date, slot, password):
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO attendance_sessions
                    (date, time_slot, password)
                VALUES (%s, %s, %s)
                ON CONFLICT (date, time_slot)
                DO UPDATE SET password=EXCLUDED.password
            """, (date, slot, password))

            conn.commit()

    finally:
        release_db_connection(conn)


def load_active_session():
    now = datetime.now(KST)

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT date, time_slot, password
                FROM attendance_sessions
                ORDER BY created_at DESC
                LIMIT 1
            """)

            row = cursor.fetchone()

        if not row:
            return None

        date_text, slot_text, password = row

        if date_text != now.strftime("%Y-%m-%d"):
            return None

        try:
            session_hour = int(slot_text)
        except Exception:
            return None

        session_start = now.replace(
            hour=session_hour,
            minute=0,
            second=0,
            microsecond=0
        )

        if session_start <= now < session_start + timedelta(hours=3):
            return row

        return None

    finally:
        release_db_connection(conn)


init_database()
_active_session = load_active_session()
if _active_session:
    current_date, current_slot, current_password = _active_session
# =====================================================
# 🔹 출석 DB 함수
# =====================================================
def has_attendance(date, slot, user_id):
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT 1
                FROM attendance_v2
                WHERE date=%s
                  AND time_slot=%s
                  AND user_id=%s
                LIMIT 1
            """, (date, slot, user_id))

            return cursor.fetchone() is not None

    finally:
        release_db_connection(conn)


def save_attendance(date, slot, user_id, username):
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO attendance_v2
                    (date, time_slot, user_id, username, points)
                VALUES
                    (%s, %s, %s, %s, 1)
                ON CONFLICT (date, time_slot, user_id)
                DO NOTHING
            """, (
                date,
                slot,
                user_id,
                username
            ))

            inserted = cursor.rowcount > 0
            conn.commit()

            return inserted

    except Exception:
        conn.rollback()
        raise

    finally:
        release_db_connection(conn)


# =====================================================
# 🔹 출석 패널
# =====================================================
class AttendanceView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ 출석하기",
        style=discord.ButtonStyle.success,
        custom_id="raid_attendance_button"
    )
    async def attendance_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        # 출석창은 일반 혈맹 출석창에서만 사용
        if interaction.channel_id != ATTENDANCE_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ 이 버튼은 일반 혈맹 출석창에서만 사용할 수 있습니다.",
                ephemeral=True
            )

        await interaction.response.send_modal(
            AttendancePasswordModal()
        )


# =====================================================
# 🔹 출석 비밀번호 입력 팝업
# =====================================================
class AttendancePasswordModal(
    discord.ui.Modal,
    title="🔐 출석 비밀번호"
):

    password_input = discord.ui.TextInput(
        label="4자리 비밀번호",
        placeholder="관리자 전용방에 표시된 4자리 번호를 입력하세요.",
        min_length=4,
        max_length=4,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):

        global current_password
        global current_date
        global current_slot

        if interaction.channel_id != ATTENDANCE_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ 일반 혈맹 출석창에서만 출석할 수 있습니다.",
                ephemeral=True
            )

        now = datetime.now(KST)

        # 패널 시간 이후 3시간 동안만 유효
        if current_password is None:
            return await interaction.response.send_message(
                "❌ 현재 출석 가능한 시간이 아닙니다.",
                ephemeral=True
            )

        # 현재 세션의 날짜/시간대 확인
        expected_date = now.strftime("%Y-%m-%d")

        if current_date != expected_date:
            return await interaction.response.send_message(
                "❌ 현재 출석 세션이 종료되었습니다.",
                ephemeral=True
            )

        # 비밀번호 세션은 생성 시각 기준 3시간 동안 유효
        try:
            session_hour = int(current_slot)
        except Exception:
            return await interaction.response.send_message(
                "❌ 출석 세션 정보를 확인할 수 없습니다.",
                ephemeral=True
            )

        session_start = now.replace(
            hour=session_hour,
            minute=0,
            second=0,
            microsecond=0
        )

        # 3시 세션은 당일 3시부터 6시까지,
        # 9시 세션은 9시부터 12시까지 등
        if now < session_start or now >= session_start + timedelta(hours=3):
            return await interaction.response.send_message(
                "❌ 출석 가능 시간이 종료되었습니다.",
                ephemeral=True
            )

        entered = str(self.password_input.value).strip()

        if entered != current_password:
            return await interaction.response.send_message(
                "❌ 비밀번호가 틀렸습니다.",
                ephemeral=True
            )

        user_id = interaction.user.id

        # 같은 세션에 한 번만 출석 가능
        if has_attendance(current_date, current_slot, user_id):
            return await interaction.response.send_message(
                "⚠️ 이미 이번 출석에 참여하셨습니다.",
                ephemeral=True
            )

        username = interaction.user.display_name

        inserted = save_attendance(
            current_date,
            current_slot,
            user_id,
            username
        )

        if not inserted:
            return await interaction.response.send_message(
                "⚠️ 이미 이번 출석에 참여하셨습니다.",
                ephemeral=True
            )

        await interaction.response.send_message(
            f"✅ 출석 완료!\n"
            f"👤 {username}\n"
            f"⭐ 1점이 지급되었습니다.",
            ephemeral=True
        )


# =====================================================
# 🔹 채널 전체 메시지 삭제
# =====================================================
async def clear_channel(channel):
    try:
        deleted = await channel.purge(
            limit=None,
            bulk=False
        )

        print(
            f"[채널 초기화] #{channel.name} : "
            f"{len(deleted)}개 삭제"
        )

    except discord.Forbidden:
        print(
            f"[채널 초기화 실패] #{channel.name} "
            f"권한 부족"
        )

    except discord.HTTPException as e:
        print(
            f"[채널 초기화 실패] #{channel.name}: {e}"
        )

    except Exception as e:
        print(
            f"[채널 초기화 오류] #{channel.name}: {e}"
        )


async def clear_both_channels():
    attendance_channel = bot.get_channel(
        ATTENDANCE_CHANNEL_ID
    )

    admin_channel = bot.get_channel(
        ADMIN_CHANNEL_ID
    )

    if attendance_channel:
        await clear_channel(attendance_channel)

    if admin_channel:
        await clear_channel(admin_channel)


# =====================================================
# 🔹 자동 출석 패널 + 비밀번호
# =====================================================
@tasks.loop(minutes=1)
async def automatic_attendance_panel():

    global current_password
    global current_date
    global current_slot
    global last_panel_key

    now = datetime.now(KST)

    # 03:00 / 09:00 / 15:00 / 21:00
    if now.hour not in ATTENDANCE_HOURS:
        return

    if now.minute != 0:
        return

    date_text = now.strftime("%Y-%m-%d")
    slot_text = get_current_slot(now.hour)

    panel_key = f"{date_text}_{slot_text}"

    # 재실행/루프 중복 방지
    if last_panel_key == panel_key:
        return

    attendance_channel = bot.get_channel(
        ATTENDANCE_CHANNEL_ID
    )

    admin_channel = bot.get_channel(
        ADMIN_CHANNEL_ID
    )

    if not attendance_channel:
        print(
            f"[자동 패널 실패] 출석 채널 없음: "
            f"{ATTENDANCE_CHANNEL_ID}"
        )
        return

    if not admin_channel:
        print(
            f"[자동 패널 실패] 관리자 채널 없음: "
            f"{ADMIN_CHANNEL_ID}"
        )
        return

    password = generate_password()

    current_password = password
    current_date = date_text
    current_slot = slot_text

    # 비밀번호를 DB에도 저장해 봇 재시작 시 복구 가능하게 함
    save_session(
        date_text,
        slot_text,
        password
    )

    try:
        # 일반 혈맹 출석창
        await attendance_channel.send(
            f"📢 **출석 시간입니다!**\n"
            f"🕒 {now.strftime('%H:%M')} 출석\n\n"
            f"아래 버튼을 눌러 출석해주세요.\n"
            f"출석 시 **1점**이 지급됩니다.",
            view=AttendanceView()
        )

        # 관리자 전용방
        await admin_channel.send(
            f"🔐 **출석 비밀번호**\n\n"
            f"```{password}```\n\n"
            f"🕒 출석 시간: {now.strftime('%H:%M')}\n"
            f"⏰ 유효시간: 3시간\n"
            f"⚠️ 이 번호는 혈맹원에게 알려주세요."
        )

        last_panel_key = panel_key

        print(
            f"[자동 출석] {date_text} "
            f"[{slot_text}:00] "
            f"비밀번호={password}"
        )

    except Exception as e:
        print(f"[자동 출석 패널 오류] {e}")


# =====================================================
# 🔹 3시간 후 채널 전체 초기화
# =====================================================
@tasks.loop(minutes=1)
async def automatic_channel_cleanup():

    now = datetime.now(KST)

    # 03:00 생성 → 06:00 삭제
    # 09:00 생성 → 12:00 삭제
    # 15:00 생성 → 18:00 삭제
    # 21:00 생성 → 00:00 삭제
    cleanup_hours = {0, 6, 12, 18}

    if now.hour not in cleanup_hours:
        return

    if now.minute != 0:
        return

    await clear_both_channels()

    print(
        f"[자동 채널 초기화] "
        f"{now.strftime('%Y-%m-%d %H:%M')}"
    )


# =====================================================
# 🔹 봇 설정
# =====================================================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# 봇 시작 시 출석 버튼을 Persistent View로 등록
bot.add_view(AttendanceView())


# =====================================================
# 🔹 관리자 전용 명령어
# =====================================================
@bot.command(name="DB초기화")
async def db_reset(ctx):

    # 모든 명령어는 관리자 전용방에서만 작동
    if not is_admin_channel(ctx):
        return await ctx.send(
            "❌ 이 명령어는 관리자 전용방에서만 사용할 수 있습니다."
        )

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            # 새 출석 데이터 전부 삭제
            cursor.execute(
                "TRUNCATE TABLE attendance_v2 RESTART IDENTITY"
            )

            # 혹시 기존 버전의 테이블이 남아 있다면
            # 기존 데이터도 함께 비움
            for table in (
                "attendance",
                "members",
                "drops",
                "boss_list",
                "bonus_points"
            ):
                cursor.execute(
                    f'TRUNCATE TABLE IF EXISTS "{table}" '
                    f'RESTART IDENTITY CASCADE'
                )

            cursor.execute(
                "TRUNCATE TABLE attendance_sessions "
                "RESTART IDENTITY CASCADE"
            )

            conn.commit()

        # 현재 출석 세션도 초기화
        global current_password
        global current_date
        global current_slot

        current_password = None
        current_date = None
        current_slot = None

        await ctx.send(
            "🧹 **DB 전체 초기화 완료**\n\n"
            "• 모든 출석 기록 삭제\n"
            "• 기존 혈맹원 데이터 삭제\n"
            "• 기존 보스 데이터 삭제\n"
            "• 기존 득템 데이터 삭제\n"
            "• 기존 가산점 데이터 삭제\n"
            "• 출석 비밀번호 세션 초기화\n\n"
            "이제 Discord ID 기준으로 "
            "출석 기록을 새로 저장합니다."
        )

    except Exception as e:
        conn.rollback()

        await ctx.send(
            f"❌ DB 초기화 실패\n```{e}```"
        )

    finally:
        release_db_connection(conn)


# =====================================================
# 🔹 명령어 오류
# =====================================================
@bot.event
async def on_command_error(ctx, error):

    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.CheckFailure):
        return

    print(
        f"[COMMAND ERROR] {ctx.command}: {error}"
    )


# =====================================================
# 🔹 봇 시작
# =====================================================
@bot.event
async def on_ready():

    if not automatic_attendance_panel.is_running():
        automatic_attendance_panel.start()

    if not automatic_channel_cleanup.is_running():
        automatic_channel_cleanup.start()

    print(f"로그인 완료: {bot.user}")
    print(
        f"출석채널: {ATTENDANCE_CHANNEL_ID}"
    )
    print(
        f"관리자채널: {ADMIN_CHANNEL_ID}"
    )


# =====================================================
# 🔹 실행
# =====================================================
keep_alive()

bot.run(
    os.getenv("DISCORD_TOKEN")
)