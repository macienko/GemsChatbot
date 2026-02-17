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

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

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
        messages = handle_message(user_id=user_number, user_text=combined_text)

        for msg in messages:
            body = msg.get("body", "")
            image = msg.get("image", "")
            video = msg.get("video", "")

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
    except Exception:
        logger.exception("Error processing message for %s", user_number)


def _buffer_worker() -> None:
    """Background thread that checks for buffers ready to process."""
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


# Start the buffer worker thread
_worker_thread = threading.Thread(target=_buffer_worker, daemon=True)
_worker_thread.start()


@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming WhatsApp messages from Twilio."""
    if os.environ.get("VALIDATE_TWILIO", "true").lower() == "true":
        if not _validate_twilio_request():
            return "Unauthorized", 403

    user_number = request.form.get("From", "")
    user_text = request.form.get("Body", "").strip()

    if not user_text:
        return "", 204

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


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
