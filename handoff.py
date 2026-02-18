"""Human hand-off state management for WhatsApp conversations."""

import os
import threading
import time

# Active handoffs: {customer_number: {"owner": str, "started_at": float, "last_activity": float}}
_handoffs: dict[str, dict] = {}
_handoff_lock = threading.Lock()

# Auto-release timeout in seconds
HANDOFF_TIMEOUT = float(os.environ.get("HANDOFF_TIMEOUT_MINUTES", "30")) * 60


def _owner_numbers() -> set[str]:
    """Return the set of owner WhatsApp numbers from env."""
    raw = os.environ.get("OWNER_NUMBERS", "")
    if not raw:
        return set()
    return {n.strip() for n in raw.split(",") if n.strip()}


def is_owner(number: str) -> bool:
    return number in _owner_numbers()


def take_over(owner: str, customer: str) -> bool:
    """Mark a customer conversation as human-controlled by owner.

    Returns False if the customer is already taken over by another owner.
    """
    now = time.monotonic()
    with _handoff_lock:
        existing = _handoffs.get(customer)
        if existing and existing["owner"] != owner:
            return False
        _handoffs[customer] = {
            "owner": owner,
            "started_at": now,
            "last_activity": now,
        }
    return True


def release(customer: str) -> None:
    """Release a customer conversation back to AI."""
    with _handoff_lock:
        _handoffs.pop(customer, None)


def get_active_handoff(customer: str) -> dict | None:
    """Return handoff info for a customer, or None if not active."""
    with _handoff_lock:
        return _handoffs.get(customer)


def get_owner_handoff(owner: str) -> str | None:
    """Return the customer number the owner is currently chatting with, or None."""
    with _handoff_lock:
        for customer, data in _handoffs.items():
            if data["owner"] == owner:
                return customer
    return None


def touch_activity(customer: str) -> None:
    """Update last_activity timestamp for a handoff."""
    with _handoff_lock:
        if customer in _handoffs:
            _handoffs[customer]["last_activity"] = time.monotonic()


def list_active() -> list[dict]:
    """Return all active handoffs."""
    with _handoff_lock:
        return [
            {"customer": customer, "owner": data["owner"]}
            for customer, data in _handoffs.items()
        ]


def cleanup_expired() -> list[tuple[str, str]]:
    """Release handoffs idle longer than timeout.

    Returns list of (customer, owner) tuples that were released.
    """
    now = time.monotonic()
    expired = []
    with _handoff_lock:
        for customer, data in list(_handoffs.items()):
            if now - data["last_activity"] >= HANDOFF_TIMEOUT:
                expired.append((customer, data["owner"]))
                del _handoffs[customer]
    return expired
