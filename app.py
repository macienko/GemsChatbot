"""Flask app exposing a Twilio WhatsApp webhook with message buffering."""

import logging
import os
import threading
import time

from dotenv import load_dotenv
from flask import Flask, request
from twilio.rest import Client as TwilioClient
from twilio.request_validator import RequestValidator

from chatbot import handle_message
from dashboard import DASHBOARD_HTML
from db import (
    init_db, check_and_increment, reset_counter,
    save_message, get_recent_messages, get_recent_contact_count,
    cleanup_old_messages,
)

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


def _process_and_reply(user_number: str, combined_text: str) -> None:
    """Process the combined message and send replies."""
    try:
        logger.info("Processing buffered message from %s: %s", user_number, combined_text)

        if not check_and_increment(user_number):
            logger.info("Daily message limit reached for %s", user_number)
            limit_msg = "You've reached your daily message limit. Please try again tomorrow."
            _send_whatsapp_message(user_number, body=limit_msg)
            save_message(user_number, "outgoing", limit_msg)
            return

        messages = handle_message(user_id=user_number, user_text=combined_text)

        for msg in messages:
            body = msg.get("body", "")
            image = msg.get("image", "")
            video = msg.get("video", "")

            if video:
                sid = _send_whatsapp_message(user_number, body=body or " ", media_url=video)
                _wait_for_message_delivered(sid)
                time.sleep(3)
                if body:
                    save_message(user_number, "outgoing", body)
            elif image:
                sid = _send_whatsapp_message(user_number, body=body, media_url=image)
                _wait_for_message_delivered(sid)
                if body:
                    save_message(user_number, "outgoing", body)
            else:
                if body:
                    sid = _send_whatsapp_message(user_number, body=body)
                    _wait_for_message_delivered(sid)
                    save_message(user_number, "outgoing", body)
    except Exception:
        logger.exception("Error processing message for %s", user_number)


def _buffer_worker() -> None:
    """Background thread that checks for buffers ready to process."""
    _cleanup_tick = 0
    while True:
        time.sleep(1)

        # Cleanup old dashboard messages every ~10 minutes
        _cleanup_tick += 1
        if _cleanup_tick >= 600:
            _cleanup_tick = 0
            try:
                deleted = cleanup_old_messages(hours=6)
                if deleted:
                    logger.info("Cleaned up %d old dashboard messages", deleted)
            except Exception:
                logger.exception("Error cleaning up old messages")

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

    save_message(user_number, "incoming", user_text)

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


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _check_dashboard_token():
    """Return (admin_token, error_response) — error_response is None if OK."""
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token:
        return None, ("Dashboard not configured", 503)
    token = request.args.get("token", "")
    if token != admin_token:
        return None, ("Unauthorized", 401)
    return admin_token, None


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Serve the conversation dashboard."""
    _, err = _check_dashboard_token()
    if err:
        return err
    return DASHBOARD_HTML, 200, {"Content-Type": "text/html"}


@app.route("/dashboard/api/messages", methods=["GET"])
def dashboard_api_messages():
    """Return recent messages as JSON."""
    _, err = _check_dashboard_token()
    if err:
        return err
    messages = get_recent_messages(hours=6)
    contact_count = get_recent_contact_count(hours=6)
    for m in messages:
        m["created_at"] = m["created_at"].isoformat()
    return {"contacts": contact_count, "messages": messages}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
