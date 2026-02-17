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
    ↓
chatbot.py → OpenAI gpt-4o (with function calling)
    ↓ (when model calls search_inventory)
sheets.py → Google Sheets (public CSV export)
    ↓
Twilio REST API → sends individual messages back to user
```

## Files

| File | Purpose |
|------|---------|
| `app.py` | Flask web server, Twilio webhook handler, message sending |
| `chatbot.py` | OpenAI integration, conversation management, response parsing |
| `sheets.py` | Google Sheets inventory search via public CSV export |
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
| `VALIDATE_TWILIO` | Set to `false` to skip Twilio signature validation (dev only) |
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

## Allowed Gemstones

Alexandrite, Amethyst, Apatite, Aquamarine, Beryl, Chrysoberyl, Citrine, Clinohumite, Emerald, Garnet, Heliodor, Kunzite, Moonstone, Morganite, Opal, Peridot, Ruby, Sapphire, Sphene, Spinel, Tanzanite, Topaz, Tourmaline, Rubellite, Paraiba, Zircon
