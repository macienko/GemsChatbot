"""Human hand-off state management for WhatsApp conversations.

Uses PostgreSQL for persistence so hand-offs survive server restarts.
Falls back to in-memory storage if DATABASE_URL is not set.
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import DictCursor

logger = logging.getLogger(__name__)

_DATABASE_URL: str | None = os.environ.get("DATABASE_URL")

# Auto-release timeout in minutes
HANDOFF_TIMEOUT_MINUTES = float(os.environ.get("HANDOFF_TIMEOUT_MINUTES", "30"))


def _owner_numbers() -> set[str]:
    """Return the set of owner WhatsApp numbers from env."""
    raw = os.environ.get("OWNER_NUMBERS", "")
    if not raw:
        return set()
    return {n.strip() for n in raw.split(",") if n.strip()}


def is_owner(number: str) -> bool:
    return number in _owner_numbers()


def _get_conn():
    if not _DATABASE_URL:
        return None
    return psycopg2.connect(_DATABASE_URL)


# ---------------------------------------------------------------------------
# In-memory fallback (used when DATABASE_URL is not set)
# ---------------------------------------------------------------------------

_handoffs: dict[str, dict] = {}
_handoff_lock = threading.Lock()


def _mem_take_over(owner: str, customer: str) -> bool:
    now = time.monotonic()
    with _handoff_lock:
        existing = _handoffs.get(customer)
        if existing and existing["owner"] != owner:
            return False
        _handoffs[customer] = {"owner": owner, "started_at": now, "last_activity": now}
    return True


def _mem_release(customer: str) -> None:
    with _handoff_lock:
        _handoffs.pop(customer, None)


def _mem_get_active_handoff(customer: str) -> dict | None:
    with _handoff_lock:
        return _handoffs.get(customer)


def _mem_get_owner_handoff(owner: str) -> str | None:
    with _handoff_lock:
        for customer, data in _handoffs.items():
            if data["owner"] == owner:
                return customer
    return None


def _mem_touch_activity(customer: str) -> None:
    with _handoff_lock:
        if customer in _handoffs:
            _handoffs[customer]["last_activity"] = time.monotonic()


def _mem_list_active() -> list[dict]:
    with _handoff_lock:
        return [
            {"customer": c, "owner": d["owner"]}
            for c, d in _handoffs.items()
        ]


def _mem_cleanup_expired() -> list[tuple[str, str]]:
    timeout = HANDOFF_TIMEOUT_MINUTES * 60
    now = time.monotonic()
    expired = []
    with _handoff_lock:
        for customer, data in list(_handoffs.items()):
            if now - data["last_activity"] >= timeout:
                expired.append((customer, data["owner"]))
                del _handoffs[customer]
    return expired


# ---------------------------------------------------------------------------
# PostgreSQL implementations
# ---------------------------------------------------------------------------

def _db_take_over(owner: str, customer: str) -> bool:
    conn = _get_conn()
    try:
        with conn, conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT owner FROM handoffs WHERE customer = %s", (customer,))
            row = cur.fetchone()
            if row and row["owner"] != owner:
                return False
            cur.execute("""
                INSERT INTO handoffs (customer, owner)
                VALUES (%s, %s)
                ON CONFLICT (customer)
                DO UPDATE SET owner = EXCLUDED.owner, started_at = NOW(), last_activity = NOW()
            """, (customer, owner))
        return True
    finally:
        conn.close()


def _db_release(customer: str) -> None:
    conn = _get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM handoffs WHERE customer = %s", (customer,))
    finally:
        conn.close()


def _db_get_active_handoff(customer: str) -> dict | None:
    conn = _get_conn()
    try:
        with conn, conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT owner, started_at, last_activity FROM handoffs WHERE customer = %s", (customer,))
            row = cur.fetchone()
            if row:
                return {"owner": row["owner"], "started_at": row["started_at"], "last_activity": row["last_activity"]}
            return None
    finally:
        conn.close()


def _db_get_owner_handoff(owner: str) -> str | None:
    conn = _get_conn()
    try:
        with conn, conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT customer FROM handoffs WHERE owner = %s LIMIT 1", (owner,))
            row = cur.fetchone()
            return row["customer"] if row else None
    finally:
        conn.close()


def _db_touch_activity(customer: str) -> None:
    conn = _get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE handoffs SET last_activity = NOW() WHERE customer = %s", (customer,))
    finally:
        conn.close()


def _db_list_active() -> list[dict]:
    conn = _get_conn()
    try:
        with conn, conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT customer, owner FROM handoffs")
            return [{"customer": row["customer"], "owner": row["owner"]} for row in cur.fetchall()]
    finally:
        conn.close()


def _db_cleanup_expired() -> list[tuple[str, str]]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=HANDOFF_TIMEOUT_MINUTES)
    conn = _get_conn()
    try:
        with conn, conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                "DELETE FROM handoffs WHERE last_activity < %s RETURNING customer, owner",
                (cutoff,),
            )
            return [(row["customer"], row["owner"]) for row in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API — dispatches to DB or in-memory based on DATABASE_URL
# ---------------------------------------------------------------------------

def take_over(owner: str, customer: str) -> bool:
    """Mark a customer conversation as human-controlled by owner.

    Returns False if the customer is already taken over by another owner.
    """
    if _DATABASE_URL:
        return _db_take_over(owner, customer)
    return _mem_take_over(owner, customer)


def release(customer: str) -> None:
    """Release a customer conversation back to AI."""
    if _DATABASE_URL:
        _db_release(customer)
    else:
        _mem_release(customer)


def get_active_handoff(customer: str) -> dict | None:
    """Return handoff info for a customer, or None if not active."""
    if _DATABASE_URL:
        return _db_get_active_handoff(customer)
    return _mem_get_active_handoff(customer)


def get_owner_handoff(owner: str) -> str | None:
    """Return the customer number the owner is currently chatting with, or None."""
    if _DATABASE_URL:
        return _db_get_owner_handoff(owner)
    return _mem_get_owner_handoff(owner)


def touch_activity(customer: str) -> None:
    """Update last_activity timestamp for a handoff."""
    if _DATABASE_URL:
        _db_touch_activity(customer)
    else:
        _mem_touch_activity(customer)


def list_active() -> list[dict]:
    """Return all active handoffs."""
    if _DATABASE_URL:
        return _db_list_active()
    return _mem_list_active()


def cleanup_expired() -> list[tuple[str, str]]:
    """Release handoffs idle longer than timeout.

    Returns list of (customer, owner) tuples that were released.
    """
    if _DATABASE_URL:
        return _db_cleanup_expired()
    return _mem_cleanup_expired()
