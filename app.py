"""Flask app exposing a Twilio WhatsApp webhook with message buffering."""

import logging
import os
import re
import threading
import time

from dotenv import load_dotenv
from flask import Flask, request
from twilio.rest import Client as TwilioClient
from twilio.request_validator import RequestValidator

from chatbot import handle_message, append_human_exchange
from handoff import (
    is_owner,
    take_over,
    release,
    get_active_handoff,
    get_owner_handoff,
    touch_activity,
    list_active,
    cleanup_expired,
    _owner_numbers,
)
from db import init_db, check_and_increment, reset_counter

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
init_db()

# Message buffer: {user_number: {"messages": [str], "last_received": float}}
_buffer: dict[str, dict] = {}
_buffer_lock = threading.Lock()

# How long to wait (seconds) after the last message before processing
BUFFER_DELAY = float(os.environ.get("MESSAGE_BUFFER_SECONDS", "30"))

ESCALATION_PHRASE = "Let me get a team member to help you with that."


def _twilio_client() -> TwilioClient:
    return TwilioClient(
        os.environ["TWILIO_ACCOUNT_SID"],
        os.environ["TWILIO_AUTH_TOKEN"],
    )


def _validate_twilio_request() -> bool:
    """Validate that the incoming request is from Twilio."""
    validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])
    url = request.url
    if request.headers.get("X-Forwarded-Proto") == "https":
        url = url.replace("http://", "https://", 1)
    return validator.validate(
        url,
        request.form,
        request.headers.get("X-Twilio-Signature", ""),
    )


def _send_whatsapp_message(to: str, body: str, media_url: str | None = None) -> str:
    """Send a single WhatsApp message via the Twilio API. Returns the message SID."""
    client = _twilio_client()
    kwargs = {
        "from_": os.environ["TWILIO_WHATSAPP_NUMBER"],
        "to": to,
        "body": body or "",
    }
    if media_url:
        kwargs["media_url"] = [media_url]
    msg = client.messages.create(**kwargs)
    logger.info("Sent message to %s (SID: %s) body=%s media=%s", to, msg.sid, body[:100] if body else "", media_url or "")
    return msg.sid


def _wait_for_message_delivered(sid: str, timeout: float = 15.0) -> None:
    """Poll Twilio until the message reaches 'delivered' or a terminal state."""
    client = _twilio_client()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.messages(sid).fetch().status
        if status in ("delivered", "read", "failed", "undelivered"):
            return
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# Owner command handling
# ---------------------------------------------------------------------------

_TAKE_RE = re.compile(r"^TAKE\s+\+?(\d+)$", re.IGNORECASE)


def _normalize_customer_number(raw_digits: str) -> str:
    """Turn digits extracted from a TAKE command into whatsapp:+... format."""
    return f"whatsapp:+{raw_digits}"


def _handle_owner_message(owner: str, text: str) -> None:
    """Route an owner's message: either a command or a forwarded reply."""
    upper = text.strip().upper()

    # --- LIST command (always available) ---
    if upper == "LIST":
        active = list_active()
        if not active:
            _send_whatsapp_message(owner, body="No active hand-offs.")
        else:
            lines = ["Active hand-offs:"]
            for h in active:
                lines.append(f"- {h['customer']} (owner: {h['owner']})")
            _send_whatsapp_message(owner, body="\n".join(lines))
        return

    # --- TAKE command ---
    take_match = _TAKE_RE.match(text.strip())
    if take_match:
        customer = _normalize_customer_number(take_match.group(1))
        # Release current handoff if owner already has one
        current = get_owner_handoff(owner)
        if current:
            release(current)
            logger.info("Owner %s released %s (switching)", owner, current)

        ok = take_over(owner, customer)
        if ok:
            _send_whatsapp_message(
                owner,
                body=f"You're now chatting with {customer}.\nYour messages will be forwarded to them.\nSend DONE to hand back to AI.",
            )
            logger.info("Owner %s took over %s", owner, customer)
        else:
            _send_whatsapp_message(owner, body=f"{customer} is already taken over by another owner.")
        return

    # --- Owner has an active takeover ---
    current_customer = get_owner_handoff(owner)
    if current_customer:
        # DONE command: release
        if upper == "DONE":
            release(current_customer)
            _send_whatsapp_message(owner, body=f"Released {current_customer}. AI will resume.")
            _send_whatsapp_message(current_customer, body="You're back with our assistant. How can I help?")
            logger.info("Owner %s released %s", owner, current_customer)
            return

        # Otherwise forward the message to the customer
        touch_activity(current_customer)
        _send_whatsapp_message(current_customer, body=text)
        # Record in conversation history so AI has context later
        append_human_exchange(current_customer, customer_msg="", owner_reply=text)
        logger.info("Owner %s -> customer %s: %s", owner, current_customer, text[:100])
        return

    # --- No active takeover and not a recognized command ---
    _send_whatsapp_message(
        owner,
        body="Commands:\n- TAKE +<number> — take over a conversation\n- LIST — show active hand-offs\n- DONE — release current conversation",
    )


# ---------------------------------------------------------------------------
# Escalation notification
# ---------------------------------------------------------------------------

def _notify_owners_of_escalation(customer: str, last_message: str) -> None:
    """Send escalation notification to all owners."""
    for owner in _owner_numbers():
        _send_whatsapp_message(
            owner,
            body=f"Customer {customer} needs help.\nLast message: \"{last_message}\"\n\nReply: TAKE {customer.replace('whatsapp:', '')}",
        )
    logger.info("Escalation notification sent for %s", customer)


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------

def _process_and_reply(user_number: str, combined_text: str) -> None:
    """Process the combined message and send replies."""
    try:
        logger.info("Processing buffered message from %s: %s", user_number, combined_text)

        if not check_and_increment(user_number):
            logger.info("Daily message limit reached for %s", user_number)
            _send_whatsapp_message(
                user_number,
                body="You've reached your daily message limit. Please try again tomorrow.",
            )
            return

        messages = handle_message(user_id=user_number, user_text=combined_text)

        escalated = False
        for msg in messages:
            body = msg.get("body", "")
            image = msg.get("image", "")
            video = msg.get("video", "")

            if ESCALATION_PHRASE in body:
                escalated = True

            if video:
                sid = _send_whatsapp_message(user_number, body=body or " ", media_url=video)
                _wait_for_message_delivered(sid)
                time.sleep(3)
            elif image:
                sid = _send_whatsapp_message(user_number, body=body, media_url=image)
                _wait_for_message_delivered(sid)
            else:
                if body:
                    sid = _send_whatsapp_message(user_number, body=body)
                    _wait_for_message_delivered(sid)

        if escalated:
            _notify_owners_of_escalation(user_number, combined_text)

    except Exception:
        logger.exception("Error processing message for %s", user_number)


def _buffer_worker() -> None:
    """Background thread that checks for buffers ready to process."""
    last_cleanup = time.monotonic()
    while True:
        time.sleep(1)
        now = time.monotonic()
        ready: list[tuple[str, str]] = []

        with _buffer_lock:
            expired_keys = []
            for user_number, data in _buffer.items():
                if now - data["last_received"] >= BUFFER_DELAY:
                    combined = "\n".join(data["messages"])
                    ready.append((user_number, combined))
                    expired_keys.append(user_number)
            for key in expired_keys:
                del _buffer[key]

        for user_number, combined_text in ready:
            # Process each user in a separate thread so they don't block each other
            threading.Thread(
                target=_process_and_reply,
                args=(user_number, combined_text),
                daemon=True,
            ).start()

        # Periodic handoff expiry check (every 60 seconds)
        if now - last_cleanup >= 60:
            last_cleanup = now
            expired = cleanup_expired()
            for customer, owner in expired:
                logger.info("Auto-released handoff: %s (owner: %s)", customer, owner)
                try:
                    _send_whatsapp_message(owner, body=f"Chat with {customer} auto-released after inactivity.")
                    _send_whatsapp_message(customer, body="You're back with our assistant. How can I help?")
                except Exception:
                    logger.exception("Error sending auto-release notifications")


_worker_started = False
_worker_start_lock = threading.Lock()


def _ensure_worker_started() -> None:
    """Start the buffer worker thread once, on first request."""
    global _worker_started
    if _worker_started:
        return
    with _worker_start_lock:
        if _worker_started:
            return
        thread = threading.Thread(target=_buffer_worker, daemon=True)
        thread.start()
        _worker_started = True
        logger.info("Buffer worker thread started")


@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming WhatsApp messages from Twilio."""
    _ensure_worker_started()

    if os.environ.get("VALIDATE_TWILIO", "true").lower() == "true":
        if not _validate_twilio_request():
            return "Unauthorized", 403

    user_number = request.form.get("From", "")
    user_text = request.form.get("Body", "").strip()

    if not user_text:
        return "", 204

    # --- Owner messages: handle immediately (no buffering) ---
    if is_owner(user_number):
        threading.Thread(
            target=_handle_owner_message,
            args=(user_number, user_text),
            daemon=True,
        ).start()
        return "<Response></Response>", 200, {"Content-Type": "application/xml"}

    # --- Customer with active hand-off: forward to owner immediately ---
    handoff = get_active_handoff(user_number)
    if handoff:
        touch_activity(user_number)

        def _forward_to_owner():
            owner = handoff["owner"]
            _send_whatsapp_message(owner, body=f"[{user_number}]\n{user_text}")
            # Record customer message in history (owner reply recorded when owner responds)
            append_human_exchange(user_number, customer_msg=user_text, owner_reply="")

        threading.Thread(target=_forward_to_owner, daemon=True).start()
        return "<Response></Response>", 200, {"Content-Type": "application/xml"}

    # --- Normal customer flow: buffer and process with AI ---
    with _buffer_lock:
        if user_number in _buffer:
            _buffer[user_number]["messages"].append(user_text)
            _buffer[user_number]["last_received"] = time.monotonic()
            logger.info("Buffered message from %s (total: %d): %s",
                        user_number, len(_buffer[user_number]["messages"]), user_text)
        else:
            _buffer[user_number] = {
                "messages": [user_text],
                "last_received": time.monotonic(),
            }
            logger.info("New buffer for %s: %s", user_number, user_text)

    # Return immediately — processing happens in background
    return "<Response></Response>", 200, {"Content-Type": "application/xml"}


@app.route("/admin/reset-counter", methods=["POST"])
def admin_reset_counter():
    """Reset a user's daily message counter. Requires ADMIN_TOKEN."""
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token:
        return {"error": "ADMIN_TOKEN not configured"}, 503

    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {admin_token}":
        return {"error": "Unauthorized"}, 401

    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id", "")
    if not user_id:
        return {"error": "user_id is required"}, 400

    found = reset_counter(user_id)
    if found:
        logger.info("Admin reset message counter for %s", user_id)
        return {"status": "ok", "user_id": user_id}, 200
    else:
        return {"error": "User not found"}, 404


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
