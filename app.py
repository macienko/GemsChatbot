"""Flask app exposing a Twilio WhatsApp webhook."""

import os
import time

from dotenv import load_dotenv
from flask import Flask, request
from twilio.rest import Client as TwilioClient
from twilio.request_validator import RequestValidator

from chatbot import handle_message

load_dotenv()

app = Flask(__name__)


def _twilio_client() -> TwilioClient:
    return TwilioClient(
        os.environ["TWILIO_ACCOUNT_SID"],
        os.environ["TWILIO_AUTH_TOKEN"],
    )


def _validate_twilio_request() -> bool:
    """Validate that the incoming request is from Twilio."""
    validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])
    url = request.url
    # Railway may terminate TLS; use X-Forwarded-Proto if present
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
    return msg.sid


def _wait_for_message_sent(sid: str, timeout: float = 10.0) -> None:
    """Poll Twilio until the message leaves 'queued'/'accepted' state."""
    client = _twilio_client()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.messages(sid).fetch().status
        if status not in ("queued", "accepted"):
            return
        time.sleep(0.5)



@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming WhatsApp messages from Twilio."""
    if os.environ.get("VALIDATE_TWILIO", "true").lower() == "true":
        if not _validate_twilio_request():
            return "Unauthorized", 403

    user_number = request.form.get("From", "")  # e.g. whatsapp:+1234567890
    user_text = request.form.get("Body", "").strip()

    if not user_text:
        return "", 204

    messages = handle_message(user_id=user_number, user_text=user_text)

    for msg in messages:
        body = msg.get("body", "")
        image = msg.get("image", "")
        video = msg.get("video", "")

        if video:
            sid = _send_whatsapp_message(user_number, body=body or " ", media_url=video)
            _wait_for_message_sent(sid)
        elif image:
            sid = _send_whatsapp_message(user_number, body=body, media_url=image)
            _wait_for_message_sent(sid)
        else:
            if body:
                sid = _send_whatsapp_message(user_number, body=body)
                _wait_for_message_sent(sid)

    # Return empty TwiML — we send messages via the REST API
    return "<Response></Response>", 200, {"Content-Type": "application/xml"}


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
