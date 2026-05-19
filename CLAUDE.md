# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

BCISB Bot is a Discord bot for parent communication at an international school in Bangkok, Thailand. It manages a school calendar, dress code schedule, and personal reminders, with all parent-facing messages in Thai.

## Running the bot

```bash
# Install Python dependencies
pip install -r requirement.txt

# Install Playwright browser (required for calendar image rendering)
playwright install chromium

# Run
python bot.py
```

Docker alternative:
```bash
docker-compose up -d
```

Required `.env` variables:
```
DISCORD_TOKEN=
CHANNEL_ID=              # calendar channel
RESOURCES_CHANNEL_ID=    # resources channel
DRESS_CHANNEL_ID=        # dress code channel
```

No test suite or linter is currently configured.

## NAS deployment (Synology at 192.168.31.172)

The production bot runs in Docker on the NAS at `/volume1/docker/BCISB-bot/`.

**Restart after a code change:**
```bash
# 1. Push code to NAS (SCP and rsync don't work — use SSH stdin pipe per file)
ssh -i ~/.ssh/id_ed25519_nas sixpsy@192.168.31.172 'cat > /volume1/docker/BCISB-bot/bot.py' < bot.py

# 2. Rebuild and restart
ssh -i ~/.ssh/id_ed25519_nas sixpsy@192.168.31.172 "sudo /usr/local/bin/restart-bcisb-bot"

# 3. Tail container logs (default 50 lines, optional arg to set count)
ssh -i ~/.ssh/id_ed25519_nas sixpsy@192.168.31.172 "sudo /usr/local/bin/bcisb-bot-logs 200"
```

Note: Python's stdout is buffered inside the container, so `print()` lines may not
appear immediately. To get real-time output, add `PYTHONUNBUFFERED=1` to the
`environment:` section of `docker-compose.yml` and redeploy.

The JSON data files (`events.json`, `dresscode.json`, etc.) are mounted as Docker volumes — they survive rebuilds and must **never** be overwritten when pushing code.

SSH key is at `~/.ssh/id_ed25519_nas`. The NAS `sshd_config` has `PubkeyAuthentication yes` and `AuthorizedKeysFile /etc/ssh/authorized_keys/%u` (Synology's home dir is world-writable so the default `~/.ssh/authorized_keys` location is rejected by SSH).

## Architecture

Everything lives in two Python files:

- **`bot.py`** — 2,000+ line monolith containing all Discord slash commands, background tasks, event logic, resource processing, and reminder delivery.
- **`calendar_render.py`** — Renders a calendar as an HTML string, then takes a Playwright screenshot to produce a PNG. Called by `/calendar` and the monthly auto-post task.

### Data files (JSON, all hand-edited or written by the bot at runtime)

| File | Purpose |
|---|---|
| `events.json` | One-off events: `{date, name, cat, detail, end_date}` (end_date optional, for multi-day events) |
| `recurring.json` | Recurring events with weekday, frequency, and `excluded_dates` |
| `dresscode.json` | Weekly schedule by weekday index + date-specific overrides |
| `categories.json` | 4 event categories with display labels, hex colors, and ANSI codes |
| `reminders.json` | Active personal DM reminders with `remind_at` timestamps |
| `resources.json` | Index of posted resource embeds |
| `calendar_state.json` | Tracks posted calendar message IDs for editing/deleting |

### Background tasks (all times UTC+7)

| Task | Schedule | Action |
|---|---|---|
| `daily_calendar_school` | 06:00 | Re-render the 2-month calendar AND post `@everyone` today's-events embed below it. Skips if today is a real holiday/weekend. |
| `daily_calendar_holiday` | 09:00 | Re-render the 2-month calendar. Skips if today is a school day (incl. holiday dates with a non-holiday event — `daily_calendar_school` covers those). |
| `delete_daily_reminder` | 00:00 | Delete yesterday's events embed from the calendar channel |
| `daily_dress_reminder` | 06:02 | Post today's + tomorrow's dress code |
| `check_dm_reminders` | Every 5 min | Poll `reminders.json` and DM users when `remind_at` is due |

Both `daily_calendar_*` loops share a `state["last_calendar_post"] = "YYYY-MM-DD"`
idempotency key, and `on_ready` runs a catch-up post if that key is older than
today's BKK date (covers restarts after a missed window).

### Holiday detection

`bot.py` has an `is_holiday_or_weekend(date) -> (bool, name)` helper that returns `(True, holiday_name)` when: (1) any event that day has `cat == "holiday"`, or (2) the day is a weekend — UNLESS there's a non-holiday event scheduled that day (e.g. a Saturday activity), in which case it returns `(False, "")` and the day is treated as a school day. Dress code and daily reminders both use this to suppress non-school-day output.

### Calendar rendering pipeline

`/calendar` and the monthly task both call `calendar_render.py`, which: builds an HTML grid with Thai month names and weekday headers → injects events as colored cells → uses Playwright (headless Chromium) to screenshot the page → returns PNG bytes sent as a Discord attachment.

### Resource processing

When files are uploaded to the resources channel, the bot extracts text from PDFs (PyMuPDF) or images, detects date patterns in the text, and surfaces clickable buttons so admins can add detected dates directly to the calendar.

## Test channel

A private channel (ID `1503578584961515691`, not visible to parents) exists for testing bot output without affecting live channels. Two admin-only commands post to it:

| Command | What it tests |
|---|---|
| `/test-dress` | Dress code embed (grey colour, `[TEST]` footer) |
| `/test-calendar` | 2-month calendar image + event list (grey colour, `[TEST]` title) |
| `/test-agenda` | 14-day agenda embed (grey colour, `[TEST]` title) |

None of these commands touch or purge the real channels.

## Known bugs fixed

- **`on_ready` fires on every Discord reconnect**, not just startup. All task loops are guarded with `if not loop.is_running(): loop.start()` to prevent duplicate loop instances from launching on reconnect.
- **`post_dress_code` race condition** — concurrent calls (e.g. from multiple loop instances) would each post a message then purge the others, leaving the channel empty. Fixed with `_dress_post_lock = asyncio.Lock()` so calls queue instead of racing.

## Key conventions

- **Slash commands** are guild-synced on startup (no global commands) for instant propagation.
- All `slow` operations (Playwright rendering, file processing) use `await interaction.response.defer()` before the work begins.
- Admin commands are gated by Discord role name `"Admin"` checked inside each command handler.
- Recurring events are stored as rules and expanded dynamically — they are never pre-materialized into `events.json`.
- Thai text is used for all parent-visible strings; English is used only for admin command names and internal logging.
