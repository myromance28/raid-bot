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
import traceback
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
# 공성전/출석 동시 요청은 최대 10개의 DB 작업으로 제한합니다.
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

# =====================================================
# 🔹 Discord Gateway 상태 감시
# =====================================================
gateway_disconnected_at = None
gateway_last_connected_at = None
gateway_watchdog_task = None
render_self_ping_task = None
GATEWAY_WATCHDOG_INTERVAL = 15
GATEWAY_MAX_DISCONNECT_SECONDS = 300  # 5분

# =====================================================
# 🔹 Render Free 슬립 완화용 경량 self-ping
# =====================================================
# Render가 제공하는 RENDER_EXTERNAL_URL의 "/"를 10분마다 1회 호출합니다.
# 별도 외부 서비스/환경변수 설정 없이 현재 Web Service 자체를 가볍게 확인합니다.
RENDER_SELF_PING_INTERVAL = 600  # 10분


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

            # 공성전 출석 세션
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS siege_sessions (
                    id SERIAL PRIMARY KEY,
                    date TEXT NOT NULL,
                    password TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                ALTER TABLE siege_sessions
                ADD COLUMN IF NOT EXISTS created_at_kst TEXT
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS siege_attendance (
                    id SERIAL PRIMARY KEY,
                    siege_session_id BIGINT NOT NULL,
                    date TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(siege_session_id, user_id)
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                ALTER TABLE bonus_sessions
                DROP CONSTRAINT IF EXISTS bonus_sessions_date_time_slot_key
            """)
            cursor.execute("""
                DROP INDEX IF EXISTS bonus_sessions_date_time_slot_key
            """)
            cursor.execute("""
                DROP INDEX IF EXISTS bonus_sessions_date_time_slot_unique_idx
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS bonus_sessions_slot_idx
                ON bonus_sessions(date, time_slot, created_at)
            """)

            # 가산점 생성시각을 KST로 명시적으로 저장합니다.
            # 기존 DB에도 자동으로 컬럼을 추가합니다.
            cursor.execute("""
                ALTER TABLE bonus_sessions
                ADD COLUMN IF NOT EXISTS created_at_kst TEXT
            """)

            # 기존 DB에 이름이 다른 UNIQUE 제약/인덱스가 남아 있어도
            # (date, time_slot) 1개 제한을 찾아 제거합니다.
            cursor.execute("""
                DO $$
                DECLARE r RECORD;
                BEGIN
                    FOR r IN
                        SELECT c.conname
                        FROM pg_constraint c
                        JOIN pg_class t ON t.oid = c.conrelid
                        WHERE t.relname = 'bonus_sessions'
                          AND c.contype = 'u'
                          AND pg_get_constraintdef(c.oid) ILIKE '%(date, time_slot)%'
                    LOOP
                        EXECUTE format(
                            'ALTER TABLE bonus_sessions DROP CONSTRAINT IF EXISTS %I',
                            r.conname
                        );
                    END LOOP;
                END $$;
            """)

            cursor.execute("""
                DO $$
                DECLARE r RECORD;
                BEGIN
                    FOR r IN
                        SELECT indexname
                        FROM pg_indexes
                        WHERE tablename = 'bonus_sessions'
                          AND indexdef ILIKE 'CREATE UNIQUE INDEX%'
                          AND indexdef ILIKE '%(date, time_slot)%'
                    LOOP
                        EXECUTE format(
                            'DROP INDEX IF EXISTS %I',
                            r.indexname
                        );
                    END LOOP;
                END $$;
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
                    bonus_session_id BIGINT
                )
            """)

            cursor.execute("""
                ALTER TABLE bonus_attendance
                ADD COLUMN IF NOT EXISTS bonus_session_id BIGINT
            """)

            cursor.execute("""
                ALTER TABLE bonus_attendance
                DROP CONSTRAINT IF EXISTS bonus_attendance_date_time_slot_user_id_key
            """)
            cursor.execute("""
                DROP INDEX IF EXISTS bonus_attendance_date_time_slot_user_id_key
            """)

            cursor.execute("""
                DO $$
                DECLARE r RECORD;
                BEGIN
                    FOR r IN
                        SELECT c.conname
                        FROM pg_constraint c
                        JOIN pg_class t ON t.oid = c.conrelid
                        WHERE t.relname = 'bonus_attendance'
                          AND c.contype = 'u'
                          AND pg_get_constraintdef(c.oid) ILIKE '%(date, time_slot, user_id)%'
                    LOOP
                        EXECUTE format(
                            'ALTER TABLE bonus_attendance DROP CONSTRAINT IF EXISTS %I',
                            r.conname
                        );
                    END LOOP;
                END $$;
            """)

            cursor.execute("""
                DO $$
                DECLARE r RECORD;
                BEGIN
                    FOR r IN
                        SELECT indexname
                        FROM pg_indexes
                        WHERE tablename = 'bonus_attendance'
                          AND indexdef ILIKE 'CREATE UNIQUE INDEX%'
                          AND indexdef ILIKE '%(date, time_slot, user_id)%'
                    LOOP
                        EXECUTE format(
                            'DROP INDEX IF EXISTS %I',
                            r.indexname
                        );
                    END LOOP;
                END $$;
            """)

            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS bonus_attendance_session_user_idx
                ON bonus_attendance(bonus_session_id, user_id)
                WHERE bonus_session_id IS NOT NULL
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


def save_attendance_atomic(
    entered_password,
    user_id,
    username
):
    """일반 출석을 세션 확인부터 기록까지 하나의 DB 트랜잭션으로 처리합니다."""
    conn = get_db_connection()
    try:
        now = datetime.now(KST)
        active_session = get_active_attendance_session(now)

        if active_session is None:
            conn.rollback()
            return "expired"

        date_text, slot_text, session_start = active_session

        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT password
                FROM attendance_sessions
                WHERE date=%s AND time_slot=%s
                LIMIT 1
            """, (date_text, slot_text))
            session = cursor.fetchone()

            if not session:
                conn.rollback()
                return "missing_session"

            if now >= session_start + timedelta(hours=6):
                conn.rollback()
                return "expired"

            if str(entered_password).strip() != str(session[0]):
                conn.rollback()
                return "password"

            cursor.execute("""
                INSERT INTO attendance_v2
                    (date, time_slot, user_id, username, points)
                VALUES (%s, %s, %s, %s, 1)
                ON CONFLICT (date, time_slot, user_id)
                DO NOTHING
                RETURNING id
            """, (date_text, slot_text, int(user_id), str(username)))

            if cursor.fetchone() is None:
                conn.rollback()
                return "duplicate"

            enqueue_google_sheet_record(
                cursor,
                date_text,
                now.strftime("%H:%M:%S"),
                "출석",
                user_id,
                username,
                1
            )

        conn.commit()
        return "success"

    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


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
def load_bonus_session(session_id):
    """특정 !가산점 호출 세션 조회."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, date, time_slot, points, password,
                       created_at, message_id, created_at_kst
                FROM bonus_sessions
                WHERE id=%s
                LIMIT 1
            """, (int(session_id),))
            return cursor.fetchone()
    finally:
        release_db_connection(conn)


def load_active_bonus_session(date_text, slot_text):
    """현재 출석 시간대에서 아직 5분이 지나지 않은 마지막 가산점 세션."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, date, time_slot, points, password,
                       created_at, message_id, created_at_kst
                FROM bonus_sessions
                WHERE date=%s
                  AND time_slot=%s
                  AND created_at_kst IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
            """, (str(date_text), str(slot_text)))
            row = cursor.fetchone()

            if not row:
                return None

            created = row[7]
            try:
                created_at_kst = datetime.strptime(
                    str(created), "%Y-%m-%d %H:%M:%S.%f"
                ).replace(tzinfo=KST)
            except Exception:
                try:
                    created_at_kst = datetime.strptime(
                        str(created), "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=KST)
                except Exception:
                    return None

            if datetime.now(KST) >= created_at_kst + timedelta(minutes=5):
                return None

            return row
    finally:
        release_db_connection(conn)


def save_bonus_session(date_text, slot_text, points, password):
    """!가산점 호출마다 새로운 5분짜리 세션 생성."""
    conn = get_db_connection()
    try:
        created_at_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S.%f")
        with conn.cursor() as cursor:
            # 구버전 DB도 이 호출에서 바로 보정합니다.
            cursor.execute("""
                ALTER TABLE bonus_sessions
                ADD COLUMN IF NOT EXISTS created_at_kst TEXT
            """)
            cursor.execute("""
                ALTER TABLE bonus_sessions
                ADD COLUMN IF NOT EXISTS message_id BIGINT
            """)
            cursor.execute("""
                INSERT INTO bonus_sessions
                    (date, time_slot, points, password, created_at_kst)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, date, time_slot, points, password,
                          created_at, message_id, created_at_kst
            """, (
                date_text, slot_text, int(points), password, created_at_kst
            ))
            row=cursor.fetchone()
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def update_bonus_message_id(session_id, message_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE bonus_sessions
                SET message_id=%s
                WHERE id=%s
            """, (int(message_id), int(session_id)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def delete_bonus_session(session_id):
    """5분짜리 세션만 삭제. bonus_attendance 기록은 유지."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM bonus_sessions WHERE id=%s",
                (int(session_id),)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def create_siege_session(date_text, password):
    conn = get_db_connection()
    try:
        created_at_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S.%f")
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO siege_sessions (date, password, created_at_kst)
                VALUES (%s, %s, %s)
                RETURNING id, date, password, created_at, created_at_kst
            """, (date_text, password, created_at_kst))
            row = cursor.fetchone()
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def load_siege_session(session_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, date, password, created_at, created_at_kst
                FROM siege_sessions
                WHERE id=%s
                LIMIT 1
            """, (int(session_id),))
            return cursor.fetchone()
    finally:
        release_db_connection(conn)


def save_siege_attendance_atomic(
    session_id,
    entered_password,
    current_date_text,
    user_id,
    username
):
    """공성전 출석 1회 처리를 하나의 DB 트랜잭션으로 수행합니다."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, date, password, created_at_kst
                FROM siege_sessions
                WHERE id=%s
                LIMIT 1
            """, (int(session_id),))
            session = cursor.fetchone()

            if not session:
                conn.rollback()
                return "expired"

            db_session_id, date_text, stored_password, created_at_kst_text = session

            if str(date_text) != str(current_date_text):
                conn.rollback()
                return "date"

            try:
                created_at_kst = datetime.strptime(
                    str(created_at_kst_text),
                    "%Y-%m-%d %H:%M:%S.%f"
                ).replace(tzinfo=KST)
            except ValueError:
                created_at_kst = datetime.strptime(
                    str(created_at_kst_text),
                    "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=KST)

            now = datetime.now(KST)

            if now >= created_at_kst + timedelta(hours=1):
                conn.rollback()
                return "expired"

            if str(entered_password).strip() != str(stored_password):
                conn.rollback()
                return "password"

            cursor.execute("""
                INSERT INTO siege_attendance
                    (siege_session_id, date, user_id, username)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (siege_session_id, user_id)
                DO NOTHING
                RETURNING id
            """, (
                int(db_session_id),
                str(date_text),
                int(user_id),
                str(username)
            ))

            if cursor.fetchone() is None:
                conn.rollback()
                return "duplicate"

            # 공성출석은 날짜/닉네임/출석만 필요하므로 time은 빈 값으로 큐에 저장합니다.
            enqueue_google_sheet_record(
                cursor,
                date_text,
                "",
                "공성출석",
                user_id,
                username,
                0
            )

        conn.commit()
        return "success"
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def delete_siege_session(session_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM siege_sessions WHERE id=%s",
                (int(session_id),)
            )
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


def save_bonus_attendance_atomic(
    session_id,
    entered_password,
    active_date,
    active_slot,
    user_id,
    username,
    time_text
):
    """가산점 제출 전체를 하나의 DB 트랜잭션으로 처리."""
    conn=get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, date, time_slot, points, password, created_at_kst
                FROM bonus_sessions
                WHERE id=%s
                LIMIT 1
            """,(int(session_id),))
            session=cursor.fetchone()

            if not session:
                conn.rollback()
                return "expired", None

            db_session_id,date_text,slot_text,points,stored_password,created_at_kst_text=session

            if str(date_text)!=str(active_date) or str(slot_text)!=str(active_slot):
                conn.rollback()
                return "slot", None

            try:
                created_at_kst=datetime.strptime(
                    str(created_at_kst_text), "%Y-%m-%d %H:%M:%S.%f"
                ).replace(tzinfo=KST)
            except Exception:
                try:
                    created_at_kst=datetime.strptime(
                        str(created_at_kst_text), "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=KST)
                except Exception:
                    conn.rollback()
                    return "expired", None

            now=datetime.now(KST)
            if now >= created_at_kst + timedelta(minutes=5):
                conn.rollback()
                return "expired", None

            if str(entered_password).strip()!=str(stored_password):
                conn.rollback()
                return "password", None

            cursor.execute("""
                INSERT INTO bonus_attendance
                    (date, time_slot, points, user_id, username, bonus_session_id)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (bonus_session_id,user_id)
                DO NOTHING
                RETURNING id
            """,(str(date_text),str(slot_text),int(points),int(user_id),
                 str(username),int(db_session_id)))

            if cursor.fetchone() is None:
                conn.rollback()
                return "duplicate", int(points)

            enqueue_google_sheet_record(
                cursor,str(date_text),str(time_text),"가산점",
                int(user_id),str(username),int(points)
            )

        conn.commit()
        return "success", int(points)

    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def save_bonus_attendance(
    session_id,
    date_text,
    slot_text,
    points,
    user_id,
    username,
    time_text=None
):
    """
    같은 호출에서는 사용자당 1회.
    새로운 !가산점 호출에서는 다시 받을 수 있습니다.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO bonus_attendance
                    (date, time_slot, points, user_id, username, bonus_session_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (bonus_session_id, user_id)
                DO NOTHING
                RETURNING id
            """, (
                date_text,
                slot_text,
                points,
                user_id,
                username,
                int(session_id)
            ))

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


def has_bonus_attendance_for_session(session_id, user_id):
    """현재 5분 가산점 호출에서 이 사용자가 이미 점수를 받았는지 확인합니다."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT 1
                FROM bonus_attendance
                WHERE bonus_session_id=%s AND user_id=%s
                LIMIT 1
            """, (int(session_id), int(user_id)))
            return cursor.fetchone() is not None
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
class SiegeAttendanceModal(
    discord.ui.Modal,
    title="🏰 공성전 출석 비밀번호"
):
    password_input = discord.ui.TextInput(
        label="공성전 비밀번호",
        placeholder="관리자 전용방에 표시된 번호를 입력하세요.",
        min_length=4,
        max_length=4,
        required=True
    )

    def __init__(self, session_id):
        super().__init__()
        self.session_id = int(session_id)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.channel_id != ATTENDANCE_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ 일반 혈맹 출석창에서만 사용할 수 있습니다.",
                ephemeral=True
            )

        # 150명이 동시에 제출해도 interaction timeout을 피하도록 즉시 defer.
        await interaction.response.defer(ephemeral=True)

        try:
            result = await run_db(
                save_siege_attendance_atomic,
                self.session_id,
                str(self.password_input.value).strip(),
                datetime.now(KST).strftime("%Y-%m-%d"),
                interaction.user.id,
                interaction.user.display_name
            )

            if result == "success":
                return await interaction.followup.send(
                    f"🏰 **공성전 출석 완료!**\n"
                    f"👤 {interaction.user.display_name}\n"
                    "✅ 출석",
                    ephemeral=True
                )

            if result == "password":
                message = "❌ 공성전 비밀번호가 틀렸습니다."
            elif result == "duplicate":
                message = "⚠️ 이미 이번 공성전 출석을 완료하셨습니다."
            elif result == "date":
                message = "❌ 오늘 생성된 공성전 출석이 아닙니다."
            else:
                message = "❌ 공성전 출석 가능시간 1시간이 종료되었거나 세션이 없습니다."

            return await interaction.followup.send(
                message,
                ephemeral=True
            )

        except Exception as e:
            print(f"[공성전 출석 오류] {type(e).__name__}: {e}")
            try:
                await interaction.followup.send(
                    "❌ 공성전 출석 처리 중 오류가 발생했습니다.",
                    ephemeral=True
                )
            except Exception:
                pass


class SiegeAttendanceButton(discord.ui.Button):
    def __init__(self, session_id):
        super().__init__(
            label="공성전",
            style=discord.ButtonStyle.secondary,
            custom_id=f"raid_siege_attendance_{int(session_id)}"
        )
        self.session_id = int(session_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            SiegeAttendanceModal(self.session_id)
        )


class SiegeAttendanceView(discord.ui.View):
    def __init__(self, session_id):
        super().__init__(timeout=3600)
        self.add_item(SiegeAttendanceButton(session_id))


async def delete_siege_message_after(message, delay_seconds=3600):
    try:
        await asyncio.sleep(delay_seconds)
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            print("[공성전 메시지 자동삭제 실패] Discord 권한 부족")
        except discord.HTTPException as e:
            print(f"[공성전 메시지 자동삭제 실패] HTTP 오류: {e}")
        except Exception as e:
            print(f"[공성전 메시지 자동삭제 오류] {type(e).__name__}: {e}")
    except asyncio.CancelledError:
        raise


async def expire_siege_session_after(session_id, delay_seconds=3600):
    try:
        await asyncio.sleep(delay_seconds)
        await run_db(delete_siege_session, session_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[공성전 세션 자동삭제 오류] {type(e).__name__}: {e}")


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

    def __init__(self, session_id):
        super().__init__()
        self.session_id=int(session_id)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.channel_id != ATTENDANCE_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ 일반 혈맹 출석창에서만 사용할 수 있습니다.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            active_session=get_active_attendance_session(datetime.now(KST))
            if active_session is None:
                return await interaction.followup.send(
                    "❌ 현재 출석 시간대가 아닙니다.",
                    ephemeral=True
                )

            active_date,active_slot,_=active_session

            result,points=await run_db(
                save_bonus_attendance_atomic,
                self.session_id,
                str(self.password_input.value).strip(),
                active_date,
                active_slot,
                interaction.user.id,
                interaction.user.display_name,
                datetime.now(KST).strftime("%H:%M:%S")
            )

            if result=="success":
                return await interaction.followup.send(
                    f"🔵 **가산점 +{points}점 지급 완료!**\n"
                    f"👤 {interaction.user.display_name}\n"
                    f"⭐ +{points}점",
                    ephemeral=True
                )

            if result=="password":
                message="❌ 가산점 비밀번호가 틀렸습니다."
            elif result=="duplicate":
                message="⚠️ 이번 가산점 호출에서는 이미 점수를 받으셨습니다."
            elif result=="slot":
                message="❌ 현재 출석 시간대의 가산점이 아닙니다."
            else:
                message="❌ 이 가산점 호출은 5분이 지나 종료되었습니다."

            await interaction.followup.send(message,ephemeral=True)

        except Exception as e:
            print(f"[가산점 처리 오류] {type(e).__name__}: {e}")
            try:
                await interaction.followup.send(
                    "❌ 가산점 처리 중 오류가 발생했습니다.\n"
                    f"오류: `{type(e).__name__}`",
                    ephemeral=True
                )
            except Exception:
                pass


class BonusButton(discord.ui.Button):
    def __init__(self, session_id):
        super().__init__(
            label="🔵 가산점",
            style=discord.ButtonStyle.primary,
            custom_id=f"raid_bonus_button_{int(session_id)}"
        )
        self.session_id = int(session_id)

    async def callback(self, interaction: discord.Interaction):
        # 버튼 클릭 순간에는 DB 조회를 하지 않고 즉시 비밀번호 입력창을 띄웁니다.
        # 실제 중복 지급 여부는 비밀번호 제출 시점의 원자적 INSERT에서 최종 판정합니다.
        await interaction.response.send_modal(
            BonusPasswordModal(self.session_id)
        )


class StandaloneBonusView(discord.ui.View):
    """특정 !가산점 호출에 연결된 5분짜리 버튼."""
    def __init__(self, session_id):
        super().__init__(timeout=300)
        self.add_item(BonusButton(session_id))


async def delete_message_after(message, delay_seconds=60):
    """호출자가 지정한 시간 후 메시지를 삭제합니다."""
    try:
        await asyncio.sleep(delay_seconds)
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            print("[가산점 메시지 자동삭제 실패] Discord 권한 부족")
        except discord.HTTPException as e:
            print(f"[가산점 메시지 자동삭제 실패] HTTP 오류: {e}")
        except Exception as e:
            print(f"[가산점 메시지 자동삭제 오류] {type(e).__name__}: {e}")
    except asyncio.CancelledError:
        raise


async def expire_bonus_session_after(session_id, delay_seconds=300):
    """1분 후 호출 세션만 삭제하고, 지급 기록은 그대로 둡니다."""
    try:
        await asyncio.sleep(delay_seconds)
        await run_db(delete_bonus_session, session_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[가산점 세션 자동삭제 오류] {type(e).__name__}: {e}")


class BonusPointButton(discord.ui.Button):
    def __init__(self, points):
        super().__init__(
            label=f"{points}점",
            style=discord.ButtonStyle.primary
        )
        self.points = points

    async def callback(self, interaction: discord.Interaction):
        if not is_admin_channel(interaction):
            return await interaction.response.send_message(
                "❌ 관리자 전용방에서만 사용할 수 있습니다.",
                ephemeral=True
            )

        # DB 작업 전에 즉시 defer
        await interaction.response.defer(ephemeral=True)

        active_session = get_active_attendance_session(datetime.now(KST))

        if active_session is None:
            return await interaction.followup.send(
                "⚠️ 현재는 출석 시간대가 아닙니다.\n"
                "출석 시간은 **03:00 / 09:00 / 15:00 / 21:00**입니다.",
                ephemeral=True
            )

        date_text, slot_text, _ = active_session

        try:
            # -------------------------------------------------
            # 현재 5분 이내에 살아있는 가산점이 있으면 재사용
            # -------------------------------------------------
            existing = await run_db(
                load_active_bonus_session,
                date_text,
                slot_text
            )

            if existing:
                (
                    session_id,
                    _,
                    _,
                    existing_points,
                    existing_password,
                    _,
                    existing_message_id,
                    _
                ) = existing

                return await interaction.followup.send(
                    "🔵 **현재 가산점이 이미 활성화되어 있습니다.**\n\n"
                    f"⭐ 가산점: **+{int(existing_points)}점**\n"
                    f"🔐 가산점 비밀번호\n"
                    f"```{existing_password}```\n\n"
                    "⏰ **기존 가산점 호출은 최초 생성 시점부터 5분 동안 유효합니다.**",
                    ephemeral=True
                )

            # -------------------------------------------------
            # 5분이 지난 경우에만 새로운 가산점 세션 생성
            # -------------------------------------------------
            password = generate_password()

            bonus_session = await run_db(
                save_bonus_session,
                date_text,
                slot_text,
                self.points,
                password
            )

            (
                session_id,
                _,
                _,
                session_points,
                session_password,
                _,
                _,
                _
            ) = bonus_session

            # -------------------------------------------------
            # 일반 혈맹방 가산점 패널
            # -------------------------------------------------
            attendance_channel = bot.get_channel(ATTENDANCE_CHANNEL_ID)

            if attendance_channel is not None:
                bonus_message = await attendance_channel.send(
                    "🔵 **가산점 패널**\n"
                    f"⭐ 가산점: **+{int(session_points)}점**\n"
                    "아래 파란색 버튼을 눌러 비밀번호를 입력하세요.\n"
                    "⏰ **이 호출은 5분 동안만 유효합니다.**",
                    view=StandaloneBonusView(session_id)
                )

                bonus_message_ids.add(bonus_message.id)

                await run_db(
                    update_bonus_message_id,
                    session_id,
                    bonus_message.id
                )

                # 5분 후 일반방 가산점 패널 삭제
                asyncio.create_task(
                    delete_message_after(bonus_message, 300)
                )

            # -------------------------------------------------
            # 관리자방 비밀번호 메시지
            #
            # ephemeral이 아니라 실제 메시지로 보내서
            # 5분 후 패널과 함께 자동 삭제할 수 있게 합니다.
            # -------------------------------------------------
            admin_password_message = await interaction.followup.send(
                f"🔵 **가산점 +{int(session_points)}점 생성 완료**\n\n"
                f"🔐 가산점 비밀번호\n"
                f"```{session_password}```\n\n"
                "⏰ **이 호출은 5분 동안만 유효합니다.**",
                wait=True
            )

            # 5분 후 관리자 비밀번호 메시지 삭제
            asyncio.create_task(
                delete_message_after(admin_password_message, 300)
            )

            # 5분 후 세션 종료
            asyncio.create_task(
                expire_bonus_session_after(session_id, 300)
            )

        except Exception as e:
            print(f"[가산점 생성 오류] {type(e).__name__}: {e}")
            try:
                await interaction.followup.send(
                    "❌ 가산점 생성 중 오류가 발생했습니다.\n"
                    f"오류: `{type(e).__name__}`",
                    ephemeral=True
                )
            except Exception:
                pass


class BonusPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

        for points in range(1, 11):
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

        # 150명 동시 제출에서도 interaction timeout을 피하도록 즉시 defer합니다.
        await interaction.response.defer(ephemeral=True)

        try:
            result = await run_db(
                save_attendance_atomic,
                str(self.password_input.value).strip(),
                interaction.user.id,
                interaction.user.display_name
            )

            messages = {
                "success": (
                    f"✅ **출석 완료!**\n"
                    f"👤 {interaction.user.display_name}"
                ),
                "expired": "❌ 현재 출석 가능한 시간이 아닙니다.",
                "missing_session": (
                    "❌ 현재 출석 세션이 아직 생성되지 않았습니다.\n"
                    "잠시 후 다시 시도해주세요."
                ),
                "password": "❌ 비밀번호가 틀렸습니다.",
                "duplicate": "⚠️ 이미 이번 출석을 완료하셨습니다."
            }

            return await interaction.followup.send(
                messages.get(result, "❌ 출석 처리에 실패했습니다."),
                ephemeral=True
            )

        except Exception as e:
            print(f"[출석 처리 오류] {type(e).__name__}: {e}")
            try:
                await interaction.followup.send(
                    "❌ 출석 처리 중 오류가 발생했습니다.\n"
                    f"오류: `{type(e).__name__}`",
                    ephemeral=True
                )
            except Exception:
                pass


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
        "아래 1~3점 중 하나를 눌러야 가산점이 생성됩니다.\n\n"
        "⚠️ 가산점은 이 명령어를 실행하기 전에는 "
        "일반 출석창에 나타나지 않습니다.",
        view=BonusPanelView()
    )


@bot.command(name="공성전")
async def siege_command(ctx):
    """관리자방에서 공성전 출석 세션을 1시간 동안 생성합니다."""
    if not is_admin_channel(ctx):
        return await ctx.send(
            "❌ 이 명령어는 관리자 전용방에서만 사용할 수 있습니다."
        )

    date_text = datetime.now(KST).strftime("%Y-%m-%d")
    password = generate_password()

    try:
        session = await run_db(create_siege_session, date_text, password)
        session_id, _, session_password, _, _ = session

        admin_message = await ctx.send(
            "🏰 **공성전 출석 시작**\n\n"
            f"🔐 공성전 비밀번호\n"
            f"```{session_password}```\n\n"
            "⏰ **호출 후 1시간 동안 유효합니다.**"
        )
        asyncio.create_task(
            delete_siege_message_after(admin_message, 3600)
        )

        attendance_channel = bot.get_channel(ATTENDANCE_CHANNEL_ID)
        if attendance_channel is None:
            await ctx.send("❌ 일반 혈맹 출석창을 찾을 수 없습니다.")
            return

        attendance_message = await attendance_channel.send(
            "🏰 **공성전 출석**\n"
            "공성전에 참여하신 분은 아래 **노란색 버튼**을 눌러 출석해주세요.\n"
            "⏰ **호출 후 1시간 동안 유효합니다.**",
            view=SiegeAttendanceView(session_id)
        )

        asyncio.create_task(
            delete_siege_message_after(attendance_message, 3600)
        )
        asyncio.create_task(
            expire_siege_session_after(session_id, 3600)
        )

        print(
            f"[공성전 생성] id={session_id} / "
            f"date={date_text} / password={session_password}"
        )

    except Exception as e:
        print(f"[공성전 생성 오류] {type(e).__name__}: {e}")
        await ctx.send(
            f"❌ 공성전 생성 중 오류가 발생했습니다.\n```{e}```"
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
            "⏰ 메시지/버튼 유지시간: 1시간",
            view=AttendanceView()
        )

        await admin_channel.send(
            f"🔐 **출석 비밀번호**\n\n"
            f"```{current_password}```\n\n"
            f"🕒 출석 시간: {now.strftime('%H:%M:%S')}\n"
            "⏰ 메시지/버튼 유지시간: 1시간\n"
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
# 🔹 Discord Gateway 감시
# =====================================================
@bot.event
async def on_connect():
    global gateway_last_connected_at
    gateway_last_connected_at = datetime.now(KST)
    print(
        f"[DISCORD] Gateway 연결됨 "
        f"({gateway_last_connected_at.strftime('%Y-%m-%d %H:%M:%S')})"
    )


@bot.event
async def on_disconnect():
    global gateway_disconnected_at
    gateway_disconnected_at = datetime.now(KST)
    print(
        f"[DISCORD] Gateway 연결 끊김 "
        f"({gateway_disconnected_at.strftime('%Y-%m-%d %H:%M:%S')})"
    )


@bot.event
async def on_resumed():
    global gateway_disconnected_at, gateway_last_connected_at
    gateway_disconnected_at = None
    gateway_last_connected_at = datetime.now(KST)
    print(
        f"[DISCORD] Gateway Resume 성공 "
        f"({gateway_last_connected_at.strftime('%Y-%m-%d %H:%M:%S')})"
    )


@bot.event
async def on_error(event, *args, **kwargs):
    print(f"[DISCORD EVENT ERROR] event={event}")
    traceback.print_exc()


async def render_self_ping_worker():
    """
    Render Free의 idle sleep을 완화하기 위한 경량 HTTP self-ping입니다.
    10분마다 자신의 public URL "/"를 한 번 호출합니다.
    """
    while True:
        try:
            await asyncio.sleep(RENDER_SELF_PING_INTERVAL)

            base_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
            if not base_url:
                print("[RENDER PING] RENDER_EXTERNAL_URL이 없어 건너뜁니다.")
                continue

            url = base_url.rstrip("/") + "/"

            def ping():
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": "RAID-Bot-Render-Ping"},
                    method="GET",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status

            status = await asyncio.to_thread(ping)
            print(f"[RENDER PING] OK status={status}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[RENDER PING 오류] {type(e).__name__}: {e}")
            await asyncio.sleep(10)


async def discord_gateway_watchdog():
    """
    Discord Gateway 연결 상태를 감시합니다.
    일시적인 끊김은 discord.py의 자동 재연결에 맡기고,
    5분 이상 disconnect 상태가 지속되면 프로세스를 종료하여
    Render가 새 프로세스를 기동하도록 합니다.
    """
    while True:
        try:
            await asyncio.sleep(GATEWAY_WATCHDOG_INTERVAL)

            global gateway_disconnected_at

            if gateway_disconnected_at is None:
                continue

            elapsed = (
                datetime.now(KST) - gateway_disconnected_at
            ).total_seconds()

            print(
                f"[DISCORD WATCHDOG] Gateway 단절 "
                f"{int(elapsed)}초 경과"
            )

            if elapsed >= GATEWAY_MAX_DISCONNECT_SECONDS:
                print(
                    "[DISCORD WATCHDOG] Gateway가 "
                    f"{GATEWAY_MAX_DISCONNECT_SECONDS}초 이상 복구되지 않아 "
                    "프로세스를 종료합니다. Render가 자동 재시작해야 합니다."
                )
                os._exit(1)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(
                f"[DISCORD WATCHDOG ERROR] "
                f"{type(e).__name__}: {e}"
            )
            await asyncio.sleep(5)


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

    global gateway_watchdog_task
    if gateway_watchdog_task is None or gateway_watchdog_task.done():
        gateway_watchdog_task = asyncio.create_task(
            discord_gateway_watchdog()
        )

    global render_self_ping_task
    if render_self_ping_task is None or render_self_ping_task.done():
        render_self_ping_task = asyncio.create_task(
            render_self_ping_worker()
        )

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
    print("가산점: 호출마다 새 세션 / 호출당 5분 유효")
    print("수동 출석: !출석")
    print("공성전 출석: !공성전 / 호출 후 1시간 유효")


# =====================================================
# 🔹 실행
# =====================================================
# 운영 버전: 기존에 정상 동작하던 단일 프로세스 구조를 유지합니다.
# Render Web Service의 PORT는 Flask가 별도 스레드에서 열어줍니다.
keep_alive()

bot.run(
    os.getenv("DISCORD_TOKEN")
)