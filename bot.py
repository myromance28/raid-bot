# -*- coding: utf-8 -*-
# =====================================================
# RAID BOT - 출석 운영 안정화 버전
# =====================================================

import os
import asyncio
import random
import string
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
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
# 테스트 모드: 2분마다 새로운 출석 세션 생성
# 실제 운영 전환 시 TEST_MODE = False
TEST_MODE = False
TEST_INTERVAL_MINUTES = 2  # 테스트 모드가 꺼져 있으므로 운영에는 사용되지 않음

# 동시 출석 처리 / DB 연결 보호
# 150명 이상이 동시에 버튼을 눌러도 DB 연결을 무제한으로 만들지 않습니다.
DB_POOL_MIN = 1
DB_POOL_MAX = 10
DB_CONCURRENCY = 10

# Google Sheets 외부 요청 보호
# 출석 순간 150개의 HTTP POST가 동시에 나가지 않도록 1건씩 순차 전송합니다.
GOOGLE_SHEETS_MIN_INTERVAL = 1.0
GOOGLE_SHEETS_IDLE_POLL = 5.0

# 실제 운영 시간: 03:00 / 09:00 / 15:00 / 21:00 (각 6시간)
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
# 🔹 Google Sheets 연동
# =====================================================
# Render 환경변수
# GOOGLE_SHEETS_URL    = Apps Script 웹 앱 /exec 주소
# GOOGLE_SHEETS_SECRET = Apps Script의 SECRET_KEY와 동일한 값
GOOGLE_SHEETS_URL = os.getenv("GOOGLE_SHEETS_URL", "").strip()
GOOGLE_SHEETS_SECRET = os.getenv("GOOGLE_SHEETS_SECRET", "").strip()


def send_to_google_sheet(
    date_text,
    time_text,
    record_type,
    user_id,
    username,
    points,
    item_name=None,
    boss_name=None
):
    """
    Google Sheets로 1건을 전송합니다.
    득템 기록인 경우 item_name / boss_name도 함께 전송합니다.
    반환값: (성공여부, retry_after_seconds, error_text)
    """
    if not GOOGLE_SHEETS_URL or not GOOGLE_SHEETS_SECRET:
        return True, None, ""

    payload = {
        "secret": GOOGLE_SHEETS_SECRET,
        "date": str(date_text),
        "time": str(time_text),
        "type": str(record_type),
        "user_id": str(user_id),
        "username": str(username),
        "points": int(points),
        "item_name": str(item_name or ""),
        "boss_name": str(boss_name or ""),
    }

    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            GOOGLE_SHEETS_URL,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=15) as response:
            response_text = response.read().decode("utf-8", errors="replace")

        try:
            result = json.loads(response_text)
        except Exception:
            result = {}

        if result.get("success") is False:
            error_text = f"Apps Script 실패: {response_text[:500]}"
            print(f"[Google Sheets] {error_text}")
            return False, None, error_text

        print(
            f"[Google Sheets] 기록 성공: "
            f"{record_type} / {username} / +{points}점"
        )
        return True, None, ""

    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(e)

        retry_after = None
        try:
            retry_after = int(e.headers.get("Retry-After")) if e.headers else None
        except Exception:
            retry_after = None

        # Cloudflare 1015는 rate limit 응답입니다.
        # Retry-After가 없더라도 안전하게 최소 30초 대기합니다.
        if e.code == 1015:
            retry_after = max(retry_after or 30, 30)

        error_text = f"HTTP {e.code}: {detail[:500]}"
        print(f"[Google Sheets] {error_text}")
        return False, retry_after, error_text

    except urllib.error.URLError as e:
        error_text = f"연결 오류: {e}"
        print(f"[Google Sheets] {error_text}")
        return False, None, error_text

    except Exception as e:
        error_text = f"{type(e).__name__}: {e}"
        print(f"[Google Sheets] 전송 오류: {error_text}")
        return False, None, error_text


# =====================================================
# 🔹 PostgreSQL 연결 풀
# =====================================================
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise Exception("DATABASE_URL 환경변수 없음")

DB_POOL = ThreadedConnectionPool(
    DB_POOL_MIN,
    DB_POOL_MAX,
    DATABASE_URL,
    sslmode="require"
)

# DB 작업 동시 실행 수도 풀 크기와 맞춰 제한합니다.
DB_SEMAPHORE = asyncio.Semaphore(DB_CONCURRENCY)


def get_db_connection():
    """PostgreSQL 연결 풀에서 연결을 하나 빌립니다."""
    return DB_POOL.getconn()


def release_db_connection(conn):
    """사용한 연결을 닫지 않고 풀에 반환합니다."""
    if conn is None:
        return
    try:
        # 이전 작업이 예외로 종료되어 aborted transaction 상태가 남아도
        # 다음 사용자가 영향을 받지 않도록 반드시 rollback합니다.
        if not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass
            DB_POOL.putconn(conn)
    except Exception:
        try:
            if not conn.closed:
                conn.close()
        except Exception:
            pass


async def run_db(func, *args):
    """
    동기 psycopg2 작업을 별도 스레드에서 실행하되,
    동시에 너무 많은 DB 작업이 몰리지 않도록 제한합니다.
    """
    async with DB_SEMAPHORE:
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


def get_test_session_start(now=None):
    """테스트 모드에서 2분 단위로 고정된 세션 시작 시각을 반환합니다."""
    if now is None:
        now = datetime.now(KST)

    bucket_minute = (now.minute // TEST_INTERVAL_MINUTES) * TEST_INTERVAL_MINUTES

    return now.replace(
        minute=bucket_minute,
        second=0,
        microsecond=0
    )


def get_test_session_slot(now=None):
    return get_test_session_start(now).strftime("%H%M")


def get_current_session():
    now = datetime.now(KST)
    return (
        now.strftime("%Y-%m-%d"),
        get_current_slot(now.hour)
    )


def get_active_attendance_session(now=None):
    """현재 KST가 어느 출석 시간대(03/09/15/21)에 속하는지 반환합니다.

    출석 가능 시간:
      03시 세션: 03:00~09:00
      09시 세션: 09:00~15:00
      15시 세션: 15:00~21:00
      21시 세션: 21:00~03:00 (다음날)

    자정 이후 00:00~02:59에는 '오늘 21시'가 아니라
    '전날 21시' 세션을 찾아야 하므로 전날 날짜도 함께 검사합니다.

    반환값: (세션 시작일자 YYYY-MM-DD, slot_text, session_start) 또는 None
    """
    if now is None:
        now = datetime.now(KST)

    # 오늘과 전날의 모든 세션 시작시각을 후보로 만듭니다.
    candidate_starts = []

    for day_offset in (0, -1):
        base_date = (now + timedelta(days=day_offset)).date()
        for start_hour in ATTENDANCE_HOURS:
            session_start = datetime(
                base_date.year,
                base_date.month,
                base_date.day,
                start_hour,
                0,
                0,
                tzinfo=KST
            )
            session_end = session_start + timedelta(hours=6)

            if session_start <= now < session_end:
                candidate_starts.append((session_start, f"{start_hour:02d}"))

    if not candidate_starts:
        return None

    # 겹치는 후보가 있을 경우 가장 최근에 시작한 세션을 사용합니다.
    session_start, slot_text = max(
        candidate_starts,
        key=lambda item: item[0]
    )

    session_date = session_start.strftime("%Y-%m-%d")
    return session_date, slot_text, session_start


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

            # 패널이 정각에 생성되지 못한 경우에도 재시작 후
            # 현재 시간대의 세션이 살아있는지 판단할 수 있도록 상태를 DB에 저장합니다.
            cursor.execute("""
                ALTER TABLE attendance_sessions
                ADD COLUMN IF NOT EXISTS panel_sent BOOLEAN NOT NULL DEFAULT FALSE
            """)

            # 가산점 패널 메시지 ID도 DB에 보관하여 봇 재시작 후 정리할 수 있게 합니다.
            cursor.execute("""
                ALTER TABLE bonus_sessions
                ADD COLUMN IF NOT EXISTS message_id BIGINT
            """)

            # Google Sheets 외부 API 요청은 출석 버튼을 누르는 순간 바로 보내지 않고
            # 이 큐에 저장한 뒤 순차 전송합니다. 따라서 150명이 동시에 눌러도
            # 외부 HTTP POST가 한꺼번에 폭발하지 않습니다.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS google_sheet_queue (
                    id BIGSERIAL PRIMARY KEY,
                    date TEXT NOT NULL,
                    time TEXT NOT NULL,
                    record_type TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT NOT NULL,
                    points INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_error TEXT,
                    sent_at TIMESTAMP NULL,
                    UNIQUE(date, time, record_type, user_id)
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS google_sheet_queue_pending_idx
                ON google_sheet_queue(sent_at, next_attempt_at, id)
            """)

            cursor.execute("""
                ALTER TABLE google_sheet_queue
                ADD COLUMN IF NOT EXISTS item_name TEXT
            """)
            cursor.execute("""
                ALTER TABLE google_sheet_queue
                ADD COLUMN IF NOT EXISTS boss_name TEXT
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
                # 테스트 모드의 세션은 HHMM 형식 (2분 단위, 예: 2238)
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
            timedelta(minutes=TEST_INTERVAL_MINUTES)
            if TEST_MODE
            else timedelta(hours=6)
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
    """기존 호환용 조회 함수. 실제 출석 처리는 원자적 INSERT를 사용합니다."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT 1
                FROM attendance_v2
                WHERE date=%s AND time_slot=%s AND user_id=%s
                LIMIT 1
            """, (date, slot, user_id))
            return cursor.fetchone() is not None
    finally:
        release_db_connection(conn)


def enqueue_google_sheet_record(
    cursor,
    date_text,
    time_text,
    record_type,
    user_id,
    username,
    points,
    item_name=None,
    boss_name=None
):
    """현재 DB 트랜잭션 안에서 Google Sheets 동기화 대상을 큐에 넣습니다."""
    if not GOOGLE_SHEETS_URL or not GOOGLE_SHEETS_SECRET:
        return False

    cursor.execute("""
        INSERT INTO google_sheet_queue
            (date, time, record_type, user_id, username, points, item_name, boss_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (date, time, record_type, user_id) DO NOTHING
    """, (
        str(date_text), str(time_text), str(record_type),
        int(user_id), str(username), int(points),
        str(item_name or ""), str(boss_name or "")
    ))
    return cursor.rowcount > 0


def save_attendance(date, slot, user_id, username):
    """
    출석 중복 확인 SELECT를 별도로 하지 않고 INSERT 한 번으로 처리합니다.
    150명이 동시에 눌러도 PostgreSQL UNIQUE 제약이 최종 중복 방지를 담당합니다.
    """
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO attendance_v2
                    (date, time_slot, user_id, username, points)
                VALUES (%s, %s, %s, %s, 1)
                ON CONFLICT (date, time_slot, user_id)
                DO NOTHING
                RETURNING id
            """, (date, slot, user_id, username))

            row = cursor.fetchone()
            inserted = row is not None

            if inserted:
                # Discord 응답 시간과 무관하게 Sheets는 별도 워커가 처리합니다.
                enqueue_google_sheet_record(
                    cursor,
                    date,
                    datetime.now(KST).strftime("%H:%M:%S"),
                    "출석",
                    user_id,
                    username,
                    1
                )

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
def load_bonus_session(date_text, slot_text):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, date, time_slot, points, password, created_at, message_id
                FROM bonus_sessions
                WHERE date=%s AND time_slot=%s
                LIMIT 1
            """, (date_text, slot_text))
            return cursor.fetchone()
    finally:
        release_db_connection(conn)


def save_bonus_session(date_text, slot_text, points, password):
    """
    같은 시간대 가산점은 최초 생성 1건만 인정합니다.
    동시에 두 관리자가 눌러도 UPDATE로 덮어쓰지 않습니다.
    반환값: (새로 생성했는지, 세션 row)
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO bonus_sessions
                    (date, time_slot, points, password)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (date, time_slot) DO NOTHING
                RETURNING id, date, time_slot, points, password, created_at, message_id
            """, (date_text, slot_text, points, password))

            row = cursor.fetchone()
            inserted = row is not None

            if row is None:
                cursor.execute("""
                    SELECT id, date, time_slot, points, password, created_at, message_id
                    FROM bonus_sessions
                    WHERE date=%s AND time_slot=%s
                    LIMIT 1
                """, (date_text, slot_text))
                row = cursor.fetchone()

            conn.commit()
            return inserted, row

    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def update_bonus_message_id(date_text, slot_text, message_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE bonus_sessions
                SET message_id=%s
                WHERE date=%s AND time_slot=%s
            """, (int(message_id), date_text, slot_text))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def has_bonus_attendance(date_text, slot_text, user_id):
    """기존 호환용 조회 함수. 실제 지급 처리는 원자적 INSERT를 사용합니다."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT 1
                FROM bonus_attendance
                WHERE date=%s AND time_slot=%s AND user_id=%s
                LIMIT 1
            """, (date_text, slot_text, user_id))
            return cursor.fetchone() is not None
    finally:
        release_db_connection(conn)


def save_bonus_attendance(date_text, slot_text, points, user_id, username, time_text=None):
    """가산점도 SELECT 후 INSERT가 아니라 원자적 INSERT 1회로 처리합니다."""
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO bonus_attendance
                    (date, time_slot, points, user_id, username)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (date, time_slot, user_id)
                DO NOTHING
                RETURNING id
            """, (date_text, slot_text, points, user_id, username))

            row = cursor.fetchone()
            inserted = row is not None

            if inserted:
                enqueue_google_sheet_record(
                    cursor,
                    date_text,
                    time_text or datetime.now(KST).strftime("%H:%M:%S"),
                    "가산점",
                    user_id,
                    username,
                    points
                )

            conn.commit()
            return inserted

    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)

def get_next_google_sheet_job():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, date, time, record_type, user_id, username,
                       points, attempts, item_name, boss_name
                FROM google_sheet_queue
                WHERE sent_at IS NULL
                  AND next_attempt_at <= CURRENT_TIMESTAMP
                ORDER BY id ASC
                LIMIT 1
            """)
            return cursor.fetchone()
    finally:
        release_db_connection(conn)


def mark_google_sheet_success(queue_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE google_sheet_queue
                SET sent_at=CURRENT_TIMESTAMP, last_error=NULL
                WHERE id=%s AND sent_at IS NULL
            """, (queue_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def mark_google_sheet_failure(queue_id, error_text, retry_seconds):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE google_sheet_queue
                SET attempts=attempts+1,
                    next_attempt_at=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                    last_error=%s
                WHERE id=%s AND sent_at IS NULL
            """, (int(max(1, retry_seconds)), str(error_text)[:1000], queue_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


async def google_sheet_sync_worker():
    """
    Google Sheets 동기화 전용 워커.
    - 출석 처리 중 외부 HTTP 요청을 하지 않음
    - 한 번에 1건만 전송
    - 기본 1초 간격으로 burst를 제거
    - 1015/429 등의 rate limit은 Retry-After/지수 백오프로 재시도
    - 큐가 DB에 저장되어 Render 재시작 후에도 이어서 처리
    """
    while True:
        try:
            if not GOOGLE_SHEETS_URL or not GOOGLE_SHEETS_SECRET:
                await asyncio.sleep(30)
                continue

            job = await run_db(get_next_google_sheet_job)

            if not job:
                await asyncio.sleep(GOOGLE_SHEETS_IDLE_POLL)
                continue

            (
                queue_id,
                date_text,
                time_text,
                record_type,
                user_id,
                username,
                points,
                attempts,
                item_name,
                boss_name,
            ) = job

            success, retry_after, error_text = await asyncio.to_thread(
                send_to_google_sheet,
                date_text,
                time_text,
                record_type,
                user_id,
                username,
                points,
                item_name,
                boss_name
            )

            if success:
                await run_db(mark_google_sheet_success, queue_id)
                await asyncio.sleep(GOOGLE_SHEETS_MIN_INTERVAL)
                continue

            # 실패 시 기본 지수 백오프. 1015/429에서 Retry-After가 있으면 우선 사용합니다.
            exponential = min(300, 30 * (2 ** min(int(attempts), 4)))
            wait_seconds = max(int(retry_after or 0), exponential)
            await run_db(
                mark_google_sheet_failure,
                queue_id,
                error_text,
                wait_seconds
            )
            print(
                f"[Google Sheets 재시도 예약] queue={queue_id} "
                f"attempt={int(attempts)+1} wait={wait_seconds}s"
            )
            await asyncio.sleep(min(wait_seconds, 30))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[Google Sheets 워커 오류] {type(e).__name__}: {e}")
            await asyncio.sleep(5)


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
            cursor.execute("SELECT boss_name FROM boss_list ORDER BY boss_name ASC")
            return [row[0] for row in cursor.fetchall()]
    finally:
        release_db_connection(conn)


def add_boss_db(name):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO boss_list (boss_name) VALUES (%s) "
                "ON CONFLICT (boss_name) DO NOTHING",
                (name,)
            )
            # RETURNING을 사용하지 않으므로 fetchone()을 호출하지 않는다.
            ok = cursor.rowcount > 0
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

            # 득템도 기존 출석과 동일하게 DB 큐를 거쳐 Google Sheets로 전송합니다.
            # 마이크로초까지 포함해 같은 초에 여러 득템을 등록해도 구분됩니다.
            now = datetime.now(KST)
            enqueue_google_sheet_record(
                cursor,
                now.strftime("%Y-%m-%d"),
                now.strftime("%H:%M:%S.%f"),
                "득템",
                user_id,
                username,
                0,
                item_name=drop_name,
                boss_name=boss_name
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


async def send_boss_panel():
    global boss_panel_message_id

    # 출석 비밀번호가 올라오는 관리자 채팅방에 표시
    admin_channel = bot.get_channel(ADMIN_CHANNEL_ID)
    if admin_channel is None:
        print(f"[보스 패널 오류] 관리자 채널을 찾을 수 없습니다: {ADMIN_CHANNEL_ID}")
        return

    # DB에 현재 등록된 전체 보스 목록을 다시 읽는다.
    # 메모리 목록이 누락되었더라도 관리자방에는 DB 기준 전체 보스가 표시된다.
    try:
        current_bosses = await run_db(load_bosses)
        boss_names[:] = current_bosses
    except Exception as e:
        print(f"[보스 목록 로드 오류] {type(e).__name__}: {e}")
        current_bosses = list(boss_names)

    # 기존 보스 패널 메시지가 있으면 갱신을 위해 삭제
    if boss_panel_message_id:
        try:
            old = await admin_channel.fetch_message(boss_panel_message_id)
            await old.delete()
        except Exception:
            pass
        boss_panel_message_id = None

    # 등록된 보스가 하나도 없으면 패널을 만들지 않는다.
    if not current_bosses:
        return

    # 현재 등록된 '전체' 보스를 빨간 버튼으로 생성
    view = BossPanelView()

    msg = await admin_channel.send(
        "👹 **보스 목록**\n"
        "현재 등록된 보스입니다.\n"
        "보스를 처치한 후 해당 보스의 빨간 버튼을 눌러 득템 이름을 입력하세요.",
        view=view
    )
    boss_panel_message_id = msg.id




class DropDeleteConfirmView(discord.ui.View):
    def __init__(self, drop_id, drop_text):
        super().__init__(timeout=60)
        self.drop_id = drop_id
        self.drop_text = drop_text

    @discord.ui.button(
        label="삭제",
        style=discord.ButtonStyle.success
    )
    async def delete_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if not is_admin_channel(interaction):
            return await interaction.response.send_message(
                "❌ 관리자 전용방에서만 사용할 수 있습니다.",
                ephemeral=True
            )

        try:
            deleted = await run_db(delete_drop_db, self.drop_id)

            if deleted:
                button.disabled = True
                for child in self.children:
                    child.disabled = True

                await interaction.response.edit_message(
                    content=(
                        "🗑️ **삭제 완료**\n\n"
                        f"`{self.drop_text}`"
                    ),
                    view=self
                )
            else:
                await interaction.response.send_message(
                    "❌ 해당 득템 내역을 찾을 수 없습니다.",
                    ephemeral=True
                )

        except Exception as e:
            print(f"[득템 삭제 오류] {type(e).__name__}: {e}")
            await interaction.response.send_message(
                f"❌ 득템 삭제 중 오류가 발생했습니다.\n```{e}```",
                ephemeral=True
            )

    @discord.ui.button(
        label="취소",
        style=discord.ButtonStyle.danger
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content="↩️ **삭제가 취소되었습니다.**",
            view=self
        )


class DropDeleteSelect(discord.ui.Select):
    def __init__(self, drops):
        self.drops = drops

        options = []
        for index, row in enumerate(drops[:25], start=1):
            drop_id, boss_name, drop_name, username, created_at = row
            # !득템 목록에서 보이는 순번과 동일하게 표시합니다.
            label = f"{index}️⃣ {drop_name} - {boss_name}"[:100]
            desc = (
                f"{username} / "
                f"{created_at.strftime('%m-%d %H:%M') if created_at else ''}"
            )[:100]

            options.append(
                discord.SelectOption(
                    label=label,
                    description=desc,
                    value=str(drop_id)
                )
            )

        super().__init__(
            placeholder="삭제할 득템 내역을 선택하세요.",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if not is_admin_channel(interaction):
            return await interaction.response.send_message(
                "❌ 관리자 전용방에서만 사용할 수 있습니다.",
                ephemeral=True
            )

        drop_id = int(self.values[0])

        selected = next(
            (row for row in self.drops if row[0] == drop_id),
            None
        )

        if selected is None:
            return await interaction.response.send_message(
                "❌ 선택한 득템 내역을 찾을 수 없습니다.",
                ephemeral=True
            )

        _, boss_name, drop_name, username, created_at = selected
        time_text = (
            created_at.strftime("%m-%d %H:%M")
            if created_at else "-"
        )

        drop_text = (
            f"{drop_name} - {boss_name} - "
            f"{username} - {time_text}"
        )

        await interaction.response.edit_message(
            content=(
                "⚠️ **정말 삭제하시겠습니까?**\n\n"
                f"`{drop_text}`"
            ),
            view=DropDeleteConfirmView(drop_id, drop_text)
        )




class DropListView(discord.ui.View):
    def __init__(self, drops):
        super().__init__(timeout=300)
        if drops:
            self.add_item(DropDeleteSelect(drops))


def load_all_drops():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, boss_name, drop_name, username, created_at
                FROM boss_drops
                ORDER BY created_at ASC, id ASC
            """)
            return cursor.fetchall()
    finally:
        release_db_connection(conn)


def delete_drop_db(drop_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM boss_drops WHERE id=%s",
                (drop_id,)
            )
            deleted = cursor.rowcount > 0
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def reset_all_drops_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM boss_drops")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


class DropResetConfirmModal(discord.ui.Modal, title="⚠️ 득템 전체 초기화"):
    confirm_text = discord.ui.TextInput(
        label='확인 문구: "동의합니다"',
        placeholder="동의합니다",
        required=True,
        min_length=5,
        max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin_channel(interaction):
            return await interaction.response.send_message(
                "❌ 관리자 전용방에서만 사용할 수 있습니다.",
                ephemeral=True
            )

        if str(self.confirm_text.value).strip() != "동의합니다":
            return await interaction.response.send_message(
                '❌ 확인 문구가 일치하지 않습니다.\n'
                '정확히 **동의합니다**라고 입력해야 초기화됩니다.',
                ephemeral=True
            )

        try:
            await run_db(reset_all_drops_db)
            await interaction.response.send_message(
                "✅ **득템 목록 전체 초기화 완료**\n"
                "등록되어 있던 모든 득템 내역이 삭제되었습니다.",
                ephemeral=True
            )
        except Exception as e:
            print(f"[득템 전체 초기화 오류] {type(e).__name__}: {e}")
            await interaction.response.send_message(
                f"❌ 득템 초기화 중 오류가 발생했습니다.\n```{e}```",
                ephemeral=True
            )


class DropResetView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(
        label="⚠️ 득템 전체 초기화",
        style=discord.ButtonStyle.danger
    )
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if not is_admin_channel(interaction):
            return await interaction.response.send_message(
                "❌ 관리자 전용방에서만 사용할 수 있습니다.",
                ephemeral=True
            )
        await interaction.response.send_modal(DropResetConfirmModal())


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

    def __init__(self, date_text, slot_text):
        super().__init__()
        self.date_text = date_text
        self.slot_text = slot_text

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.channel_id != ATTENDANCE_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ 일반 혈맹 출석창에서만 사용할 수 있습니다.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            # 비밀번호/점수/생성시각을 메모리가 아니라 DB에서 다시 읽습니다.
            # Render 재시작 후에도 기존 가산점 패널이 계속 작동할 수 있습니다.
            bonus_session = await run_db(
                load_bonus_session,
                self.date_text,
                self.slot_text
            )

            if not bonus_session:
                return await interaction.followup.send(
                    "❌ 현재 활성화된 가산점이 없습니다.",
                    ephemeral=True
                )

            _, date_text, slot_text, bonus_points, stored_password, created_at, _ = bonus_session
            now = datetime.now(KST)

            if date_text != now.strftime("%Y-%m-%d"):
                return await interaction.followup.send(
                    "❌ 현재 출석 시간대의 가산점이 아닙니다.",
                    ephemeral=True
                )

            # DB timestamp는 timezone 정보가 없는 경우가 있으므로 KST 기준으로 처리합니다.
            if created_at is not None:
                if created_at.tzinfo is None:
                    created_at_kst = created_at.replace(tzinfo=KST)
                else:
                    created_at_kst = created_at.astimezone(KST)

                if now >= created_at_kst + timedelta(hours=6):
                    return await interaction.followup.send(
                        "❌ 현재 출석 시간대의 가산점 유효시간이 종료되었습니다.",
                        ephemeral=True
                    )

            entered = str(self.password_input.value).strip()

            if entered != str(stored_password):
                return await interaction.followup.send(
                    "❌ 가산점 비밀번호가 틀렸습니다.",
                    ephemeral=True
                )

            user_id = interaction.user.id
            username = interaction.user.display_name
            now_text = now.strftime("%H:%M:%S")

            inserted = await run_db(
                save_bonus_attendance,
                date_text,
                slot_text,
                int(bonus_points),
                user_id,
                username,
                now_text
            )

            if not inserted:
                return await interaction.followup.send(
                    "⚠️ 이미 이번 가산점을 받으셨습니다.",
                    ephemeral=True
                )

            await interaction.followup.send(
                f"🔵 **가산점 +{int(bonus_points)}점 지급 완료!**\n"
                f"👤 {username}\n"
                f"⭐ +{int(bonus_points)}점",
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
        active_session = get_active_attendance_session()

        if active_session is None:
            return await interaction.response.send_message(
                "❌ 현재는 출석 시간대가 아닙니다.",
                ephemeral=True
            )

        date_text, slot_text, _ = active_session
        bonus_session = await run_db(load_bonus_session, date_text, slot_text)

        if not bonus_session:
            return await interaction.response.send_message(
                "❌ 현재 활성화된 가산점이 없습니다.",
                ephemeral=True
            )

        await interaction.response.send_modal(
            BonusPasswordModal(date_text, slot_text)
        )


class StandaloneBonusView(discord.ui.View):
    """일반 출석창에 별도로 표시되는 가산점 전용 Persistent View."""

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

        now = datetime.now(KST)
        active_session = get_active_attendance_session(now)

        if active_session is None:
            return await interaction.response.send_message(
                "⚠️ 현재는 출석 시간대가 아닙니다.\n"
                "출석 시간은 **03:00 / 09:00 / 15:00 / 21:00**입니다.",
                ephemeral=True
            )

        date_text, slot_text, session_start = active_session

        password = generate_password()

        try:
            inserted, bonus_session = await run_db(
                save_bonus_session,
                date_text,
                slot_text,
                self.points,
                password
            )

            if not inserted:
                existing_points = int(bonus_session[3]) if bonus_session else 0
                return await interaction.response.send_message(
                    f"⚠️ **{slot_text}시 시간대 가산점**이 이미 생성되어 있습니다.\n"
                    f"현재 가산점: **+{existing_points}점**\n"
                    "같은 시간대에는 가산점을 다시 생성할 수 없습니다.",
                    ephemeral=True
                )

            # 메모리 변수는 화면 표시용 캐시일 뿐, 실제 인증 기준은 DB입니다.
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
                f"⏰ 유효시간: 3시간",
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
                    "⏰ 유효시간: 3시간",
                    view=StandaloneBonusView()
                )
                bonus_message_ids.add(bonus_message.id)
                await run_db(update_bonus_message_id, date_text, slot_text, bonus_message.id)

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
        if interaction.channel_id != ATTENDANCE_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ 일반 출석창에서만 출석할 수 있습니다.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            now = datetime.now(KST)
            active_session = get_active_attendance_session(now)

            if active_session is None:
                return await interaction.followup.send(
                    "❌ 현재 출석 가능한 시간이 아닙니다.",
                    ephemeral=True
                )

            date_text, slot_text, session_start = active_session

            def load_session_for_attendance():
                conn = get_db_connection()
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("""
                            SELECT date, time_slot, password
                            FROM attendance_sessions
                            WHERE date=%s AND time_slot=%s
                            LIMIT 1
                        """, (date_text, slot_text))
                        return cursor.fetchone()
                finally:
                    release_db_connection(conn)

            session = await run_db(load_session_for_attendance)

            if not session:
                return await interaction.followup.send(
                    "❌ 현재 출석 세션이 아직 생성되지 않았습니다.\n"
                    "잠시 후 다시 시도해주세요.",
                    ephemeral=True
                )

            stored_date, stored_slot, stored_password = session

            if stored_date != date_text or str(stored_slot) != str(slot_text):
                return await interaction.followup.send(
                    "❌ 현재 출석 세션이 아닙니다.",
                    ephemeral=True
                )

            if now >= session_start + timedelta(hours=6):
                return await interaction.followup.send(
                    "❌ 출석 가능 시간이 종료되었습니다.",
                    ephemeral=True
                )

            entered = str(self.password_input.value).strip()
            if entered != str(stored_password):
                return await interaction.followup.send(
                    "❌ 비밀번호가 틀렸습니다.",
                    ephemeral=True
                )

            inserted = await run_db(
                save_attendance,
                date_text,
                slot_text,
                interaction.user.id,
                interaction.user.display_name
            )

            if not inserted:
                return await interaction.followup.send(
                    "⚠️ 이미 이번 출석에 참여하셨습니다.",
                    ephemeral=True
                )

            await interaction.followup.send(
                f"✅ 출석 완료!\n"
                f"👤 {interaction.user.display_name}\n"
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
@tasks.loop(seconds=10)
async def automatic_attendance_panel():
    """
    현재 출석 시간대의 세션이 없으면 생성하고,
    정각을 놓쳐도 유효 시간 안에서 패널을 보장합니다.
    """
    global current_password
    global current_date
    global current_slot
    global last_panel_key

    now = datetime.now(KST)

    if TEST_MODE:
        date_text = now.strftime("%Y-%m-%d")
        slot_text = get_test_session_slot(now)
        session_start = get_test_session_start(now)
        session_end = session_start + timedelta(minutes=TEST_INTERVAL_MINUTES)
    else:
        active_session = get_active_attendance_session(now)
        if active_session is None:
            return
        date_text, slot_text, session_start = active_session
        session_end = session_start + timedelta(hours=6)

    # DB에서 해당 시간대 세션을 먼저 확인합니다.
    def load_session_with_panel():
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT date, time_slot, password, panel_sent
                    FROM attendance_sessions
                    WHERE date=%s AND time_slot=%s
                    LIMIT 1
                """, (date_text, slot_text))
                return cursor.fetchone()
        finally:
            release_db_connection(conn)

    session = await run_db(load_session_with_panel)

    if session:
        current_date, current_slot, current_password, panel_sent = session
    else:
        password = generate_password()

        def create_session_if_missing():
            conn = get_db_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO attendance_sessions
                            (date, time_slot, password, panel_sent)
                        VALUES (%s, %s, %s, FALSE)
                        ON CONFLICT (date, time_slot) DO NOTHING
                        RETURNING date, time_slot, password, panel_sent
                    """, (date_text, slot_text, password))
                    row = cursor.fetchone()

                    if row is None:
                        cursor.execute("""
                            SELECT date, time_slot, password, panel_sent
                            FROM attendance_sessions
                            WHERE date=%s AND time_slot=%s
                            LIMIT 1
                        """, (date_text, slot_text))
                        row = cursor.fetchone()

                    conn.commit()
                    return row
            except Exception:
                conn.rollback()
                raise
            finally:
                release_db_connection(conn)

        session = await run_db(create_session_if_missing)
        if not session:
            return
        current_date, current_slot, current_password, panel_sent = session

    # 현재 세션은 DB가 진실의 원천입니다.
    panel_key = f"{date_text}_{slot_text}"
    if panel_sent or last_panel_key == panel_key:
        return

    attendance_channel = bot.get_channel(ATTENDANCE_CHANNEL_ID)
    admin_channel = bot.get_channel(ADMIN_CHANNEL_ID)

    if not attendance_channel or not admin_channel:
        print("[자동 패널 실패] 출석/관리자 채널을 찾을 수 없습니다.")
        return

    validity_text = f"{TEST_INTERVAL_MINUTES}분" if TEST_MODE else "3시간"

    try:
        await attendance_channel.send(
            f"📢 **출석 시간입니다!**\n"
            f"🕒 {now.strftime('%H:%M:%S')} 출석\n\n"
            "아래 버튼을 눌러 출석해주세요.\n"
            "출석 시 **1점**이 지급됩니다.\n"
            f"⏰ 유효시간: {validity_text}",
            view=AttendanceView()
        )

        await admin_channel.send(
            f"🔐 **출석 비밀번호**\n\n"
            f"```{current_password}```\n\n"
            f"🕒 출석 시간: {now.strftime('%H:%M:%S')}\n"
            f"⏰ 유효시간: {validity_text}\n"
            "⚠️ 이 번호는 혈맹원에게 알려주세요."
        )

        await send_boss_panel()

        def mark_panel_sent():
            conn = get_db_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE attendance_sessions
                        SET panel_sent=TRUE
                        WHERE date=%s AND time_slot=%s
                    """, (date_text, slot_text))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                release_db_connection(conn)

        await run_db(mark_panel_sent)
        last_panel_key = panel_key

        print(
            f"[자동 출석] {date_text} [세션 {slot_text}] "
            f"비밀번호={current_password} / 패널 생성 완료"
        )

    except Exception as e:
        # 패널 전송 중 하나라도 실패하면 panel_sent를 TRUE로 만들지 않습니다.
        # 다음 10초 주기에 다시 시도할 수 있습니다.
        print(f"[자동 출석 패널 오류] {type(e).__name__}: {e}")


# =====================================================
# 🔹 3시간 후 채널 전체 초기화
# =====================================================
@tasks.loop(seconds=10)
async def automatic_channel_cleanup():

    now = datetime.now(KST)

    if TEST_MODE:
        # 테스트 모드에서도 가산점 종료 여부는 DB의 created_at을 기준으로 합니다.
        date_text = now.strftime("%Y-%m-%d")
        slot_text = get_test_session_slot(now)
        bonus_session = await run_db(load_bonus_session, date_text, slot_text)

        if bonus_session and bonus_session[5] is not None:
            created_at = bonus_session[5]
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=KST)
            else:
                created_at = created_at.astimezone(KST)

            if now >= created_at + timedelta(hours=6):
                message_id = bonus_session[6]
                attendance_channel = bot.get_channel(ATTENDANCE_CHANNEL_ID)

                if message_id and attendance_channel is not None:
                    try:
                        message = await attendance_channel.fetch_message(message_id)
                        await message.delete()
                    except Exception:
                        pass

                await run_db(delete_bonus_session, date_text, slot_text)
                print("[가산점 종료] 6시간이 지나 가산점 세션을 종료했습니다.")

        return

    # 운영 모드:
    # 채팅방 초기화: 00:00 / 06:00 / 12:00 / 18:00
    # 일반 혈맹방과 관리자방의 채팅을 함께 초기화합니다.
    cleanup_hours = {0, 6, 12, 18}

    if now.hour not in cleanup_hours:
        return

    # 정각의 첫 1분 안에 한 번만 실행합니다.
    if now.minute > 0:
        return

    await clear_both_channels()

    print(
        f"[자동 채널 초기화] "
        f"{now.strftime('%Y-%m-%d %H:%M')}"
    )


# =====================================================
# 🔹 봇 설정
# =====================================================
google_sheet_worker_task = None

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# 봇 시작 시 출석 버튼을 Persistent View로 등록
bot.add_view(AttendanceView())
bot.add_view(StandaloneBonusView())


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

                        cursor.execute(
                            "TRUNCATE TABLE google_sheet_queue "
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


@bot.command(name="보스추가", aliases=["보스 추가"])
async def boss_add(ctx, *, boss_name: str = ""):
    if not is_admin_channel(ctx):
        return await ctx.send("❌ 관리자 전용방에서만 사용할 수 있습니다.")

    name = " ".join(boss_name.split()).strip()
    if not name:
        return await ctx.send("❌ 사용법: `!보스추가 이프리트`")

    try:
        inserted = await run_db(add_boss_db, name)

        if not inserted:
            return await ctx.send(
                f"⚠️ **{name}**은 이미 등록되어 있습니다."
            )

        # DB에 추가된 뒤 현재 등록된 모든 보스 목록으로
        # 관리자방 패널을 다시 생성한다.
        await ctx.send(f"🔴 **{name}** 보스가 등록되었습니다.")
        await send_boss_panel()

    except Exception as e:
        print(f"[보스추가 오류] {type(e).__name__}: {e}")
        await ctx.send(
            "❌ 보스 추가 중 DB 오류가 발생했습니다.\n"
            f"```{e}```"
        )



@bot.command(name="보스삭제", aliases=["보스 삭제"])
async def boss_delete(ctx, *, boss_name: str = ""):
    if not is_admin_channel(ctx):
        return await ctx.send("❌ 관리자 전용방에서만 사용할 수 있습니다.")

    name = " ".join(boss_name.split()).strip()
    if not name:
        return await ctx.send("❌ 사용법: `!보스삭제 이프리트`")

    try:
        deleted = await run_db(delete_boss_db, name)

        if not deleted:
            return await ctx.send(
                f"❌ **{name}** 보스를 찾을 수 없습니다."
            )

        await ctx.send(f"🗑️ **{name}** 보스가 삭제되었습니다.")
        await send_boss_panel()

    except Exception as e:
        print(f"[보스삭제 오류] {type(e).__name__}: {e}")
        await ctx.send(
            "❌ 보스 삭제 중 DB 오류가 발생했습니다.\n"
            f"```{e}```"
        )



@bot.command(name="득템")
async def drop_list_command(ctx):
    if not is_admin_channel(ctx):
        return await ctx.send("❌ 관리자 전용방에서만 사용할 수 있습니다.")

    try:
        drops = await run_db(load_all_drops)
        if not drops:
            return await ctx.send(
                "🎁 **전체 득템 내역**\n\n현재 등록된 득템 내역이 없습니다."
            )

        lines = ["🎁 **전체 득템 내역**", "━━━━━━━━━━━━━━━━━━━━", ""]
        for index, row in enumerate(drops, start=1):
            drop_id, boss_name, drop_name, username, created_at = row
            time_text = created_at.strftime("%m-%d %H:%M") if created_at else "-"
            lines.append(f"**{index}️⃣ {drop_name}**")
            lines.append(f"   👹 {boss_name}　│　👤 {username}　│　🕒 {time_text}")
            lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")

        chunks=[]
        current=""
        for line in lines:
            if len(current)+len(line)+1 > 1900:
                chunks.append(current)
                current=""
            current += line+"\n"
        if current.strip():
            chunks.append(current)

        for i, chunk in enumerate(chunks):
            if i == len(chunks)-1:
                await ctx.send(chunk, view=DropListView(drops))
            else:
                await ctx.send(chunk)

    except Exception as e:
        print(f"[득템 조회 오류] {type(e).__name__}: {e}")
        await ctx.send(f"❌ 득템 내역 조회 중 오류가 발생했습니다.\n```{e}```")



def load_weekly_scores(start_date, end_date):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT
                    user_id,
                    MAX(username) AS username,
                    SUM(attendance_points) AS attendance_points,
                    SUM(bonus_points) AS bonus_points,
                    SUM(attendance_points + bonus_points) AS total_points
                FROM (
                    SELECT user_id, username,
                           COALESCE(SUM(points), 0) AS attendance_points,
                           0::BIGINT AS bonus_points
                    FROM attendance_v2
                    WHERE date >= %s AND date <= %s
                    GROUP BY user_id, username

                    UNION ALL

                    SELECT user_id, username,
                           0::BIGINT AS attendance_points,
                           COALESCE(SUM(points), 0) AS bonus_points
                    FROM bonus_attendance
                    WHERE date >= %s AND date <= %s
                    GROUP BY user_id, username
                ) AS score_data
                GROUP BY user_id
                ORDER BY total_points DESC,
                         attendance_points DESC,
                         bonus_points DESC,
                         user_id ASC
            """, (start_date, end_date, start_date, end_date))
            return cursor.fetchall()
    finally:
        release_db_connection(conn)


def make_weekly_page(rows, period_name, start_date, end_date, page, page_size=50):
    total_pages=max(1,(len(rows)+page_size-1)//page_size)
    page=max(0,min(page,total_pages-1))
    offset=page*page_size
    page_rows=rows[offset:offset+page_size]

    lines=[
        f"📊 **{period_name} 주간 점수**",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📅 `{start_date} 00:00 ~ {end_date} 현재`",
        f"📄 **{page+1} / {total_pages} 페이지**  •  총 {len(rows)}명",
        "",
        "```text",
        "순위  혈맹원                    출석   가산점   총점",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    import unicodedata
    for i,row in enumerate(page_rows):
        rank=offset+i+1
        _,username,attendance_points,bonus_points,total_points=row
        name=str(username)
        display=""
        width=0
        for ch in name:
            cw=2 if unicodedata.east_asian_width(ch) in "WFA" else 1
            if width+cw>20: break
            display+=ch
            width+=cw
        display+=" " * max(0,20-width)

        rank_text={1:"🥇",2:"🥈",3:"🥉"}.get(rank,f"{rank:>3}")
        lines.append(
            f"{rank_text:<4}  {display}  "
            f"{int(attendance_points or 0):>4}   "
            f"{int(bonus_points or 0):>5}   "
            f"{int(total_points or 0):>4}"
        )

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "```"
    ]
    return "\n".join(lines),total_pages,page


class WeeklyScoreView(discord.ui.View):
    def __init__(self,ctx,rows,period_name,start_date,end_date,page=0):
        super().__init__(timeout=300)
        self.ctx_id=ctx.author.id
        self.rows=rows
        self.period_name=period_name
        self.start_date=start_date
        self.end_date=end_date
        self.page=page
        self.page_size=50
        self.refresh_buttons()

    def refresh_buttons(self):
        total_pages=max(1,(len(self.rows)+self.page_size-1)//self.page_size)
        self.previous_button.disabled=self.page<=0
        self.next_button.disabled=self.page>=total_pages-1

    async def interaction_check(self,interaction):
        if interaction.user.id!=self.ctx_id:
            await interaction.response.send_message(
                "❌ 이 주간 점수표를 실행한 사람만 페이지를 넘길 수 있습니다.",
                ephemeral=True
            )
            return False
        return True

    async def update_page(self,interaction):
        content,_,current_page=make_weekly_page(
            self.rows,self.period_name,self.start_date,
            self.end_date,self.page,self.page_size
        )
        self.page=current_page
        self.refresh_buttons()
        await interaction.response.edit_message(content=content,view=self)

    @discord.ui.button(label="◀ 이전",style=discord.ButtonStyle.secondary)
    async def previous_button(self,interaction,button):
        if self.page>0:
            self.page-=1
        await self.update_page(interaction)

    @discord.ui.button(label="다음 ▶",style=discord.ButtonStyle.primary)
    async def next_button(self,interaction,button):
        total_pages=max(1,(len(self.rows)+self.page_size-1)//self.page_size)
        if self.page<total_pages-1:
            self.page+=1
        await self.update_page(interaction)


async def weekly_score_command(ctx,weeks=1):
    if not is_admin_channel(ctx):
        return await ctx.send("❌ 관리자 전용방에서만 사용할 수 있습니다.")

    now=datetime.now(KST)
    today=now.date()
    current_monday=today-timedelta(days=today.weekday())
    start_date=current_monday-timedelta(days=(weeks-1)*7)

    rows=await run_db(
        load_weekly_scores,
        start_date.isoformat(),
        today.isoformat()
    )

    period_name={1:"이번 주",2:"최근 2주",3:"최근 3주",4:"최근 4주"}[weeks]
    content,_,page=make_weekly_page(
        rows,period_name,start_date.isoformat(),
        today.isoformat(),0,50
    )

    await ctx.send(
        content,
        view=WeeklyScoreView(
            ctx,rows,period_name,
            start_date.isoformat(),today.isoformat(),page
        )
    )



def load_period_scores(start_date, end_date):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT
                    user_id,
                    MAX(username) AS username,
                    SUM(attendance_points) AS attendance_points,
                    SUM(bonus_points) AS bonus_points,
                    SUM(attendance_points + bonus_points) AS total_points
                FROM (
                    SELECT user_id, username,
                           COALESCE(SUM(points), 0) AS attendance_points,
                           0::BIGINT AS bonus_points
                    FROM attendance_v2
                    WHERE date >= %s AND date <= %s
                    GROUP BY user_id, username

                    UNION ALL

                    SELECT user_id, username,
                           0::BIGINT AS attendance_points,
                           COALESCE(SUM(points), 0) AS bonus_points
                    FROM bonus_attendance
                    WHERE date >= %s AND date <= %s
                    GROUP BY user_id, username
                ) AS score_data
                GROUP BY user_id
                ORDER BY total_points DESC,
                         attendance_points DESC,
                         bonus_points DESC,
                         user_id ASC
            """, (start_date, end_date, start_date, end_date))
            return cursor.fetchall()
    finally:
        release_db_connection(conn)


def make_period_page(rows, start_date, end_date, page, page_size=50):
    import unicodedata
    total_pages = max(1, (len(rows) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    offset = page * page_size

    lines = [
        "📊 **기간별 점수 조회**",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📅 `{start_date} 00:00 ~ {end_date} 23:59`",
        f"📄 **{page + 1} / {total_pages} 페이지**  •  총 {len(rows)}명",
        "",
        "```text",
        "순위  혈맹원                    출석   가산점   총점",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    for i, row in enumerate(rows[offset:offset + page_size], start=offset + 1):
        _, username, attendance_points, bonus_points, total_points = row
        name = str(username)
        display = ""
        width = 0
        for ch in name:
            cw = 2 if unicodedata.east_asian_width(ch) in "WFA" else 1
            if width + cw > 20:
                break
            display += ch
            width += cw
        display += " " * max(0, 20 - width)

        rank_text = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i:>3}")
        lines.append(
            f"{rank_text:<4}  {display}  "
            f"{int(attendance_points or 0):>4}   "
            f"{int(bonus_points or 0):>5}   "
            f"{int(total_points or 0):>4}"
        )

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "```"
    ]
    return "\n".join(lines), total_pages, page


class PeriodScoreView(discord.ui.View):
    def __init__(self, ctx, rows, start_date, end_date, page=0):
        super().__init__(timeout=300)
        self.ctx_id = ctx.author.id
        self.rows = rows
        self.start_date = start_date
        self.end_date = end_date
        self.page = page
        self.page_size = 50
        self.refresh_buttons()

    def refresh_buttons(self):
        total = max(1, (len(self.rows) + self.page_size - 1) // self.page_size)
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= total - 1

    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx_id:
            await interaction.response.send_message(
                "❌ 이 기간 조회를 실행한 사람만 사용할 수 있습니다.",
                ephemeral=True
            )
            return False
        return True

    async def update_page(self, interaction):
        content, _, self.page = make_period_page(
            self.rows, self.start_date, self.end_date,
            self.page, self.page_size
        )
        self.refresh_buttons()
        await interaction.response.edit_message(content=content, view=self)

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction, button):
        self.page = max(0, self.page - 1)
        await self.update_page(interaction)

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction, button):
        total = max(1, (len(self.rows) + self.page_size - 1) // self.page_size)
        self.page = min(total - 1, self.page + 1)
        await self.update_page(interaction)


class PeriodQueryModal(discord.ui.Modal, title="📅 기간 조회"):
    start_date = discord.ui.TextInput(
        label="시작 날짜",
        placeholder="예: 2026-08-01",
        max_length=10,
        required=True
    )
    end_date = discord.ui.TextInput(
        label="종료 날짜",
        placeholder="예: 2026-08-17",
        max_length=10,
        required=True
    )

    async def on_submit(self, interaction):
        if interaction.channel_id != ADMIN_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ 관리자 전용방에서만 사용할 수 있습니다.",
                ephemeral=True
            )

        start_text = self.start_date.value.strip()
        end_text = self.end_date.value.strip()

        try:
            start = datetime.strptime(start_text, "%Y-%m-%d").date()
            end = datetime.strptime(end_text, "%Y-%m-%d").date()
        except ValueError:
            return await interaction.response.send_message(
                "❌ 날짜 형식이 올바르지 않습니다. 예: `2026-08-01`",
                ephemeral=True
            )

        if start > end:
            return await interaction.response.send_message(
                "❌ 시작 날짜가 종료 날짜보다 늦을 수 없습니다.",
                ephemeral=True
            )

        if (end - start).days > 366:
            return await interaction.response.send_message(
                "❌ 최대 1년 범위까지만 조회할 수 있습니다.",
                ephemeral=True
            )

        try:
            rows = await run_db(load_period_scores, start.isoformat(), end.isoformat())

            if not rows:
                return await interaction.response.send_message(
                    f"📊 **기간 조회 결과**\n\n`{start_text} ~ {end_text}`\n\n"
                    "조회된 기록이 없습니다."
                )

            content, _, page = make_period_page(
                rows, start.isoformat(), end.isoformat(), 0, 50
            )
            await interaction.response.send_message(
                content,
                view=PeriodScoreView(
                    interaction, rows,
                    start.isoformat(), end.isoformat(), page
                )
            )
        except Exception as e:
            print(f"[기간조회 오류] {type(e).__name__}: {e}")
            await interaction.response.send_message(
                f"❌ 기간 조회 중 DB 오류가 발생했습니다.\n```{e}```",
                ephemeral=True
            )


class PeriodQueryStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="📅 기간 입력", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction, button):
        if interaction.channel_id != ADMIN_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ 관리자 전용방에서만 사용할 수 있습니다.",
                ephemeral=True
            )
        await interaction.response.send_modal(PeriodQueryModal())


@bot.command(name="출석")
async def attendance_command(ctx):
    """관리자방에서 실행하면 일반 출석채널에 출석 패널을 띄우고,
    관리자방에는 출석 비밀번호와 보스 패널을 표시합니다.
    """
    global current_date, current_slot, current_password, last_panel_key

    if not is_admin_channel(ctx):
        return await ctx.send(
            "❌ 이 명령어는 관리자 전용방에서만 사용할 수 있습니다."
        )

    active_session = get_active_attendance_session()
    if active_session is None:
        return await ctx.send(
            "❌ 현재는 출석 가능한 시간대가 아닙니다.\n"
            "출석 시간: **03:00~09:00 / 09:00~15:00 / "
            "15:00~21:00 / 21:00~03:00**"
        )

    date_text, slot_text, session_start = active_session

    def load_attendance_session_for_slot():
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT date, time_slot, password, panel_sent
                    FROM attendance_sessions
                    WHERE date=%s AND time_slot=%s
                    LIMIT 1
                """, (date_text, slot_text))
                return cursor.fetchone()
        finally:
            release_db_connection(conn)

    session = await run_db(load_attendance_session_for_slot)

    if session:
        current_date, current_slot, current_password, panel_sent = session
    else:
        password = generate_password()

        def create_manual_session_if_missing():
            conn = get_db_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO attendance_sessions
                            (date, time_slot, password, panel_sent)
                        VALUES (%s, %s, %s, FALSE)
                        ON CONFLICT (date, time_slot) DO NOTHING
                        RETURNING date, time_slot, password, panel_sent
                    """, (date_text, slot_text, password))
                    row = cursor.fetchone()

                    if row is None:
                        cursor.execute("""
                            SELECT date, time_slot, password, panel_sent
                            FROM attendance_sessions
                            WHERE date=%s AND time_slot=%s
                            LIMIT 1
                        """, (date_text, slot_text))
                        row = cursor.fetchone()

                    conn.commit()
                    return row
            except Exception:
                conn.rollback()
                raise
            finally:
                release_db_connection(conn)

        session = await run_db(create_manual_session_if_missing)
        if session:
            current_date, current_slot, current_password, panel_sent = session
        else:
            return await ctx.send(
                "❌ 현재 시간대의 출석 세션을 만들지 못했습니다."
            )

    if current_password is None:
        return await ctx.send(
            "❌ 현재 시간대의 출석 비밀번호를 확인할 수 없습니다."
        )

    attendance_channel = bot.get_channel(ATTENDANCE_CHANNEL_ID)
    admin_channel = bot.get_channel(ADMIN_CHANNEL_ID)

    if attendance_channel is None or admin_channel is None:
        return await ctx.send(
            "❌ 출석채널 또는 관리자채널을 찾을 수 없습니다."
        )

    try:
        now = datetime.now(KST)

        await attendance_channel.send(
            f"📢 **{slot_text}시 출석 시간입니다!**\n"
            f"🕒 출석 가능시간: **{slot_text}:00 ~ "
            f"{(int(slot_text) + 6) % 24:02d}:00**\n"
            "아래 버튼을 눌러 출석해주세요.\n"
            "⭐ 출석 시 **1점**\n"
            "⏰ 유효시간: 6시간",
            view=AttendanceView()
        )

        await admin_channel.send(
            f"🔐 **출석 비밀번호**\n\n"
            f"```{current_password}```\n\n"
            f"🕒 출석 시간: {now.strftime('%H:%M:%S')}\n"
            "⏰ 유효시간: 6시간\n"
            "⚠️ 이 번호는 혈맹원에게 알려주세요."
        )

        await send_boss_panel()

        def mark_manual_panel_sent():
            conn = get_db_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE attendance_sessions
                        SET panel_sent=TRUE
                        WHERE date=%s AND time_slot=%s
                    """, (date_text, slot_text))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                release_db_connection(conn)

        await run_db(mark_manual_panel_sent)
        last_panel_key = f"{date_text}_{slot_text}"

        await ctx.send(
            f"✅ **{slot_text}시 출석 패널 생성 완료**\n"
            "🔐 비밀번호와 👹 보스 패널도 관리자방에 표시했습니다."
        )

    except Exception as e:
        print(f"[수동 출석 패널 오류] {type(e).__name__}: {e}")
        await ctx.send(
            "❌ 출석 패널 생성 중 오류가 발생했습니다.\n"
            f"```{e}```"
        )


@bot.command(name="기간조회")
async def period_query_command(ctx):
    if not is_admin_channel(ctx):
        return await ctx.send("❌ 관리자 전용방에서만 사용할 수 있습니다.")

    await ctx.send(
        "📅 **기간 조회**\n\n"
        "조회할 시작 날짜와 종료 날짜를 입력해주세요.\n\n"
        "**예시**\n"
        "시작 날짜: `2026-08-01`\n"
        "종료 날짜: `2026-08-17`\n\n"
        "아래 **📅 기간 입력** 버튼을 눌러 날짜를 입력하세요.",
        view=PeriodQueryStartView()
    )


@bot.command(name="주간")
async def weekly_command(ctx):
    await weekly_score_command(ctx,1)


@bot.command(name="2주간")
async def two_week_command(ctx):
    await weekly_score_command(ctx,2)


@bot.command(name="3주간")
async def three_week_command(ctx):
    await weekly_score_command(ctx,3)


@bot.command(name="4주간")
async def four_week_command(ctx):
    await weekly_score_command(ctx,4)



@bot.command(name="득템초기화")
async def drop_reset_command(ctx):
    if not is_admin_channel(ctx):
        return await ctx.send("❌ 관리자 전용방에서만 사용할 수 있습니다.")

    await ctx.send(
        "⚠️ **득템 목록 전체 초기화 경고** ⚠️\n\n"
        "이 작업을 진행하면 현재 DB에 등록된 **모든 득템 내역이 삭제됩니다.**\n"
        "삭제된 득템 내역은 복구할 수 없습니다.\n\n"
        "정말 초기화하려면 아래 버튼을 누르고\n"
        '**"동의합니다"**를 입력하세요.',
        view=DropResetView()
    )


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
    global current_date, current_slot, current_password

    try:
        boss_names[:] = await run_db(load_bosses)
    except Exception as e:
        print(f"[보스 목록 로드 오류] {e}")
        boss_names.clear()

    # 재연결/재시작 시에도 현재 출석 세션을 DB에서 즉시 복구합니다.
    try:
        active = await run_db(load_active_session)
        if active:
            current_date, current_slot, current_password = active
    except Exception as e:
        print(f"[출석 세션 복구 오류] {e}")

    if not automatic_attendance_panel.is_running():
        automatic_attendance_panel.start()

    if not automatic_channel_cleanup.is_running():
        automatic_channel_cleanup.start()

    global google_sheet_worker_task
    if google_sheet_worker_task is None or google_sheet_worker_task.done():
        google_sheet_worker_task = asyncio.create_task(google_sheet_sync_worker())

    print(f"로그인 완료: {bot.user}")
    print(
        f"출석채널: {ATTENDANCE_CHANNEL_ID}"
    )
    print(
        f"관리자채널: {ADMIN_CHANNEL_ID}"
    )
    print(
        f"테스트모드: {TEST_MODE} / "
        f"운영 시간: 03:00, 09:00, 15:00, 21:00 (KST)"
    )
    print("출석 가능시간: 각 시작 시각부터 6시간")
    print("가산점: 동일 출석 시간대 1회 / 유효시간 6시간")
    print("수동 출석: !출석")


# =====================================================
# 🔹 실행
# =====================================================
# 운영 버전: 기존에 정상 동작하던 단일 프로세스 구조를 유지합니다.
# Render Web Service의 PORT는 Flask가 별도 스레드에서 열어줍니다.
keep_alive()

bot.run(
    os.getenv("DISCORD_TOKEN")
)