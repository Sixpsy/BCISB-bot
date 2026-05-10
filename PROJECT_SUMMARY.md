# BCISB Calendar Bot — Project Summary

## Overview
A Discord bot for parent communication in a school class. It displays an event calendar as an image directly in a Discord channel, manages events via slash commands, and automatically replaces old calendar posts with updated ones including a changelog.

## Current Status: Full Feature Bot

### What's Done

**Core Bot (`bot.py`)**
- Discord bot using `discord.py 2.3.2` with modern **slash commands** (`app_commands`)
- Guild-specific command sync (instant, no duplicates)
- Deferred responses to handle slow Playwright rendering without Discord timeout errors
- `.env` config for `DISCORD_TOKEN`, `CHANNEL_ID`, `RESOURCES_CHANNEL_ID`, `DRESS_CHANNEL_ID`
- Global error handler — friendly Thai error messages for all slash commands
- Welcome DM sent automatically when new members join the server

**Slash Commands — Parents**
- `/calendar` — Shows a **2-month calendar** (current + next month) as a side-by-side image, with ANSI-colored event list and "today" marker
- `/remind` — Personal DM reminder subscription with timing choices (1h, 3h, 1 day, 3 days, 1 week before)
- `/my-reminders` — View and cancel personal reminder subscriptions with dropdown
- `/help` — Shows available commands in Thai (parent commands for everyone, admin commands for Admin role only)

**Slash Commands — Admin Only**
- `/add-event` — Add event with date, category autocomplete, name, and optional detail
- `/remove-event` — Remove event or recurring series with autocomplete
- `/add-recurring` — Add recurring events (weekly/biweekly on a specific weekday)
- `/skip-event` — Skip a recurring event on a specific date (e.g. holidays)
- `/post-resource` — Paste email/app content manually as structured embed + thread
- `/set-dress-schedule` — Set default dress code for each weekday
- `/set-dress` — Set dress code override for a specific date
- `/post-dress` — Manually trigger dress code post

**Calendar Rendering (`calendar_render.py`)**
- Generates calendar images by rendering HTML → screenshot via **Playwright async API**
- Color-coded event categories loaded dynamically from `categories.json`
- Thai month names and weekday headers
- Single-month view (auto-post) and two-month side-by-side view (`/calendar`)
- ANSI-colored event list with category labels and details

**Daily Automation**
- **Daily calendar reminder** (06:00 UTC+7): Posts `@everyone` embed listing today's events. Skips holidays and weekends. Auto-deleted at midnight.
- **Daily dress code** (06:00 UTC+7): Posts today's and tomorrow's dress code. Skips holidays and weekends. Shows upcoming special dress days.
- **Monthly auto-post**: Posts calendar on the 1st of each month.
- **DM reminder checker**: Runs every 5 minutes, sends DMs when reminder time arrives.

**Reminder System**
- Type 1 — Daily channel `@everyone` embed at 06:00 UTC+7 (auto-deleted at midnight)
- Type 2 — Personal DM via `/remind` with timing choices, stored in `reminders.json`
- `/my-reminders` to view/cancel subscriptions with dropdown selector

**Resource Storage**
- Dedicated `#resources` channel
- `/post-resource` for manual text paste
- File upload listener: Admin drops PDF/image → auto-extract text → embed card + thread
- PDF text extraction via PyMuPDF
- Date detection in documents with buttons to add dates to calendar
- All resources indexed in `resources.json`

**Dress Code System**
- Weekly schedule (Mon-Fri defaults) stored in `dresscode.json`
- Date-specific overrides for special days
- Holiday-aware: skips holidays and shows "วันหยุด" instead of dress code
- Upcoming special dress days section

**Channel Cleanup**
- All calendar commands purge channel before posting
- Daily reminders auto-delete at midnight
- Raw file uploads deleted after processing

### Files

```
BCISB bot/
├── .env                    # DISCORD_TOKEN, CHANNEL_ID, RESOURCES_CHANNEL_ID, DRESS_CHANNEL_ID
├── .gitignore              # Protects .env and generated files from version control
├── bot.py                  # Main bot — all slash commands, tasks, event handlers
├── calendar_render.py      # HTML rendering + Playwright screenshot
├── categories.json         # Event categories (key, label, bg, text, ansi)
├── events.json             # One-off event data (date, name, category, detail)
├── recurring.json          # Recurring event definitions (auto-generated)
├── dresscode.json          # Weekly dress schedule + date overrides (auto-generated)
├── calendar_state.json     # Tracks message IDs for replace logic (auto-generated)
├── reminders.json          # Personal DM reminder subscriptions (auto-generated)
├── resources.json          # Index of all posted resources (auto-generated)
├── requirement.txt         # All Python dependencies
├── Dockerfile              # Docker deployment config
├── .dockerignore           # Docker build exclusions
├── calendar.png            # Generated single-month image (auto-generated)
└── calendar_2m.png         # Generated two-month image (auto-generated)
```

### How to Run

```bash
pip install -r requirement.txt
playwright install chromium
python bot.py
```

**Discord Developer Portal requirements:**
- Bot permissions: Send Messages, Attach Files, Manage Messages, Embed Links, Create Public Threads
- Scopes: `applications.commands`, `bot`
- Privileged Intents: **Server Members Intent** (required for welcome DM), **Message Content Intent**

---

## Nice-to-Have Improvements

- **Remind button on daily posts**: Add "🔔 แจ้งเตือนฉัน" buttons on daily event embeds so parents can subscribe without knowing `/remind`
- **Thai-friendly date input**: Accept `20/5/2569` or `20 พ.ค.` format in `/add-event`
- **AI Q&A (`/ask`)**: Answer parent questions from `resources.json` + `events.json` using LLM API
- **Event detail button**: "📋 ดูรายละเอียด" button under calendar image for full event details
- **Multi-day events**: Support date ranges (start_date + end_date)
- **Event editing**: `/edit-event` command to modify existing events without remove+add
- **Past event cleanup**: Automatic removal of past events from `events.json`
