# gcal-freedom-sync

Daemon that polls Google Calendar and automatically starts/stops [Freedom.to](https://freedom.to) blocking sessions. Sends Telegram notifications before blocks start and when they end. Answers natural language schedule questions via a Telegram bot backed by Claude.

## Features

- **Auto-blocking** — starts a Freedom session when a calendar event begins, stops it when the event ends
- **Locked mode** — enables Freedom's strong mode so blocks can't be bypassed
- **Pre-notifications** — Telegram messages at 10 min and 2 min before each block
- **Block end summary** — notifies which block completed and actual duration
- **Telegram bot** — ask anything: "what block am I in?", "what's coming up today?", etc.
- **Multiple users** — each user has their own Google Calendar and devices; all share one Freedom account

## How it works

```
Google Calendar ──poll every 60s──► daemon.py ──► Freedom.to API (start/stop session)
                                        │
                                        └──► Telegram bot (notifications + queries)
                                                    │
                                                    └──► Claude API (natural language)
```

The daemon runs on a Raspberry Pi (or any always-on Linux box) as a systemd service.

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/parthraut214/gcal-freedom-sync.git
cd gcal-freedom-sync
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Google Calendar credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com) → create a project
2. Enable the **Google Calendar API**
3. Create an OAuth 2.0 client ID (Desktop app) → download as `credentials.json`
4. Add yourself as a test user under OAuth consent screen
5. Run the daemon once locally — it will print an auth URL, paste the code back, and save `token_<name>.json`

### 3. Freedom.to session

Freedom has no public API. Session cookies are captured once via Playwright:

```bash
python freedom_client.py --setup-session
```

This opens Chrome, you log in to Freedom.to, then it saves `freedom_session.json`. Copy this to the Pi.

To find your **blocklist ID** and **device IDs**:

```bash
python freedom_client.py --sniff
```

Manually start a session in the browser — the API calls (including IDs) are printed to the terminal.

### 4. Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token
2. Start a chat with your new bot and send any message
3. Find your `chat_id`: call `https://api.telegram.org/bot<TOKEN>/getUpdates` and look for `message.chat.id`

### 5. Configure

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` — see inline comments. Each user needs:
- Their Google Calendar IDs (work, sleep, or any blocking calendar)
- Their Freedom device IDs
- Their Telegram `chat_id`
- Their own `google_token_path` and `state_path` (e.g. `token_alice.json`, `state_alice.json`)

### 6. Claude API (optional)

The Telegram bot uses Claude to answer arbitrary schedule questions. Set `ANTHROPIC_API_KEY` in your `.env` file (copy `.env.example`). Without it, the bot falls back to pattern-matched responses for common queries.

### 7. Deploy to the Pi

```bash
rsync -av --exclude='*.json' --exclude='.env' --exclude='venv' \
  ./ root@<PI_IP>:/opt/gcal-freedom-sync/

# Copy secrets separately
scp credentials.json freedom_session.json token_*.json .env \
  root@<PI_IP>:/opt/gcal-freedom-sync/
```

Install the systemd service:

```bash
cp systemd/gcal-freedom-sync.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gcal-freedom-sync
```

Logs: `journalctl -u gcal-freedom-sync -f` or `/var/log/gcal-freedom-sync.log`

## Adding a user

1. Have them complete the Google OAuth flow (they need their own `token_<name>.json`)
2. Get their Freedom device IDs via `--sniff`
3. Get their Telegram `chat_id` (they start the bot, you check `getUpdates`)
4. Add a block under `users:` in `config.yaml` and restart the service

## File reference

| File | Purpose |
|------|---------|
| `daemon.py` | Main loop: polls calendars, manages Freedom sessions, runs Telegram bot |
| `gcal_client.py` | Google Calendar API wrapper |
| `freedom_client.py` | Freedom.to HTTP API client + one-time session setup tool |
| `state.py` | Persists active block state to `state_<name>.json` |
| `config.example.yaml` | Config template (copy to `config.yaml` and fill in) |
| `.env.example` | Environment variable template |
| `systemd/` | systemd service unit file |
