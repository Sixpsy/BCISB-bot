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
```

The JSON data files (`events.json`, `dresscode.json`, etc.) are mounted as Docker volumes — they survive rebuilds and must **never** be overwritten when pushing code.

SSH key is at `~/.ssh/id_ed25519_nas`. The NAS `sshd_config` has `PubkeyAuthentication yes` and `AuthorizedKeysFile /etc/ssh/authorized_keys/%u` (Synology's home dir is world-writable so the default `~/.ssh/authorized_keys` location is rejected by SSH).

## Architecture

Everything lives in two Python files:

- **`bot.py`** — 2,000+ line monolith containing all Discord slash commands, background tasks, event logic, resource processing, and reminder delivery.
- **`calendar_render.py`** — Renders a calendar as an HTML string, then takes a Playwright screenshot to produce a PNG. Called by `/calendar` and the monthly auto-post task.

### Data files (JSON, all hand-edited or written by the bot at runtime)

| File | Purpose |
|---|---|
| `events.json` | One-off events: `{date, name, cat, detail}` |
| `recurring.json` | Recurring events with weekday, frequency, and `excluded_dates` |
| `dresscode.json` | Weekly schedule by weekday index + date-specific overrides |
| `categories.json` | 4 event categories with display labels, hex colors, and ANSI codes |
| `reminders.json` | Active personal DM reminders with `remind_at` timestamps |
| `resources.json` | Index of posted resource embeds |
| `calendar_state.json` | Tracks posted calendar message IDs for editing/deleting |

### Background tasks (all times UTC+7)

| Task | Schedule | Action |
|---|---|---|
| `daily_channel_reminder` | 06:00 | Post `@everyone` embed of today's events |
| `delete_daily_reminder` | 00:00 | Delete the morning reminder from the previous day |
| `daily_dress_reminder` | 06:02 | Post today's + tomorrow's dress code |
| `monthly_calendar` | 06:05, 1st of month | Auto-post full calendar image |
| `check_dm_reminders` | Every 5 min | Poll `reminders.json` and DM users when `remind_at` is due |

### Holiday detection

`bot.py` has a `is_holiday(date)` helper that checks: (1) weekend, and (2) whether any event on that date has `cat == "holiday"`. Dress code and daily reminders both use this to suppress non-school-day output.

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

Neither command touches or purges the real channels.

## Known bugs fixed

- **`on_ready` fires on every Discord reconnect**, not just startup. All task loops are guarded with `if not loop.is_running(): loop.start()` to prevent duplicate loop instances from launching on reconnect.
- **`post_dress_code` race condition** — concurrent calls (e.g. from multiple loop instances) would each post a message then purge the others, leaving the channel empty. Fixed with `_dress_post_lock = asyncio.Lock()` so calls queue instead of racing.

## Key conventions

- **Slash commands** are guild-synced on startup (no global commands) for instant propagation.
- All `slow` operations (Playwright rendering, file processing) use `await interaction.response.defer()` before the work begins.
- Admin commands are gated by Discord role name `"Admin"` checked inside each command handler.
- Recurring events are stored as rules and expanded dynamically — they are never pre-materialized into `events.json`.
- Thai text is used for all parent-visible strings; English is used only for admin command names and internal logging.
