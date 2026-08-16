# -*- coding: utf-8 -*-
# =====================================================
# RAID BOT - 출석 전용 테스트 버전
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

# 일반 출석창
ATTENDANCE_CHANNEL_ID = 1538446431772217405

# 관리자 전용 비밀번호 방
ADMIN_CHANNEL_ID = 1538446527968706691

# 출석 패널 / 비밀번호 생성 시간
# 테스트 모드: 1분마다 새로운 출석 세션 생성
TEST_MODE = True
TEST_INTERVAL_MINUTES = 1

# 실제 운영 시간: 03:00 / 09:00 / 15:00 / 21:00
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

        # 테스트 모드에서는 각 분의 세션을 1분 동안만 유효하게 한다.
        # 운영 모드에서는 기존처럼 생성 시각 기준 3시간 동안 유효하다.
        try:
            if TEST_MODE:
                session_hour = int(current_slot[:2])
                session_minute = int(current_slot[2:4])
            else:
                session_hour = int(current_slot)
                session_minute = 0
        except Exception:
            return await interaction.response.send_message(
                "❌ 출석 세션 정보를 확인할 수 없습니다.",
                ephemeral=True
            )

        session_start = now.replace(
            hour=session_hour,
            minute=session_minute,
            second=0,
            microsecond=0
        )

        session_duration = timedelta(minutes=1) if TEST_MODE else timedelta(hours=3)

        if now < session_start or now >= session_start + session_duration:
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

    date_text = now.strftime("%Y-%m-%d")

    if TEST_MODE:
        # 테스트 모드에서는 매 분을 별도의 출석 세션으로 사용
        # 예: 22:01 -> 2201, 22:02 -> 2202
        slot_text = now.strftime("%H%M")
        panel_key = f"{date_text}_{slot_text}"
    else:
        # 운영 모드: 03:00 / 09:00 / 15:00 / 21:00
        if now.hour not in ATTENDANCE_HOURS:
            return

        if now.minute != 0:
            return

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
        # 테스트 모드에서는 새 세션을 만들기 전에 이전 패널/비밀번호를 삭제한다.
        # 별도의 cleanup 루프가 같은 분에 새 패널을 지우지 않도록 한다.
        if TEST_MODE:
            await clear_both_channels()

        # 일반 출석창
        validity_text = "1분" if TEST_MODE else "3시간"

        await attendance_channel.send(
            f"📢 **출석 시간입니다!**\n"
            f"🕒 {now.strftime('%H:%M:%S')} 출석\n\n"
            f"아래 버튼을 눌러 출석해주세요.\n"
            f"출석 시 **1점**이 지급됩니다.\n"
            f"⏰ 유효시간: {validity_text}",
            view=AttendanceView()
        )

        # 관리자 전용방
        await admin_channel.send(
            f"🔐 **출석 비밀번호**\n\n"
            f"```{password}```\n\n"
            f"🕒 출석 시간: {now.strftime('%H:%M:%S')}\n"
            f"⏰ 유효시간: {validity_text}\n"
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

    if TEST_MODE:
        # 테스트 모드에서는 automatic_attendance_panel이
        # 새 패널 생성 직전에 이전 메시지를 삭제하므로 여기서는 아무것도 하지 않는다.
        return

    # 운영 모드:
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
    # 관리자 전용방에서만 실행
    if not is_admin_channel(ctx):
        return await ctx.send(
            "❌ 이 명령어는 관리자 전용방에서만 사용할 수 있습니다."
        )

    await ctx.send(
        "⚠️ **DB 전체 초기화 경고** ⚠️\n\n"
        "🚨 **주의 요망** 🚨\n"
        "이 작업을 진행하면 현재 저장된 **모든 출석 정보가 초기화됩니다.**\n\n"
        "삭제되는 정보:\n"
        "• 모든 출석 기록\n"
        "• 출석 비밀번호 세션\n\n"
        "⚠️ **초기화 후에는 데이터를 복구할 수 없습니다.**\n\n"
        "초기화를 진행하려면 아래 문구를 **정확하게 입력하세요.**\n\n"
        "```초기화 동의```\n\n"
        "⏰ 30초 이내에 입력해야 합니다."
    )

    def check(message):
        return (
            message.author.id == ctx.author.id
            and message.channel.id == ctx.channel.id
            and message.content.strip() == "초기화 동의"
        )

    try:
        await bot.wait_for("message", timeout=30.0, check=check)
    except asyncio.TimeoutError:
        return await ctx.send(
            "⌛ **DB 초기화가 취소되었습니다.**\n"
            "30초 안에 `초기화 동의`가 입력되지 않았습니다."
        )

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "TRUNCATE TABLE attendance_v2 "
                "RESTART IDENTITY CASCADE"
            )

            cursor.execute(
                "TRUNCATE TABLE attendance_sessions "
                "RESTART IDENTITY CASCADE"
            )

            conn.commit()

        global current_password
        global current_date
        global current_slot
        global last_panel_key

        current_password = None
        current_date = None
        current_slot = None
        last_panel_key = None

        await ctx.send(
            "🧹 **DB 전체 초기화 완료**\n\n"
            "• 모든 출석 기록 삭제\n"
            "• 출석 비밀번호 세션 초기화\n"
            "• 현재 출석 세션 초기화 완료\n\n"
            "이제 Discord ID 기준으로 "
            "출석 기록을 새로 저장합니다."
        )

    except Exception as e:
        conn.rollback()

        await ctx.send(
            f"❌ **DB 초기화 실패**\n```{e}```"
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
    print(
        f"테스트모드: {TEST_MODE}, "
        f"출석패널 주기: {TEST_INTERVAL_MINUTES}분"
    )


# =====================================================
# 🔹 실행
# =====================================================
keep_alive()

bot.run(
    os.getenv("DISCORD_TOKEN")
)