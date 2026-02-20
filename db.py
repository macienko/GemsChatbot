"""Lightweight PostgreSQL helper for daily message limits.

Uses DATABASE_URL (provided automatically by Railway's Postgres add-on).
If DATABASE_URL is not set, all limit checks are skipped (unlimited mode).
"""

import logging
import os
from datetime import date

import psycopg2
from psycopg2.extras import DictCursor

logger = logging.getLogger(__name__)

_DATABASE_URL: str | None = os.environ.get("DATABASE_URL")


def _get_conn():
    """Return a new database connection."""
    if not _DATABASE_URL:
        return None
    return psycopg2.connect(_DATABASE_URL)


def init_db() -> None:
    """Create required tables if they don't exist."""
    conn = _get_conn()
    if conn is None:
        logger.info("DATABASE_URL not set — message limits disabled")
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_message_counts (
                    user_id     TEXT PRIMARY KEY,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    last_reset  DATE NOT NULL DEFAULT CURRENT_DATE
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id         SERIAL PRIMARY KEY,
                    phone      TEXT NOT NULL,
                    direction  TEXT NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
                    body       TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_created_at
                ON messages (created_at)
            """)
        logger.info("Database initialised (tables ready)")
    finally:
        conn.close()


def check_and_increment(user_id: str) -> bool:
    """Increment user's daily counter. Returns True if the message is allowed.

    Automatically resets the counter when the date has changed.
    If DATABASE_URL is not set or DAILY_MESSAGE_LIMIT is not set, always allows.
    """
    limit_str = os.environ.get("DAILY_MESSAGE_LIMIT")
    conn = _get_conn()

    # No DB or no limit configured → unlimited
    if conn is None or not limit_str:
        return True

    daily_limit = int(limit_str)
    today = date.today()

    try:
        with conn, conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                "SELECT message_count, last_reset FROM user_message_counts WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()

            if row is None:
                # First message from this user
                cur.execute(
                    "INSERT INTO user_message_counts (user_id, message_count, last_reset) VALUES (%s, 1, %s)",
                    (user_id, today),
                )
                return True

            count = row["message_count"]
            last_reset = row["last_reset"]

            if last_reset < today:
                # New day — reset counter
                cur.execute(
                    "UPDATE user_message_counts SET message_count = 1, last_reset = %s WHERE user_id = %s",
                    (today, user_id),
                )
                return True

            if count >= daily_limit:
                return False

            cur.execute(
                "UPDATE user_message_counts SET message_count = message_count + 1 WHERE user_id = %s",
                (user_id,),
            )
            return True
    finally:
        conn.close()


def reset_counter(user_id: str) -> bool:
    """Reset a user's daily message counter. Returns True if user was found."""
    conn = _get_conn()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE user_message_counts SET message_count = 0, last_reset = %s WHERE user_id = %s",
                (date.today(), user_id),
            )
            return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dashboard: message persistence & queries
# ---------------------------------------------------------------------------

def save_message(phone: str, direction: str, body: str) -> None:
    """Persist a single message. Fails silently if no DB."""
    conn = _get_conn()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (phone, direction, body) VALUES (%s, %s, %s)",
                (phone, direction, body),
            )
    except Exception:
        logger.exception("Failed to save message")
    finally:
        conn.close()


def get_recent_messages(hours: int = 6) -> list[dict]:
    """Return messages from the last N hours, ordered by created_at ASC."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn, conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """SELECT phone, direction, body, created_at
                   FROM messages
                   WHERE created_at > NOW() - make_interval(hours => %s)
                   ORDER BY created_at ASC""",
                (hours,),
            )
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_recent_contact_count(hours: int = 6) -> int:
    """Return count of unique phone numbers in the last N hours."""
    conn = _get_conn()
    if conn is None:
        return 0
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(DISTINCT phone) FROM messages
                   WHERE created_at > NOW() - make_interval(hours => %s)""",
                (hours,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def cleanup_old_messages(hours: int = 6) -> int:
    """Delete messages older than N hours. Returns count deleted."""
    conn = _get_conn()
    if conn is None:
        return 0
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM messages WHERE created_at < NOW() - make_interval(hours => %s)",
                (hours,),
            )
            conn.commit()
            return cur.rowcount
    finally:
        conn.close()
