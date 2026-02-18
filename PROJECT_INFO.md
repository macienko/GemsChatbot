# GemChatBot - Project Information

## Overview

WhatsApp chatbot for B2B gemstone inventory search. Built with Python/Flask, deployed on Railway. Uses OpenAI gpt-4o with function calling and Google Sheets as the inventory database.

## Architecture

```
WhatsApp User
    ↓
Twilio (WhatsApp Business API)
    ↓ POST /webhook
Flask App (app.py)
    ├── Owner? → handle command or forward to customer
    ├── Customer with active hand-off? → forward to owner
    └── Normal customer → buffer → AI
            ↓
        chatbot.py → OpenAI gpt-4o (with function calling)
            ↓ (when model calls search_inventory)
        sheets.py → Google Sheets (public CSV export)
            ↓
        Twilio REST API → sends messages back
            ↓ (if AI escalates)
        handoff.py → notify owners
```

## Files

| File | Purpose |
|------|---------|
| `app.py` | Flask web server, Twilio webhook handler, message sending, owner routing |
| `chatbot.py` | OpenAI integration, conversation management, response parsing |
| `handoff.py` | Human hand-off state management (owner takeover of conversations) |
| `sheets.py` | Google Sheets inventory search via public CSV export |
| `db.py` | PostgreSQL helper for daily message limits |
| `prompt.md` | System prompt defining chatbot behavior |
| `requirements.txt` | Python dependencies |
| `Procfile` | Railway/Heroku process definition |
| `.env.example` | Template for environment variables |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key (sk-...) |
| `TWILIO_ACCOUNT_SID` | Twilio Account SID (AC...) |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token |
| `TWILIO_WHATSAPP_NUMBER` | Twilio WhatsApp sender (e.g. `whatsapp:+14155238886`) |
| `GOOGLE_SHEETS_ID` | Google Spreadsheet ID (from the URL) |
| `GOOGLE_SHEETS_GID` | Worksheet GID (default: `0` for first tab, visible in URL after `gid=`) |
| `MESSAGE_BUFFER_SECONDS` | Seconds to wait after last message before processing (default: `30`) |
| `VALIDATE_TWILIO` | Set to `false` to skip Twilio signature validation (dev only) |
| `OWNER_NUMBERS` | Comma-separated owner WhatsApp numbers for hand-off (e.g. `whatsapp:+1234567890`) |
| `HANDOFF_TIMEOUT_MINUTES` | Auto-release hand-off after inactivity (default: `30`) |
| `DATABASE_URL` | PostgreSQL connection string (auto-provided by Railway Postgres add-on) |
| `DAILY_MESSAGE_LIMIT` | Max messages per user per day (unset = unlimited) |
| `ADMIN_TOKEN` | Secret token for admin endpoints (reset counter, etc.) |
| `PORT` | Server port (Railway sets this automatically) |

## Google Sheets Setup

1. Open the Google Sheet
2. Click **Share** → **General access** → **Anyone with the link** → **Viewer**
3. Copy the spreadsheet ID from the URL: `https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit`
4. Set `GOOGLE_SHEETS_ID` to that ID
5. If using a tab other than the first, set `GOOGLE_SHEETS_GID` (visible in URL as `gid=...`)

### Expected Sheet Columns

Gemstone, Carat weight, Single/Pair, Shape, Origin, Treatment, Color, Clarity, Price per ct, Report, Link, Photo, Video

## Twilio Setup

1. Create a Twilio account at twilio.com
2. Activate the WhatsApp Sandbox (or connect a WhatsApp Business number)
3. Set the webhook URL to: `https://your-railway-domain.up.railway.app/webhook`
4. Method: POST

## Railway Deployment

1. Push code to a GitHub repo
2. Connect the repo in Railway
3. Set all environment variables in Railway dashboard
4. Railway auto-detects the Procfile and deploys

## Message Flow

For each gemstone result that has a video:
1. **Message 1**: Video media message (sent first)
2. **Message 2**: Text description + photo image (sent second)

For results without video:
1. **Single message**: Text description + photo (if available)

## Conversation Management

- Conversations are stored in-memory per phone number
- Greeting-only messages ("hi", "hello") reset the conversation
- Max 20 message pairs kept per user
- For production scaling, replace `_conversations` dict in `chatbot.py` with Redis

## Human Hand-off

Owners can take over conversations from the AI using WhatsApp commands sent to the same Twilio number.

### How it works

1. **Escalation**: When the AI can't help (out-of-scope requests), it tells the customer "Let me get a team member to help you with that." and notifies all owners via WhatsApp with the customer's number
2. **Takeover**: Owner sends `TAKE +<customer-number>` to start chatting directly with the customer
3. **Chatting**: Owner's messages are forwarded to the customer, customer's messages are forwarded to the owner (no buffering delay)
4. **Release**: Owner sends `DONE` to hand back to AI. The customer is notified and AI resumes with full context of the human conversation

### Owner commands

| Command | Description |
|---------|-------------|
| `TAKE +<number>` | Take over a customer conversation |
| `DONE` | Release current conversation back to AI |
| `LIST` | Show all active hand-offs |

### Key details

- Owner messages are never buffered (instant delivery)
- Customer messages during hand-off skip the AI and go directly to the owner
- Human conversation is recorded in the AI's history so it has context when resuming
- Hand-offs auto-release after 30 minutes of inactivity (configurable via `HANDOFF_TIMEOUT_MINUTES`)
- Multiple owners supported; each customer can only be taken over by one owner at a time

## Allowed Gemstones

Alexandrite, Amethyst, Apatite, Aquamarine, Beryl, Chrysoberyl, Citrine, Clinohumite, Emerald, Garnet, Heliodor, Kunzite, Moonstone, Morganite, Opal, Peridot, Ruby, Sapphire, Sphene, Spinel, Tanzanite, Topaz, Tourmaline, Rubellite, Paraiba, Zircon

## Changelog

> **Instructions for AI assistants:** After every code change to this project, add an entry below documenting what was changed, why, and any non-obvious details (gotchas, workarounds, design decisions). This log helps future sessions understand the current state of the codebase without re-reading everything. Keep entries concise but specific — mention file names, function names, and config values where relevant.

<!-- Add newest entries at the top -->

| Date | Summary | Files changed | Details |
|------|---------|---------------|---------|
| 2026-02-18 | Add daily message limit per user with PostgreSQL | `db.py` (new), `app.py`, `requirements.txt`, `.env.example` | New `user_message_counts` table tracks daily usage per phone number. Counter auto-resets each day. Limit configurable via `DAILY_MESSAGE_LIMIT` env var; unset = unlimited. Admin endpoint `POST /admin/reset-counter` (requires `ADMIN_TOKEN`) to manually reset a user. Gracefully degrades: if `DATABASE_URL` is not set, limits are disabled entirely. |
