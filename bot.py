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


async def run_db(func, *args):
    """동기 psycopg2 작업을 별도 스레드에서 실행해 Discord 이벤트 루프를 막지 않음."""
    return await asyncio.to_thread(func, *args)


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

# =====================================================
# 🔹 현재 가산점 세션
# =====================================================
bonus_password = None
bonus_date = None
bonus_slot = None
bonus_points = None
bonus_created_at = None
bonus_message_ids = set()
boss_names = []
boss_panel_message_id = None


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

            # 가산점 세션
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bonus_sessions (
                    id SERIAL PRIMARY KEY,
                    date TEXT NOT NULL,
                    time_slot TEXT NOT NULL,
                    points INTEGER NOT NULL,
                    password TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(date, time_slot)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS boss_list (
                    id SERIAL PRIMARY KEY,
                    boss_name TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS boss_drops (
                    id SERIAL PRIMARY KEY,
                    boss_name TEXT NOT NULL,
                    drop_name TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 가산점 출석 기록
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bonus_attendance (
                    id SERIAL PRIMARY KEY,
                    date TEXT NOT NULL,
                    time_slot TEXT NOT NULL,
                    points INTEGER NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(date, time_slot, user_id)
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
            if TEST_MODE:
                # 테스트 모드의 세션은 HHMM 형식 (예: 2238)
                if len(str(slot_text)) != 4:
                    return None
                session_hour = int(str(slot_text)[:2])
                session_minute = int(str(slot_text)[2:4])

                if not (0 <= session_hour <= 23 and 0 <= session_minute <= 59):
                    return None
            else:
                # 운영 모드의 세션은 HH 형식 (예: 21)
                session_hour = int(slot_text)
                session_minute = 0

                if not (0 <= session_hour <= 23):
                    return None
        except Exception:
            return None

        session_start = now.replace(
            hour=session_hour,
            minute=session_minute,
            second=0,
            microsecond=0
        )

        session_duration = (
            timedelta(minutes=1)
            if TEST_MODE
            else timedelta(hours=3)
        )

        if session_start <= now < session_start + session_duration:
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
# 🔹 가산점 DB 함수
# =====================================================
def save_bonus_session(date_text, slot_text, points, password):
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO bonus_sessions
                    (date, time_slot, points, password)
                VALUES
                    (%s, %s, %s, %s)
                ON CONFLICT (date, time_slot)
                DO UPDATE SET
                    points = EXCLUDED.points,
                    password = EXCLUDED.password,
                    created_at = CURRENT_TIMESTAMP
            """, (
                date_text,
                slot_text,
                points,
                password
            ))

            conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        release_db_connection(conn)


def has_bonus_attendance(date_text, slot_text, user_id):
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT 1
                FROM bonus_attendance
                WHERE date=%s
                  AND time_slot=%s
                  AND user_id=%s
                LIMIT 1
            """, (
                date_text,
                slot_text,
                user_id
            ))

            return cursor.fetchone() is not None

    finally:
        release_db_connection(conn)


def save_bonus_attendance(
    date_text,
    slot_text,
    points,
    user_id,
    username
):
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO bonus_attendance
                    (date, time_slot, points, user_id, username)
                VALUES
                    (%s, %s, %s, %s, %s)
                ON CONFLICT (date, time_slot, user_id)
                DO NOTHING
            """, (
                date_text,
                slot_text,
                points,
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


def delete_bonus_session(date_text, slot_text):
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                DELETE FROM bonus_sessions
                WHERE date=%s AND time_slot=%s
            """, (date_text, slot_text))

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        release_db_connection(conn)


# =====================================================
# 🔹 보스 기능
# =====================================================
def load_bosses():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT boss_name FROM boss_list ORDER BY id ASC")
            return [row[0] for row in cursor.fetchall()]
    finally:
        release_db_connection(conn)


def add_boss_db(name):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO boss_list (boss_name) VALUES (%s) "
                "ON CONFLICT (boss_name) DO NOTHING RETURNING id",
                (name,)
            )
            ok = cursor.fetchone() is not None
        conn.commit()
        return ok
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def delete_boss_db(name):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM boss_list WHERE boss_name=%s", (name,))
            ok = cursor.rowcount > 0
        conn.commit()
        return ok
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def save_boss_drop_db(boss_name, drop_name, user_id, username):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO boss_drops "
                "(boss_name, drop_name, user_id, username) "
                "VALUES (%s,%s,%s,%s)",
                (boss_name, drop_name, user_id, username)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


async def send_boss_panel():
    global boss_panel_message_id
    admin_channel = bot.get_channel(ADMIN_CHANNEL_ID)
    if admin_channel is None:
        return

    if boss_panel_message_id:
        try:
            old = await admin_channel.fetch_message(boss_panel_message_id)
            await old.delete()
        except Exception:
            pass
        boss_panel_message_id = None

    if not boss_names:
        return

    msg = await admin_channel.send(
        "👹 **보스 목록**\n"
        "아래 빨간색 버튼을 눌러 득템 이름을 입력하세요.",
        view=BossPanelView()
    )
    boss_panel_message_id = msg.id


class BossDropModal(discord.ui.Modal, title="🎁 득템 이름 입력"):
    drop_name = discord.ui.TextInput(
        label="득템 이름",
        placeholder="획득한 아이템 이름을 입력하세요.",
        min_length=1,
        max_length=100,
        required=True
    )

    def __init__(self, boss_name):
        super().__init__()
        self.boss_name = boss_name

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            item = str(self.drop_name.value).strip()
            await run_db(
                save_boss_drop_db,
                self.boss_name,
                item,
                interaction.user.id,
                interaction.user.display_name
            )
            await interaction.followup.send(
                f"🎁 **득템 등록 완료**\n"
                f"👹 보스: **{self.boss_name}**\n"
                f"💎 득템: **{item}**",
                ephemeral=True
            )
        except Exception as e:
            print(f"[보스 득템 오류] {type(e).__name__}: {e}")
            await interaction.followup.send(
                "❌ 득템 저장 중 오류가 발생했습니다.",
                ephemeral=True
            )


class BossButton(discord.ui.Button):
    def __init__(self, boss_name):
        super().__init__(label=boss_name, style=discord.ButtonStyle.danger)
        self.boss_name = boss_name

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BossDropModal(self.boss_name))


class BossPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for name in boss_names:
            self.add_item(BossButton(name))


# =====================================================
# 🔹 출석 패널
# =====================================================
class BonusPasswordModal(
    discord.ui.Modal,
    title="🔵 가산점 비밀번호"
):
    password_input = discord.ui.TextInput(
        label="4자리 가산점 비밀번호",
        placeholder="관리자 전용방에 표시된 번호를 입력하세요.",
        min_length=4,
        max_length=4,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        global bonus_password
        global bonus_date
        global bonus_slot
        global bonus_points
        global bonus_created_at

        if interaction.channel_id != ATTENDANCE_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ 일반 혈맹 출석창에서만 사용할 수 있습니다.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            if (
                bonus_password is None
                or bonus_date is None
                or bonus_slot is None
                or bonus_points is None
                or bonus_created_at is None
            ):
                return await interaction.followup.send(
                    "❌ 현재 활성화된 가산점이 없습니다.",
                    ephemeral=True
                )

            now = datetime.now(KST)

            # 가산점은 생성 시점부터 정확히 1시간 유효
            if now >= bonus_created_at + timedelta(hours=1):
                bonus_password = None
                bonus_date = None
                bonus_slot = None
                bonus_points = None
                bonus_created_at = None

                return await interaction.followup.send(
                    "❌ 가산점 유효시간이 종료되었습니다.",
                    ephemeral=True
                )

            entered = str(self.password_input.value).strip()

            if entered != bonus_password:
                return await interaction.followup.send(
                    "❌ 가산점 비밀번호가 틀렸습니다.",
                    ephemeral=True
                )

            user_id = interaction.user.id
            username = interaction.user.display_name

            already = await run_db(
                has_bonus_attendance,
                bonus_date,
                bonus_slot,
                user_id
            )

            if already:
                return await interaction.followup.send(
                    "⚠️ 이미 이번 가산점을 받으셨습니다.",
                    ephemeral=True
                )

            inserted = await run_db(
                save_bonus_attendance,
                bonus_date,
                bonus_slot,
                bonus_points,
                user_id,
                username
            )

            if not inserted:
                return await interaction.followup.send(
                    "⚠️ 이미 이번 가산점을 받으셨습니다.",
                    ephemeral=True
                )

            await interaction.followup.send(
                f"🔵 **가산점 +{bonus_points}점 지급 완료!**\n"
                f"👤 {username}\n"
                f"⭐ +{bonus_points}점",
                ephemeral=True
            )

        except Exception as e:
            print(
                f"[가산점 처리 오류] "
                f"{type(e).__name__}: {e}"
            )

            try:
                await interaction.followup.send(
                    "❌ 가산점 처리 중 오류가 발생했습니다.",
                    ephemeral=True
                )
            except Exception:
                pass


class BonusButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="🔵 가산점",
            style=discord.ButtonStyle.primary,
            custom_id="raid_bonus_button"
        )

    async def callback(self, interaction: discord.Interaction):
        if (
            bonus_password is None
            or bonus_date is None
            or bonus_slot is None
            or bonus_points is None
        ):
            return await interaction.response.send_message(
                "❌ 현재 활성화된 가산점이 없습니다.",
                ephemeral=True
            )

        await interaction.response.send_modal(
            BonusPasswordModal()
        )


class StandaloneBonusView(discord.ui.View):
    """일반 출석창에 별도로 표시되는 가산점 전용 패널."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(BonusButton())


class BonusPointButton(discord.ui.Button):
    def __init__(self, points):
        super().__init__(
            label=f"{points}점",
            style=discord.ButtonStyle.primary
        )
        self.points = points

    async def callback(self, interaction: discord.Interaction):
        global bonus_password
        global bonus_date
        global bonus_slot
        global bonus_points
        global bonus_created_at

        if not is_admin_channel(interaction):
            return await interaction.response.send_message(
                "❌ 관리자 전용방에서만 사용할 수 있습니다.",
                ephemeral=True
            )

        # 한 번 생성된 가산점 세션이 아직 1시간 안에 있다면
        # 새 가산점을 덮어쓰지 않도록 한다.
        if (
            bonus_password is not None
            and bonus_created_at is not None
            and datetime.now(KST) < bonus_created_at + timedelta(hours=1)
        ):
            return await interaction.response.send_message(
                f"⚠️ 현재 **+{bonus_points}점 가산점**이 이미 활성화되어 있습니다.\n"
                "기존 가산점이 종료된 후 새 가산점을 생성해주세요.",
                ephemeral=True
            )

        now = datetime.now(KST)
        date_text = now.strftime("%Y-%m-%d")

        # 테스트 모드에서도 명령어를 누른 시각을 세션 키로 사용
        if TEST_MODE:
            slot_text = now.strftime("%H%M%S")
        else:
            slot_text = now.strftime("%H%M")

        password = generate_password()

        try:
            await run_db(
                save_bonus_session,
                date_text,
                slot_text,
                self.points,
                password
            )

            bonus_password = password
            bonus_date = date_text
            bonus_slot = slot_text
            bonus_points = self.points
            bonus_created_at = now

            # 관리자방에는 비밀번호만 생성하고,
            # 일반 출석창에는 출석 패널과 별개인 가산점 버튼을 즉시 전송한다.
            await interaction.response.send_message(
                f"🔵 **가산점 +{self.points}점 생성 완료**\n\n"
                f"🔐 가산점 비밀번호\n"
                f"```{password}```\n\n"
                f"⏰ 유효시간: 1시간",
                ephemeral=False
            )

            attendance_channel = bot.get_channel(ATTENDANCE_CHANNEL_ID)

            if attendance_channel is not None:
                # 기존 출석 패널/메시지는 건드리지 않고
                # 가산점 패널만 새 메시지로 추가한다.
                bonus_message = await attendance_channel.send(
                    "🔵 **가산점 패널**\n"
                    f"⭐ 현재 가산점: **+{self.points}점**\n"
                    "아래 파란색 버튼을 눌러 가산점 비밀번호를 입력하세요.\n"
                    "⏰ 유효시간: 1시간",
                    view=StandaloneBonusView()
                )
                bonus_message_ids.add(bonus_message.id)

        except Exception as e:
            print(
                f"[가산점 생성 오류] "
                f"{type(e).__name__}: {e}"
            )

            await interaction.response.send_message(
                "❌ 가산점 생성 중 오류가 발생했습니다.",
                ephemeral=True
            )


class BonusPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

        for points in range(1, 6):
            self.add_item(BonusPointButton(points))


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
                "❌ 일반 출석창에서만 출석할 수 있습니다.",
                ephemeral=True
            )

        # Discord의 응답 제한시간을 먼저 확보한다.
        # 이후 PostgreSQL 작업이 조금 늦어져도
        # "뭔가 잘못되었어요"가 발생하지 않도록 한다.
        await interaction.response.defer(ephemeral=True)

        try:
            now = datetime.now(KST)

            if current_password is None:
                return await interaction.followup.send(
                    "❌ 현재 출석 가능한 시간이 아닙니다.",
                    ephemeral=True
                )

            expected_date = now.strftime("%Y-%m-%d")

            if current_date != expected_date:
                return await interaction.followup.send(
                    "❌ 현재 출석 세션이 종료되었습니다.",
                    ephemeral=True
                )

            try:
                if TEST_MODE:
                    session_hour = int(current_slot[:2])
                    session_minute = int(current_slot[2:4])
                else:
                    session_hour = int(current_slot)
                    session_minute = 0
            except Exception:
                return await interaction.followup.send(
                    "❌ 출석 세션 정보를 확인할 수 없습니다.",
                    ephemeral=True
                )

            session_start = now.replace(
                hour=session_hour,
                minute=session_minute,
                second=0,
                microsecond=0
            )

            session_duration = (
                timedelta(minutes=1)
                if TEST_MODE
                else timedelta(hours=3)
            )

            if now < session_start or now >= session_start + session_duration:
                return await interaction.followup.send(
                    "❌ 출석 가능 시간이 종료되었습니다.",
                    ephemeral=True
                )

            entered = str(self.password_input.value).strip()

            if entered != current_password:
                return await interaction.followup.send(
                    "❌ 비밀번호가 틀렸습니다.",
                    ephemeral=True
                )

            user_id = interaction.user.id
            username = interaction.user.display_name

            # DB 작업은 별도 스레드에서 실행하여 Discord 이벤트 루프를 막지 않는다.
            already_attended = await run_db(
                has_attendance,
                current_date,
                current_slot,
                user_id
            )

            if already_attended:
                return await interaction.followup.send(
                    "⚠️ 이미 이번 출석에 참여하셨습니다.",
                    ephemeral=True
                )

            inserted = await run_db(
                save_attendance,
                current_date,
                current_slot,
                user_id,
                username
            )

            if not inserted:
                return await interaction.followup.send(
                    "⚠️ 이미 이번 출석에 참여하셨습니다.",
                    ephemeral=True
                )

            await interaction.followup.send(
                f"✅ 출석 완료!\n"
                f"👤 {username}\n"
                f"⭐ 1점이 지급되었습니다.",
                ephemeral=True
            )

        except Exception as e:
            print(f"[출석 처리 오류] {type(e).__name__}: {e}")

            try:
                await interaction.followup.send(
                    "❌ 출석 처리 중 오류가 발생했습니다.\n"
                    "잠시 후 다시 시도해주세요.",
                    ephemeral=True
                )
            except Exception:
                pass



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

        # 여기서는 출석 패널만 생성한다.
        # 가산점은 !가산점 명령어로 생성된 활성 세션이 있을 때만
        # AttendanceView에 파란색 버튼으로 표시된다.
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
        # 출석 패널은 1분마다 automatic_attendance_panel이 교체한다.
        # 가산점은 별도 생성이므로 생성 후 1시간 동안 유지한다.
        global bonus_password
        global bonus_date
        global bonus_slot
        global bonus_points
        global bonus_created_at

        if (
            bonus_password is not None
            and bonus_created_at is not None
            and now >= bonus_created_at + timedelta(hours=1)
        ):
            old_date = bonus_date
            old_slot = bonus_slot

            bonus_password = None
            bonus_date = None
            bonus_slot = None
            bonus_points = None
            bonus_created_at = None

            # 1시간이 지나면 일반 출석창에 별도로 표시했던
            # 가산점 패널도 삭제한다.
            attendance_channel = bot.get_channel(ATTENDANCE_CHANNEL_ID)
            if attendance_channel is not None:
                for message_id in list(bonus_message_ids):
                    try:
                        message = await attendance_channel.fetch_message(message_id)
                        await message.delete()
                    except Exception:
                        pass
                    finally:
                        bonus_message_ids.discard(message_id)

            if old_date and old_slot:
                try:
                    await run_db(
                        delete_bonus_session,
                        old_date,
                        old_slot
                    )
                except Exception as e:
                    print(f"[가산점 DB 정리 오류] {e}")

            print(
                "[가산점 종료] 1시간이 지나 "
                "가산점 세션을 종료했습니다."
            )

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
class DBResetConfirmModal(discord.ui.Modal, title="⚠️ DB 전체 초기화 확인"):
    confirm_input = discord.ui.TextInput(
        label="초기화 동의 문구",
        placeholder="아래 문구를 정확하게 입력하세요: 초기화 동의",
        min_length=6,
        max_length=20,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        global boss_panel_message_id
        if interaction.channel_id != ADMIN_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ 관리자 전용방에서만 초기화할 수 있습니다.",
                ephemeral=True
            )

        entered = str(self.confirm_input.value).strip()

        if entered != "초기화 동의":
            return await interaction.response.send_message(
                "❌ 확인 문구가 일치하지 않습니다.\n"
                "DB 초기화가 취소되었습니다.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        conn = None

        try:
            conn = await run_db(get_db_connection)

            def reset_database(connection):
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "TRUNCATE TABLE attendance_v2 "
                            "RESTART IDENTITY CASCADE"
                        )

                        cursor.execute(
                            "TRUNCATE TABLE attendance_sessions "
                            "RESTART IDENTITY CASCADE"
                        )

                        cursor.execute(
                            "TRUNCATE TABLE bonus_attendance, bonus_sessions "
                            "RESTART IDENTITY CASCADE"
                        )

                        cursor.execute(
                            "TRUNCATE TABLE boss_drops, boss_list "
                            "RESTART IDENTITY CASCADE"
                        )

                        connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    release_db_connection(connection)

            await run_db(reset_database, conn)
            conn = None

            global current_password
            global current_date
            global current_slot
            global last_panel_key
            global bonus_password
            global bonus_date
            global bonus_slot
            global bonus_points
            global bonus_created_at

            current_password = None
            current_date = None
            current_slot = None
            last_panel_key = None

            bonus_password = None
            bonus_date = None
            bonus_slot = None
            bonus_points = None
            bonus_created_at = None
            boss_names.clear()
            boss_panel_message_id = None

            # DB 초기화 시 일반 출석창의 가산점 패널도 제거
            attendance_channel = bot.get_channel(ATTENDANCE_CHANNEL_ID)
            if attendance_channel is not None:
                for message_id in list(bonus_message_ids):
                    try:
                        message = await attendance_channel.fetch_message(message_id)
                        await message.delete()
                    except Exception:
                        pass
                    finally:
                        bonus_message_ids.discard(message_id)

            await interaction.followup.send(
                "🧹 **DB 전체 초기화 완료**\n\n"
                "• 모든 출석 기록 삭제\n"
                "• 출석 비밀번호 세션 초기화\n"
                "• 현재 출석 세션 초기화 완료\n\n"
                "이제 Discord ID 기준으로 출석 기록을 새로 저장합니다.",
                ephemeral=True
            )

        except Exception as e:
            if conn is not None:
                try:
                    await run_db(release_db_connection, conn)
                except Exception:
                    pass

            print(f"[DB 초기화 오류] {type(e).__name__}: {e}")

            await interaction.followup.send(
                f"❌ **DB 초기화 실패**\n```{e}```",
                ephemeral=True
            )


@bot.command(name="가산점")
async def bonus_command(ctx):
    # 가산점은 이 명령어를 입력했을 때만 생성/표시된다.
    # 자동 출석 루프에서는 가산점 세션을 절대 생성하지 않는다.
    if not is_admin_channel(ctx):
        return await ctx.send(
            "❌ 이 명령어는 관리자 전용방에서만 사용할 수 있습니다."
        )

    await ctx.send(
        "🔵 **가산점 패널**\n\n"
        "부여할 가산점을 선택하세요.\n"
        "아래 1~5점 중 하나를 눌러야 가산점이 생성됩니다.\n\n"
        "⚠️ 가산점은 이 명령어를 실행하기 전에는 "
        "일반 출석창에 나타나지 않습니다.",
        view=BonusPanelView()
    )


@bot.command(name="보스추가")
async def boss_add(ctx, *, boss_name: str = ""):
    if not is_admin_channel(ctx):
        return await ctx.send("❌ 관리자 전용방에서만 사용할 수 있습니다.")
    name = boss_name.strip()
    if not name:
        return await ctx.send("❌ 사용법: `!보스추가 이프리트`")
    inserted = await run_db(add_boss_db, name)
    if not inserted:
        return await ctx.send(f"⚠️ **{name}**은 이미 등록되어 있습니다.")
    boss_names.append(name)
    await ctx.send(f"🔴 **{name}** 보스가 추가되었습니다.")
    await send_boss_panel()


@bot.command(name="보스삭제")
async def boss_delete(ctx, *, boss_name: str = ""):
    if not is_admin_channel(ctx):
        return await ctx.send("❌ 관리자 전용방에서만 사용할 수 있습니다.")
    name = boss_name.strip()
    if not name:
        return await ctx.send("❌ 사용법: `!보스삭제 이프리트`")
    deleted = await run_db(delete_boss_db, name)
    if not deleted:
        return await ctx.send(f"❌ **{name}** 보스를 찾을 수 없습니다.")
    boss_names[:] = [x for x in boss_names if x != name]
    await ctx.send(f"🗑️ **{name}** 보스가 삭제되었습니다.")
    await send_boss_panel()


@bot.command(name="DB초기화")
async def db_reset(ctx):
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
        "• 출석 비밀번호 세션\n"
        "• 가산점 기록 및 세션\n\n"
        "⚠️ **초기화 후에는 데이터를 복구할 수 없습니다.**\n\n"
        "아래 **확인 버튼**을 눌러 초기화 확인창을 열어주세요.",
        view=DBResetConfirmView()
    )


class DBResetConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(
        label="⚠️ 초기화 진행",
        style=discord.ButtonStyle.danger,
        custom_id="raid_db_reset_confirm_button"
    )
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if interaction.channel_id != ADMIN_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ 관리자 전용방에서만 초기화할 수 있습니다.",
                ephemeral=True
            )

        await interaction.response.send_modal(DBResetConfirmModal())





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
    try:
        boss_names[:] = await run_db(load_bosses)
    except Exception as e:
        print(f"[보스 목록 로드 오류] {e}")
        boss_names.clear()


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
    print("가산점 기능: !가산점 / 1~5점 / 유효시간 1시간")


# =====================================================
# 🔹 실행
# =====================================================
keep_alive()

bot.run(
    os.getenv("DISCORD_TOKEN")
)