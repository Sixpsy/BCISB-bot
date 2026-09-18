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

# PowerSchool relay (optional — feature is inert if POWERSCHOOL_CHANNEL_ID is unset)
POWERSCHOOL_CHANNEL_ID=  # private channel for homeroom-teacher messages
POWERSCHOOL_USER=        # guardian portal username
POWERSCHOOL_PASS=        # guardian portal password
POWERSCHOOL_URL=         # optional, defaults to https://bcisb.powerschool.com
POWERSCHOOL_HUB_URL=     # optional, defaults to https://bcisb.guardian.powerschool.com
POWERSCHOOL_TEACHER=     # optional, defaults to "Randy" — matched on message author
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

**`%u` expands to `Sixpsy`, with a capital S.** You log in as `sixpsy`, but the
account's canonical name is `Sixpsy` (`whoami` confirms it, and files under
`/volume1` are owned by `Sixpsy`). Both `/etc/ssh/authorized_keys/Sixpsy` and
`…/sixpsy` exist; **only the capital one is ever read**. A key appended to the
lowercase file is silently ignored — the handshake shows the key being offered
and rejected with no hint as to why. Appending needs `sudo`, which is not
passwordless; only the two `/usr/local/bin/` wrapper scripts are.

## Architecture

Everything lives in two Python files:

- **`bot.py`** — 2,000+ line monolith containing all Discord slash commands, background tasks, event logic, resource processing, and reminder delivery.
- **`calendar_render.py`** — Renders a calendar as an HTML string, then takes a Playwright screenshot to produce a PNG. Called by `/calendar` and the monthly auto-post task.
- **`powerschool.py`** — Playwright login to the BCISB PowerSchool guardian portal plus an `explore` CLI for dumping the authenticated portal. See the PowerSchool relay section below.
- **`canva_fetch.py`** — Captures a public Canva view link page-by-page (`python canva_fetch.py <url> <outdir>`). Imported by `canva_worker.py`, **not** by `bot.py`.
- **`canva_worker.py`** — Runs on the **Mac mini**, not the NAS. Drains the bot's Canva queue over SSH, renders each design, and writes the pages into Synology Photos. See the Canva capture section below.

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
| `powerschool_state.json` | Hashes of already-relayed PowerSchool messages (de-dup) + `last_check` + `canva_seen` (Canva URLs already queued) |
| `canva_queue.json` | Canva links awaiting capture by `canva_worker.py` on the Mac mini |

### Background tasks (all times UTC+7)

| Task | Schedule | Action |
|---|---|---|
| `daily_calendar_school` | 06:00 | Re-render the 2-month calendar AND post `@everyone` today's-events embed below it. Skips if today is a real holiday/weekend. |
| `daily_calendar_holiday` | 09:00 | Re-render the 2-month calendar. Skips if today is a school day (incl. holiday dates with a non-holiday event — `daily_calendar_school` covers those). |
| `delete_daily_reminder` | 00:00 | Delete yesterday's events embed from the calendar channel |
| `daily_dress_reminder` | 06:02 | Post today's + tomorrow's dress code |
| `check_dm_reminders` | Every 5 min | Poll `reminders.json` and DM users when `remind_at` is due |
| `daily_powerschool_check` | Hourly, :15 past | Poll PowerSchool for new homeroom-teacher messages, relay them to the private channel, and queue any linked Canva designs for capture |

Both `daily_calendar_*` loops share a `state["last_calendar_post"] = "YYYY-MM-DD"`
idempotency key, and `on_ready` runs a catch-up post if that key is older than
today's BKK date (covers restarts after a missed window).

### Holiday detection

`bot.py` has an `is_holiday_or_weekend(date) -> (bool, name)` helper that returns `(True, holiday_name)` when: (1) any event that day has `cat == "holiday"`, or (2) the day is a weekend — UNLESS there's a non-holiday event scheduled that day (e.g. a Saturday activity), in which case it returns `(False, "")` and the day is treated as a school day. Dress code and daily reminders both use this to suppress non-school-day output.

### Calendar rendering pipeline

`/calendar` and the monthly task both call `calendar_render.py`, which: builds an HTML grid with Thai month names and weekday headers → injects events as colored cells → uses Playwright (headless Chromium) to screenshot the page → returns PNG bytes sent as a Discord attachment.

### Resource processing

When files are uploaded to the resources channel, the bot extracts text from PDFs (PyMuPDF) or images, detects date patterns in the text, and surfaces clickable buttons so admins can add detected dates directly to the calendar.

## PowerSchool relay

Relays the homeroom teacher's messages into a private Discord channel, checked
daily at 18:00 BKK. `powerschool.py` does the reading; `bot.py` does the
scheduling, de-duplication and posting. `/check-powerschool` (Admin) runs it
on demand.

### Where the messages actually are

Not in the classic `/guardian/` portal — that is why they cannot be found
there. They are in **PowerSchool Messaging**, a Sendbird chat surfaced through
MyPowerHub. The route, confirmed against the live app on 2026-08-28:

1. Sign in to the SIS portal at `https://bcisb.powerschool.com`.
2. Go to `https://bcisb.guardian.powerschool.com` (MyPowerHub) — SSO off the
   same session, no second login.
3. Click the header `button[aria-label="Messaging"]`. It is a **button, not a
   link**, which is why it never turns up in a nav-link scan.
4. Expand the **"Classes" accordion** — it starts `aria-expanded="false"` and
   the conversations are not in the DOM until it is opened. Missing this step
   makes the inbox look empty. Two measured gotchas here:
   - Its accessible name is **`"Classes0 unread messages"`**, not `"Classes"`,
     so `get_by_role("button", name="Classes", exact=True)` never matches. The
     unread suffix also disappears once expanded, so anchor on the prefix.
     `_EXPAND_GROUPS_JS` matches `innerText` in JS instead.
   - The pane is a lazily-loaded micro-frontend: the accordion appears roughly
     **8 seconds** after the Messaging click on a cold browser context, so
     `_open_messaging` polls to a deadline rather than waiting once.
5. Click a conversation; the message list renders and is readable.

Sendbird's REST API (`api.messenger-inbox.mfe.powerschool.com/api/user/session`
→ `api-<appid>.sendbird.com/v3/...`) sits behind this and would be sturdier
than the DOM, but it means handling a third-party session token. The DOM inside
the already-authenticated Playwright session needs no extra credentials.

### Confirmed selectors (`powerschool.py`)

| What | Selector |
|---|---|
| Message list | `.messenger-inbox__conversation__message-list` |
| One message | `.messenger-inbox__message-content` |
| Sender name | `.messenger-inbox__message-content__middle__sender-name` |
| Body | `.messenger-inbox__message-content__middle__body-container` |
| Timestamp | `…__body-container__created-at` |
| Date separator | `.messenger-inbox__conversation__date-separator__label` |
| System message | `.messenger-inbox__admin-message`, `.sendbird-admin-message` |
| Conversation row | `button.messenger-inbox__messenger-channel-preview__button` |

A third trap, in the conversation list. The row is laid out as:

```
div.messenger-inbox__messenger-channel-preview
  div.messenger-channel-preview                             <- the preview
  button.messenger-inbox__messenger-channel-preview__button  <- SIBLING
```

The button is a **sibling** of the preview, not its ancestor, so the obvious
`button:has(.messenger-channel-preview)` matches nothing — measured 0 hits
against the live DOM. Use the explicit button class.

Two more traps the extraction handles, both verified against the live DOM:

- **Sender names appear only on the first message of a consecutive run** by the
  same person, so the last seen name is carried forward. Reading each bubble
  independently leaves later messages with an empty author, and they then fail
  the teacher filter.
- **Date separators are relative** ("Today", "Yesterday"). They are normalised
  to an absolute date before hashing, because otherwise the same message gets a
  new `message_id()` when the label rolls over and is relayed again every day.

The teacher renders as `Randy Allen Hudson Jr.` (classic portal:
`Hudson Jr., Randy Allen`, randyhudson@bcisb.ac.th), so the default
`POWERSCHOOL_TEACHER=Randy` matches. Matching is on the author field only.

### Login success cannot be detected from the URL

Verified with a bogus account: a *rejected* sign-in also ends up at
`https://bcisb.powerschool.com/guardian/home.html` (the form's own action),
with the form re-rendered and the text "Invalid Username or Password!".
`_login()` therefore requires both a `/guardian/` URL *and* the absence of
`input[name='pw']`. Do not "simplify" this back to a URL check — the failure
would be invisible, showing up only as `posted 0 new message(s)` every night.

### Other notes

- Only the recently-rendered messages are read; the chat list is virtualised
  and history is not scrolled back. Fine for a notification relay.
- First run posts only the newest `PS_FIRST_RUN_LIMIT` (3) messages and marks
  older ones seen, so enabling the feature does not dump a term's backlog.
- `powerschool_state.json` **must** exist as a file on the NAS before
  `docker-compose up`, or Docker creates a directory at the mount point.
- The PowerSchool REST API is enabled on the instance but needs a plugin
  `client_id`/`secret` from a school admin. If BCISB will issue those, it beats
  scraping.
- Single student assumed. MyPowerHub has a student switcher; a second child
  would need iterating it.
- `bot.py` calls `client.run(TOKEN)` at module level with no
  `if __name__ == "__main__"` guard, so it **cannot be imported** for testing —
  importing it connects a second live bot and starts every daily loop. Test
  `powerschool.py` standalone, and the Discord side via `/check-powerschool`.
- Verified end-to-end on 2026-08-28: 2 messages fetched, authors correct,
  timestamps `2026-08-24T17:00:00+07:00` / `2026-08-28T12:44:00+07:00`,
  message ids identical across two consecutive runs (de-dup holds).
- `python powerschool.py explore` still exists as a discovery tool: it logs in
  and dumps the classic portal's nav and page text (desktop + mobile) to
  `./ps_dump/`.
- **`posted_at` is hashed into `message_id()`**, so its stored ISO form must
  never be "prettified" — changing it changes every id and re-relays the whole
  backlog. The embed footer is formatted for display only, by
  `_fmt_posted_at()` in `bot.py`, which reuses `fmt_thai_date` →
  `PowerSchool · วันศุกร์ที่ 28 สิงหาคม 2569 12:44 น.`. It checks for a `T`
  before parsing because `datetime.fromisoformat("2026-08-24")` returns
  midnight, which would show a `00:00 น.` the portal never reported;
  `_compose_posted_at` legitimately returns a bare date when it cannot parse
  the time label.

## Canva capture (`canva_fetch.py`)

The teacher's messages carry raw Canva links (which is why `_subject_from_body`
has to skip naked URLs when deriving a title). Every page of each linked design
is captured and written to Synology Photos as one dated folder per newsletter,
alongside a PDF (`canva_fetch.to_pdf`) built from those same page images —
embedded at full captured resolution, no re-encode, so it costs no detail
relative to the pages themselves:

```
/volume1/photo/BCISB Newsletters/2026-08-24 Weekly Newsletter/page1.jpg …
/volume1/photo/BCISB Newsletters/2026-08-24 Weekly Newsletter/design.pdf
```

### Why the work is split across two machines

`bot.py` finds Canva links in relayed messages and appends them to
`canva_queue.json`. It **never renders Canva itself**: a capture peaks near
1 GB of Chromium and the Synology has ~1.7 GB free, so adding that to the box
running the calendar and dress-code posts risks the host OOM-killer reaping the
whole container. `canva_worker.py` on the Mac mini drains the queue:

```bash
python3 canva_worker.py            # drain the queue
python3 canva_worker.py --dry-run  # show what would be captured
python3 canva_worker.py --once URL --posted-at 2026-08-24   # one-off backfill
```

A URL is marked seen in `canva_seen` when **queued**, so it is never queued
twice; a failed capture stays in the queue and the next run retries it. The
worker re-reads the queue before removing finished entries, so a link the bot
adds mid-render is not lost. Files reach the NAS through `ssh 'cat > path'` —
this NAS rejects scp and rsync.

Facts measured against the live viewer on 2026-09-05:

- **There is no API and no download.** Canva's Connect API is OAuth-scoped to
  designs the authenticated account owns, so a teacher's share link is
  unreachable. The `More` menu on a public view link offers no Download item.
  Screenshot capture is the only route.
- **The `og:image` shortcut is dead.** `…/screen` returns **Cloudflare 403 to
  every plain HTTP client** — browser UA, Discordbot, Twitterbot,
  facebookexternalhit all included. Only a real browser session gets through,
  so don't "optimise" this into a `requests.get`.
- **Anchor on `aria-label`, never class names.** Classes are hashed
  (`m_U7nQ`, `_8jGYJw`) and change on every Canva deploy. The stable handles are
  `[aria-label="Next page"]` and `[aria-label="Go to page"]`, the latter's text
  reading `1 / 3` — that is where the page count comes from.
- **Don't use "Hide controls".** It removes the nav buttons from the DOM, so
  paging then silently captures page 1 N times. Instead the viewport is much
  *wider* than the page, which puts the chrome outside the page's box; the clip
  excludes it while `Next page` still works.
- **The page is located by geometry**, skipping only boxes that fill *both*
  axes (the app root). Guarding on width alone breaks landscape designs, which
  fit by width. Only A4 portrait has been tested; the code logs loudly rather
  than shipping a silent chrome-baked capture.
- **Resolution is the quality knob, not JPEG quality.** Canva serves photo
  resolution proportional to *device* pixels (CSS size × DPR), so rendering
  bigger fetches genuinely larger sources. Measuring source-px per captured-px
  across the page's photos, the median is 1.07 / 1.00 / 0.97 at captures of
  3394×4800 / 4526×6400 / 5656×8000 — past ~4500×6400 it is pure upscaling.
  Hence the `max` preset. PNG is pointless here: it produced a 73 MB PDF where
  JPEG q95 is visually identical at 14 MB.
- **Budget ~1 GB RAM per capture.** Measured peak RSS of the chromium tree was
  ~911 MB (`max`) and ~1036 MB (`high`) — the presets change output size, *not*
  memory, and are not even ordered the way you would expect. Do not "optimise"
  this back onto the NAS by dropping to a smaller preset; it would not help.
- `--disable-dev-shm-usage` is required: Docker's default 64 MB `/dev/shm`
  cannot hold a 4500×6400 raster and the tab dies without a useful error.
- **Wait for images to stop upgrading, not a fixed delay.** Canva paints a tiny
  blurred placeholder and swaps in the full photo a beat later, so a fixed
  `PAGE_MS` sleep can capture the placeholder — one photo on a page came out
  unrecognisably blurred exactly this way. `_wait_for_images` polls until every
  visible image reports `complete` and the total decoded pixel count stops
  growing, and warns when an image is still under 0.35× its displayed size.

### Dating for Synology Photos

A Playwright screenshot carries no EXIF, so Synology Photos would file every
page under its upload date. `canva_worker.stamp_date` writes the newsletter's
own date into `DateTimeOriginal`, and the file's mtime is set to match as a
fallback.

**Use piexif, not Pillow.** Re-saving through Pillow — even with
`quality="keep", subsampling="keep"` — re-encodes: the pixels came back
measurably different and the file grew 3.5%. piexif rewrites only the APP1
segment, verified byte-identical for +1 KB. Pillow's
`getexif().get_ifd(0x8769)` also silently fails to persist `DateTimeOriginal`,
which is the tag Synology actually reads — `DateTime` alone is not enough.

Overwriting a file does **not** reliably re-index: after replacing the August
pages, DSM kept thumbnails from the previous version. Forcing it needs
`rm -rf */@eaDir` plus a `touch` to now so the indexer sees a change event;
EXIF still drives the photo date, so the timeline is unaffected and the mtime
can be set back afterwards.

Discord output is unchanged — a captured newsletter is 14–19 MB, well over
Discord's 10 MB limit, so the pages only ever go to Synology Photos.
DSM indexes them on write: `@eaDir` thumbnails appeared immediately for files
delivered over SSH by an external process, so no manual re-index is needed.

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
- **"Today" highlighted on the wrong day** — the container has no `TZ` set, so
  `date.today()` returns the **UTC** date. The 06:00 BKK auto-post runs at 23:00
  UTC the day before, so `calendar_render._month_cells` boxed *yesterday* while
  the log correctly reported the message as edited. Every "today" now comes from
  `datetime.now(BANGKOK_TZ).date()` (`calendar_render.BANGKOK_TZ`, and the three
  former `date.today()` calls in `bot.py`). `TZ: "Asia/Bangkok"` was added to
  `docker-compose.yml` as defence-in-depth only — the code must not rely on it,
  since a bare `python bot.py` run has no compose file.
- **The `@everyone` events embed was never deleted** — `delete_daily_reminder`
  fires at 17:00 UTC, which is already **00:00 of the next** BKK day, so the
  stored embed's date never equalled `today_str`. The guard took its
  pop-and-return branch every night: state cleared, message left in the channel,
  no log line either way. The comparison is now inverted (a *stale* date is what
  triggers the delete) and the keep branch leaves the state key intact. This was
  masked until `post_two_month_calendar` switched to editing in place — the old
  unconditional channel purge used to remove the orphan as a side effect.
- **Orphaned embeds are now self-healed** — `post_daily_calendar` overwrote
  `state["daily_reminder_msg"]` without deleting the previous message, so any
  missed midnight run leaked an embed permanently. It now deletes a stored embed
  carrying a different date before recording the new one.

## Key conventions

- **Slash commands** are guild-synced on startup (no global commands) for instant propagation.
- All `slow` operations (Playwright rendering, file processing) use `await interaction.response.defer()` before the work begins.
- Admin commands are gated by Discord role name `"Admin"` checked inside each command handler.
- Recurring events are stored as rules and expanded dynamically — they are never pre-materialized into `events.json`.
- Thai text is used for all parent-visible strings; English is used only for admin command names and internal logging.
