import os
import io
import re
import uuid
import json
import asyncio
import discord
from discord import app_commands
from discord.ext import tasks
from datetime import date, datetime, timezone, timedelta, time as dtime
from pathlib import Path
from dotenv import load_dotenv
import aiohttp
from calendar_render import (
    generate_two_month_image,
    load_events, TH_MONTHS, next_month,
)

# Optional deps — bot works without them but OCR/PDF won't extract text
try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False


# ---- Config ----
load_dotenv()
TOKEN      = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
BASE_DIR            = Path(__file__).resolve().parent
EVENTS_FILE         = BASE_DIR / "events.json"
RECURRING_FILE      = BASE_DIR / "recurring.json"
STATE_FILE          = BASE_DIR / "calendar_state.json"
REMINDERS_FILE      = BASE_DIR / "reminders.json"
RESOURCES_FILE      = BASE_DIR / "resources.json"

BANGKOK_TZ = timezone(timedelta(hours=7))
_dress_post_lock = asyncio.Lock()

TH_WEEKDAYS = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]

MONTH_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

CATEGORIES_FILE = BASE_DIR / "categories.json"

def load_categories_data() -> list:
    """Load raw category list from categories.json."""
    with open(CATEGORIES_FILE, encoding="utf-8") as f:
        return json.load(f)

def load_categories() -> set:
    """Return a set of valid category keys (reloads from disk each call)."""
    return {c["key"] for c in load_categories_data()}

def load_cat_labels() -> dict:
    """Return a dict of {key: Thai label} (reloads from disk each call)."""
    return {c["key"]: c["label"] for c in load_categories_data()}

def cat_color_int(cat_key: str, fallback: int = 0x534AB7) -> int:
    """Return a Discord embed color integer matching the category's text color.
    Uses the text color (dark, saturated) rather than the bg (light pastel) so the
    Discord embed stripe is clearly visible. Falls back to calendar purple if unknown."""
    for c in load_categories_data():
        if c["key"] == cat_key:
            return int(c["text"].lstrip("#"), 16)
    return fallback

# ---------------------------------------------------------------------------
#  ANSI helpers for Discord ```ansi``` code blocks
#  Codes: 32=green 33=yellow/orange 34=blue 35=magenta  37=white  0=reset
#  Style: 1=bold  2=dim
# ---------------------------------------------------------------------------
_ESC = "\x1b"
_RST = f"{_ESC}[0m"

def _ansi(text: str, code: str, bold: bool = False, dim: bool = False) -> str:
    """Wrap text in an ANSI escape sequence for use inside a ```ansi``` Discord block."""
    style = "1" if bold else ("2" if dim else "")
    prefix = f"{_ESC}[{style + ';' if style else ''}{code}m"
    return f"{prefix}{text}{_RST}"

def load_cat_ansi() -> dict:
    """Return {category_key: ansi_code_str} from categories.json."""
    return {c["key"]: c.get("ansi", "37") for c in load_categories_data()}

CAT_VALID = load_categories()   # cached at startup for the modal default; always reloaded on submit

async def category_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Dynamic category dropdown — reads categories.json at call time."""
    data = load_categories_data()
    choices = []
    for c in data:
        display = f"{c['label']} ({c['key']})"
        if not current or current.lower() in display.lower() or current.lower() in c["key"].lower():
            choices.append(app_commands.Choice(name=display[:100], value=c["key"]))
    return choices[:25]

# Resources channel — set RESOURCES_CHANNEL_ID in .env
_res_id = os.getenv("RESOURCES_CHANNEL_ID")
RESOURCES_CHANNEL_ID = int(_res_id) if _res_id else None

# Dress-code channel
_dress_id = os.getenv("DRESS_CHANNEL_ID")
DRESS_CHANNEL_ID = int(_dress_id) if _dress_id else None
DRESSCODE_FILE   = BASE_DIR / "dresscode.json"

# Private test channel (not visible to parents) — admin /test-* commands post here
TEST_CHANNEL_ID = int(os.getenv("TEST_CHANNEL_ID", "1503578584961515691"))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True          # Required for on_member_join welcome DM

client = discord.Client(intents=intents)
tree   = app_commands.CommandTree(client)


# =============================================
#  Calendar state: track posted message IDs
# =============================================
def save_json_atomic(path: Path, data) -> None:
    """Write JSON to a sibling .tmp file then os.replace() so a crash mid-write
    can never truncate the live file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state: dict):
    save_json_atomic(STATE_FILE, state)

def get_month_events(events: list, year: int, month: int) -> list:
    result = []
    for e in events:
        d = datetime.strptime(e["date"], "%Y-%m-%d").date()
        if d.year == year and d.month == month:
            result.append(e)
    result.sort(key=lambda x: x["date"])
    return result

def load_reminders() -> list:
    if REMINDERS_FILE.exists():
        with open(REMINDERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []

def save_reminders(reminders: list):
    save_json_atomic(REMINDERS_FILE, reminders)

def save_events(events: list):
    """Atomic write for events.json (one-off events)."""
    save_json_atomic(EVENTS_FILE, events)


# ---- Resource storage ----
def load_resources() -> list:
    if RESOURCES_FILE.exists():
        with open(RESOURCES_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []

def save_resources(resources: list):
    save_json_atomic(RESOURCES_FILE, resources)


# ---- Recurring events ----
def load_recurring() -> list:
    if RECURRING_FILE.exists():
        with open(RECURRING_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []

def save_recurring(recurring: list):
    save_json_atomic(RECURRING_FILE, recurring)

def expand_recurring_for_month(rec: dict, year: int, month: int) -> list:
    """Expand a recurring event into individual event dicts for a given month."""
    import calendar as _cal
    start   = datetime.strptime(rec["start_date"], "%Y-%m-%d").date()
    end     = datetime.strptime(rec["end_date"], "%Y-%m-%d").date() if rec.get("end_date") else None
    excluded = set(rec.get("excluded_dates", []))
    weekday  = rec["weekday"]          # 0=Mon … 6=Sun
    step     = 7 if rec["recurrence"] == "weekly" else 14

    # First occurrence on or after start_date on the correct weekday
    days_ahead = (weekday - start.weekday()) % 7
    first_occ  = start + timedelta(days=days_ahead)

    _, num_days  = _cal.monthrange(year, month)
    month_start  = date(year, month, 1)
    month_end    = date(year, month, num_days)

    results = []
    current = first_occ
    while current <= month_end:
        if current >= month_start:
            if end is None or current <= end:
                date_str = current.strftime("%Y-%m-%d")
                if date_str not in excluded:
                    ev = {"date": date_str, "name": rec["name"],
                          "cat": rec["cat"], "_recurring_id": rec["id"]}
                    if rec.get("detail"):
                        ev["detail"] = rec["detail"]
                    results.append(ev)
        current += timedelta(days=step)
    return results

def get_combined_events_range(y1: int, m1: int, y2: int, m2: int) -> list:
    """Return one-off events + expanded recurring events for a two-month window."""
    one_off   = load_events(EVENTS_FILE)
    expanded  = []
    for rec in load_recurring():
        expanded.extend(expand_recurring_for_month(rec, y1, m1))
        expanded.extend(expand_recurring_for_month(rec, y2, m2))
    return one_off + expanded


# ---- Dress code ----
def load_dresscode() -> dict:
    if DRESSCODE_FILE.exists():
        with open(DRESSCODE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"schedule": {}, "overrides": {}}

def save_dresscode(dc: dict):
    save_json_atomic(DRESSCODE_FILE, dc)

def get_dress_for_date(d: date) -> dict | None:
    """Return dress info dict for a date, checking overrides then weekly schedule."""
    dc = load_dresscode()
    date_str = d.strftime("%Y-%m-%d")
    if date_str in dc.get("overrides", {}):
        return dc["overrides"][date_str]
    wd = str(d.weekday())   # "0"=Mon … "6"=Sun
    return dc.get("schedule", {}).get(wd)

def get_events_for_date(d: date) -> list:
    """Return one-off and recurring events for a specific date."""
    date_str = d.strftime("%Y-%m-%d")
    one_off = [e for e in load_events(EVENTS_FILE) if e.get("date") == date_str]
    recurring_on_day = []
    for rec in load_recurring():
        for ev in expand_recurring_for_month(rec, d.year, d.month):
            if ev["date"] == date_str:
                recurring_on_day.append(ev)
    return one_off + recurring_on_day

def is_holiday_or_weekend(d: date) -> tuple[bool, str]:
    """
    Returns (True, holiday_name) if the day should be treated as a holiday/day-off.
    A day is considered a holiday if it is a weekend or has a cat='holiday' event,
    BUT only when there is no non-recurring, non-holiday event scheduled that day
    (e.g. a school activity on a Saturday overrides the holiday treatment).
    Returns (False, "") if it is a normal school day.
    """
    events_on_day = get_events_for_date(d)
    # A non-holiday one-off event means school is running — don't treat as holiday
    has_school_event = any(e.get("cat") != "holiday" for e in events_on_day)
    if has_school_event:
        return False, ""
    # Public holiday in events.json
    holiday_event = next((e for e in events_on_day if e.get("cat") == "holiday"), None)
    if holiday_event:
        return True, holiday_event.get("name", "วันหยุดนักขัตฤกษ์")
    # Weekend
    if d.weekday() == 5:
        return True, "วันเสาร์"
    if d.weekday() == 6:
        return True, "วันอาทิตย์"
    return False, ""

def next_school_day(d: date, max_lookahead: int = 60) -> date:
    """Return the next school day after d (skips weekends AND holidays)."""
    nxt = d + timedelta(days=1)
    for _ in range(max_lookahead):
        is_hol, _ = is_holiday_or_weekend(nxt)
        if not is_hol:
            return nxt
        nxt += timedelta(days=1)
    return nxt

def fmt_thai_date(d: date) -> str:
    wd = TH_WEEKDAYS[d.weekday()]
    return f"วัน{wd}ที่ {d.day} {TH_MONTHS[d.month]} {d.year + 543}"

def _dress_section(d: date, info: dict | None, icon: str) -> str:
    """Build one day's block for the embed description."""
    wd       = TH_WEEKDAYS[d.weekday()]
    date_str = f"วัน{wd}ที่ {d.day} {TH_MONTHS[d.month]} {d.year + 543}"
    is_hol, hol_name = is_holiday_or_weekend(d)
    if is_hol:
        dress_line = f"# 🎉  วันหยุด — {hol_name}"
        note_line  = ""
    elif info:
        dress_line = f"# {info['dress']}"
        note_line  = f"\n> _{info['note']}_" if info.get("note") else ""
    else:
        dress_line = "# _ยังไม่ได้กำหนด_"
        note_line  = ""
    return f"### {icon}  {date_str}\n{dress_line}{note_line}"


def get_upcoming_special_days(after: date) -> list[tuple[date, dict]]:
    """Return all date overrides strictly after `after`, sorted by date."""
    overrides = load_dresscode().get("overrides", {})
    result = []
    for date_str, info in overrides.items():
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        if d > after:
            result.append((d, info))
    result.sort(key=lambda x: x[0])
    return result


_dress_last_posted: date | None = None

async def post_dress_code(target_date: date = None, force: bool = False):
    """Purge the dress channel, then post a fresh dress-code embed.

    `force=True` bypasses the once-per-day idempotency guard (used by the
    manual /post-dress slash command so admins can re-post on demand).
    """
    global _dress_last_posted
    async with _dress_post_lock:
        today = target_date or datetime.now(BANGKOK_TZ).date()
        if not force and _dress_last_posted == today:
            print(f"[dress] Already posted for {today}, skipping duplicate trigger")
            return
        await _post_dress_code_inner(target_date)
        _dress_last_posted = today


def build_dress_embed(today: date, test_mode: bool = False) -> discord.Embed:
    """Embed for today + next school day's dress code, plus upcoming specials.
    Shared by `_post_dress_code_inner` (live) and `/test-dress` (test channel)."""
    tomorrow      = next_school_day(today)
    today_info    = get_dress_for_date(today)
    tomorrow_info = get_dress_for_date(tomorrow)
    specials      = get_upcoming_special_days(after=today)

    SEP  = "\n​\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n​\n"
    desc = (
        _dress_section(today,    today_info,    "🌅") +
        SEP +
        _dress_section(tomorrow, tomorrow_info, "🌄")
    )
    if specials:
        lines = ["### 📌  วันแต่งกายพิเศษที่กำลังจะมาถึง\n"]
        for d, info in specials:
            wd       = TH_WEEKDAYS[d.weekday()]
            date_lbl = f"**{d.day} {TH_MONTHS[d.month]} {d.year + 543}**  ({wd})"
            dress    = info.get("dress", "—")
            note     = f"  _— {info['note']}_" if info.get("note") else ""
            lines.append(f"◆  {date_lbl}  —  {dress}{note}")
        desc += SEP + "\n".join(lines)

    title  = "👗  แจ้งเครื่องแต่งกาย" + ("  [ทดสอบ]" if test_mode else "")
    color  = 0x95A5A6 if test_mode else 0x1ABC9C
    embed  = discord.Embed(title=title, description=desc, color=color)
    prefix = "[TEST] " if test_mode else ""
    embed.set_footer(
        text=f"{prefix}อัปเดตล่าสุด: {datetime.now(BANGKOK_TZ).strftime('%d/%m/%Y %H:%M')} น."
    )
    return embed


async def _post_dress_code_inner(target_date: date = None):
    if not DRESS_CHANNEL_ID:
        print("[dress] DRESS_CHANNEL_ID not set in .env — skipping")
        return
    channel = client.get_channel(DRESS_CHANNEL_ID)
    if not channel:
        try:
            channel = await client.fetch_channel(DRESS_CHANNEL_ID)
        except Exception as e:
            print(f"[dress] Cannot find channel {DRESS_CHANNEL_ID}: {e}")
            return

    today = target_date or datetime.now(BANGKOK_TZ).date()
    embed = build_dress_embed(today)

    # Post the new embed FIRST, then delete old messages.
    # This prevents purge from killing a deferred slash-command interaction
    # that lives in the same channel (e.g. /post-dress used inside #dress).
    new_msg = await channel.send(embed=embed)

    # Now purge everything except the message we just sent.
    # Bulk-delete only works on messages <14 days old; older messages are
    # walked individually so the channel never accumulates stale posts.
    try:
        await channel.purge(limit=200, check=lambda m: m.id != new_msg.id)
    except Exception as e:
        print(f"[dress] Purge failed: {e}")
    try:
        async for old in channel.history(limit=50, before=new_msg):
            if old.id == new_msg.id:
                continue
            try:
                await old.delete()
            except discord.HTTPException:
                pass
    except Exception as e:
        print(f"[dress] Old-message sweep failed: {e}")

    print(f"[dress] Posted dress code — today={today} / tomorrow={tomorrow} / specials={len(specials)}")


# ---- Text cleaning ----
def clean_extracted_text(text: str) -> str:
    """
    Clean and reformat raw PDF/OCR text for readable Discord display.

    Strategy:
    - Replace zero-width spaces (used as word separators in Thai PDFs) with regular spaces
    - Detect "X:" section headers → bold Discord markdown
    - Detect short label:value pairs (Date/Subject/etc.) → merge onto one line with bold label
    - Join consecutive wrapped body lines into single paragraphs
    - Preserve bullet-point lines as individual blocks
    """
    # 1. Replace zero-width space (word separator in many Thai PDFs) with regular space
    text = text.replace("\u200b", " ")
    # Remove other invisible chars (zero-width non-joiner/joiner, BOM, soft hyphen)
    for ch in ("\u200c", "\u200d", "\ufeff", "\u00ad"):
        text = text.replace(ch, "")

    # 2. Normalise each line: collapse multiple spaces, strip
    lines = [re.sub(r"  +", " ", ln).strip() for ln in text.split("\n")]

    # Patterns
    HEADER_RE = re.compile(r"^.{2,60}:\s*$")          # short line ending with ":"
    BULLET_RE = re.compile(r"^[●•·\-–—]|^\d+\.")      # bullet / list item

    result_blocks = []
    current = []

    def flush():
        if current:
            result_blocks.append(" ".join(current))
            current.clear()

    i = 0
    while i < len(lines):
        line = lines[i]

        # Empty line → paragraph boundary
        if not line:
            flush()
            i += 1
            continue

        # Bullet item → own block
        if BULLET_RE.match(line):
            flush()
            result_blocks.append(line)
            i += 1
            continue

        # Line ending with ":" → either a section header or a label:value pair
        if HEADER_RE.match(line):
            # Peek at next non-empty line (value candidate)
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            next_line = lines[j].strip() if j < len(lines) else ""

            # Peek one further (to check what comes after the value)
            k = j + 1
            while k < len(lines) and not lines[k].strip():
                k += 1
            after_next = lines[k].strip() if k < len(lines) else ""

            label_text = line.rstrip(":").strip()

            # Treat as label:value when:
            #   • value is short (< 55 chars) and isn't itself a header or bullet
            #   • AND either the label is short (≤ 20 chars, e.g. "Date", "Kindergarten ECAs")
            #          OR what follows the value is a new section / end of text
            is_metadata = (
                next_line
                and len(next_line) < 55
                and not HEADER_RE.match(next_line)
                and not BULLET_RE.match(next_line)
                and (
                    len(label_text) <= 20
                    or not after_next
                    or HEADER_RE.match(after_next)
                    or BULLET_RE.match(after_next)
                )
            )

            flush()
            if is_metadata:
                result_blocks.append(f"**{label_text}:** {next_line}")
                i = j + 1           # skip the value line we consumed
            else:
                # True section heading — bold, own block; body follows below
                result_blocks.append(f"**{line}**")
                i += 1
            continue

        # Regular body text — accumulate into current paragraph
        current.append(line)
        i += 1

    flush()
    return "\n\n".join(b for b in result_blocks if b).strip()


def extract_dates_from_text(text: str, ref_year: int = None) -> list:
    """
    Scan extracted text for English month-name dates and return a sorted list of
    (date, context_snippet) tuples, skipping dates already in the past.
    """
    if not ref_year:
        ref_year = date.today().year

    # Matches "March 25th, 2026", "April 27th 2026", "June 19th" etc.
    pattern = re.compile(
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?[,\s]+(\d{4})?",
        re.IGNORECASE,
    )

    results = []
    seen = set()

    for m in pattern.finditer(text):
        month = MONTH_EN.get(m.group(1).lower())
        day   = int(m.group(2))
        year  = int(m.group(3)) if m.group(3) else ref_year
        if not month or not (1 <= day <= 31):
            continue
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d in seen or d < date.today():
            continue
        seen.add(d)
        # Grab ~80 chars of surrounding context, collapsed to one line
        s = max(0, m.start() - 70)
        e = min(len(text), m.end() + 70)
        ctx = re.sub(r"\s+", " ", text[s:e]).strip()
        results.append((d, ctx))

    results.sort(key=lambda x: x[0])
    return results


# ---- Text extraction ----
def extract_text_from_pdf(data: bytes) -> str:
    """
    Extract plain text from PDF bytes.
    Strategy per page:
      1. Try PyMuPDF selectable-text extraction.
      2. If a page yields no text (scanned/image page), render it at 200 DPI
         and run pytesseract OCR as a fallback (requires both libs).
    """
    if not HAS_PYMUPDF:
        print("[resources] PyMuPDF not installed — cannot extract PDF text. "
              "Run: pip install PyMuPDF")
        return ""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        pages = []
        for page_num, page in enumerate(doc, start=1):
            text = page.get_text().strip()
            if text:
                pages.append(text)
                print(f"[resources] PDF page {page_num}: extracted {len(text)} chars via text layer")
            else:
                print(f"[resources] PDF page {page_num}: no text layer, skipping (OCR removed)")
        doc.close()
        result = clean_extracted_text("\n\n".join(pages))
        print(f"[resources] PDF total extracted: {len(result)} chars across {len(pages)} page(s)")
        return result
    except Exception as e:
        print(f"[resources] PDF extraction error: {e}")
        return ""



# ---- Post a resource to the resources channel as embed + thread ----
SOURCE_LABELS = {
    "email": "📧 อีเมลโรงเรียน",
    "app":   "📱 แอปโรงเรียน",
    "other": "📄 อื่นๆ",
}
SOURCE_COLORS = {
    "email": 0x3498DB,
    "app":   0x2ECC71,
    "other": 0x95A5A6,
}

async def post_resource_to_channel(
    channel,
    title: str,
    source: str,
    date_str: str,
    content: str,
    filename: str = None,
    file_data: bytes = None,
) -> tuple[int, int]:
    """
    Create a structured embed in the resources channel, open a thread on it,
    post the extracted text, and attach the original file. Returns (message_id, thread_id).
    """
    embed = discord.Embed(
        title=title,
        color=SOURCE_COLORS.get(source, 0x95A5A6),
    )
    embed.add_field(name="แหล่งที่มา", value=SOURCE_LABELS.get(source, source), inline=True)
    embed.add_field(name="วันที่เอกสาร", value=date_str, inline=True)
    if filename:
        embed.add_field(name="ไฟล์", value=filename, inline=True)
    has_text = bool(content and content.strip())
    embed.add_field(
        name="เนื้อหา",
        value="✅ มีข้อความแนบในเธรด" if has_text else "⚠️ ไม่สามารถดึงข้อความได้",
        inline=False,
    )
    embed.set_footer(text=f"เพิ่มเมื่อ {datetime.now(BANGKOK_TZ).strftime('%d/%m/%Y %H:%M')} น.")

    msg = await channel.send(embed=embed)
    thread = await msg.create_thread(name=title[:100], auto_archive_duration=10080)  # 7-day archive

    # Post original file first so it's at the top of the thread for easy reference
    if file_data and filename:
        try:
            original_file = discord.File(io.BytesIO(file_data), filename=filename)
            await thread.send("📎 **ไฟล์ต้นฉบับ**", file=original_file)
        except discord.HTTPException as e:
            print(f"[resources] Could not attach original file: {e}")

    # Post extracted text (split into 1900-char chunks)
    if has_text:
        await thread.send("📄 **เนื้อหาที่ดึงออกมา**")
        chunks = [content[i:i+1900] for i in range(0, len(content), 1900)]
        for chunk in chunks:
            await thread.send(chunk)
    else:
        await thread.send("⚠️ _(ไม่มีข้อความที่สามารถดึงได้จากไฟล์นี้)_")

    # Detect dates and offer to add them as calendar events
    if has_text:
        dates = extract_dates_from_text(content, ref_year=datetime.now(BANGKOK_TZ).year)
        if dates:
            lines = ["─" * 30,
                     "🗓️ **พบวันที่ในเอกสาร — กดปุ่มเพื่อเพิ่มลงปฏิทิน** _(Admin เท่านั้น)_"]
            for d, ctx in dates[:5]:
                th_month = TH_MONTHS[d.month]
                short_ctx = (ctx[:90] + "…") if len(ctx) > 90 else ctx
                lines.append(f"  · **{d.day} {th_month} {d.year + 543}** — _{short_ctx}_")
            view = DateSelectionView(dates)
            await thread.send("\n".join(lines), view=view)

    return msg.id, thread.id


async def clear_channel_messages(channel):
    """Delete ALL messages in the channel so no stale calendar remains."""
    try:
        deleted = await channel.purge(limit=500)
        print(f"[clear_channel] Deleted {len(deleted)} message(s) from #{channel.name}")
    except discord.Forbidden:
        print("[clear_channel] Missing 'Manage Messages' permission — cannot purge!")
    except discord.HTTPException as e:
        print(f"[clear_channel] Purge failed: {e}")


def format_event_list(events_by_month: list, cat_ansi: dict = None) -> str:
    """Build a formatted event list.

    events_by_month : list of (year, month, events_list) tuples
    cat_ansi        : if provided, output is a Discord ```ansi``` code block with
                      per-category colors and spacious layout; otherwise plain markdown.
    """
    today      = datetime.now(BANGKOK_TZ).date()
    use_ansi   = cat_ansi is not None
    cat_labels = load_cat_labels() if use_ansi else {}
    lines: list[str] = []
    today_inserted   = False

    for year, month, evts in events_by_month:
        if not evts:
            continue

        # ── Month header ────────────────────────────────────────────────────
        if use_ansi:
            lines.append(_ansi(f"{TH_MONTHS[month]}  {year + 543}", "37", bold=True))
            lines.append("─" * 30)
        else:
            lines.append(f"**{TH_MONTHS[month]} {year + 543}**")

        for ev in evts:
            ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()

            # ── Skip past events — they show on the calendar image only ──────
            if ev_date < today:
                continue

            # ── Today / upcoming marker ──────────────────────────────────────
            if not today_inserted and ev_date >= today:
                if use_ansi:
                    lines.append("")
                    marker = "▶  วันนี้" if ev_date == today else "▶  กิจกรรมที่กำลังจะมา"
                    lines.append(_ansi(marker, "37", dim=True))
                else:
                    lines.append("--- วันนี้ ---" if ev_date == today
                                 else "--- กิจกรรมที่กำลังจะมา ---")
                today_inserted = True

            detail  = ev.get("detail", "")
            cat_key = ev.get("cat", "")
            code    = cat_ansi.get(cat_key, "37") if use_ansi else ""

            if use_ansi:
                cat_lbl = cat_labels.get(cat_key, cat_key)
                if ev.get("end_date") and ev["end_date"] != ev["date"]:
                    ev_end_d = datetime.strptime(ev["end_date"], "%Y-%m-%d").date()
                    if ev_date.month == ev_end_d.month:
                        th_day = f"{ev_date.day}–{ev_end_d.day} {TH_MONTHS[ev_date.month]}"
                    else:
                        th_day = f"{ev_date.day} {TH_MONTHS[ev_date.month]} – {ev_end_d.day} {TH_MONTHS[ev_end_d.month]}"
                else:
                    th_day = f"{ev_date.day} {TH_MONTHS[ev_date.month]}"
                lines.append("")                                                  # breathing room
                lines.append(_ansi(f"  ●  {ev['name']}", code, bold=True))      # bold event name
                lines.append(_ansi(f"     {cat_lbl}  ·  {th_day}", code, dim=True))  # dim meta
                if detail:
                    lines.append(_ansi(f"     ↳  {detail}", code, dim=True))    # dim detail
            else:
                lines.append(f"  \u00b7 {ev_date.day} {TH_MONTHS[month]} \u2014 {ev['name']}")
                if detail:
                    lines.append(f"    _↳ {detail}_")

    # ── Fallback when nothing is upcoming ───────────────────────────────────
    if not today_inserted and lines:
        if use_ansi:
            lines.append("")
            lines.append(_ansi("  ไม่มีกิจกรรมที่กำลังจะมาในช่วงนี้", "37", dim=True))
        else:
            lines.append("--- กิจกรรมที่กำลังจะมา ---")
            lines.append("  ไม่มีกิจกรรมที่กำลังจะมาในช่วงนี้")

    if not lines:
        empty = _ansi("  ไม่มีกิจกรรมในช่วงนี้", "37", dim=True) if use_ansi else "ไม่มีกิจกรรมในช่วงนี้"
        return f"```ansi\n{empty}\n```" if use_ansi else empty

    body = "\n".join(lines)
    return f"```ansi\n{body}\n```" if use_ansi else body


# =============================================
#  Discord UI: Add Event from Resource (Modal + Buttons)
# =============================================
class AddEventModal(discord.ui.Modal):
    """Modal that opens when an admin clicks a detected-date button."""

    def __init__(self, detected_date: date):
        th_month = TH_MONTHS[detected_date.month]
        title = f"เพิ่มกิจกรรม {detected_date.day} {th_month} {detected_date.year + 543}"
        super().__init__(title=title[:45])   # Discord modal title max 45 chars
        self.detected_date = detected_date

        self.event_name = discord.ui.TextInput(
            label="ชื่อกิจกรรม",
            placeholder="เช่น ECA Term 3 เริ่มต้น, วันสุดท้ายสมัคร ECA",
            required=True,
            max_length=100,
        )
        self.add_item(self.event_name)

        _cats = " / ".join(sorted(load_categories()))
        self.category = discord.ui.TextInput(
            label=f"หมวดหมู่  ({_cats})",
            placeholder="activity",
            default="activity",
            required=True,
            max_length=10,
        )
        self.add_item(self.category)

        self.detail = discord.ui.TextInput(
            label="รายละเอียดเพิ่มเติม (ไม่บังคับ)",
            placeholder="เช่น เริ่ม 10.00 น., เตรียมชุดสำรอง, สถานที่: หอประชุม",
            required=False,
            max_length=200,
        )
        self.add_item(self.detail)

    async def on_submit(self, interaction: discord.Interaction):
        cat = self.category.value.strip().lower()
        valid_cats = load_categories()   # always reload fresh so edits to categories.json take effect
        if cat not in valid_cats:
            await interaction.response.send_message(
                f"หมวดหมู่ไม่ถูกต้อง ใช้ได้: {', '.join(sorted(valid_cats))}",
                ephemeral=True,
            )
            return

        # Defer immediately — post_two_month_calendar takes seconds (Playwright render)
        # and would exceed Discord's 3-second modal response window.
        await interaction.response.defer(ephemeral=True)

        date_str = self.detected_date.strftime("%Y-%m-%d")
        name     = self.event_name.value.strip()
        detail   = self.detail.value.strip() if self.detail.value else ""

        events = load_events(EVENTS_FILE)
        event = {"date": date_str, "name": name, "cat": cat}
        if detail:
            event["detail"] = detail
        events.append(event)
        save_events(events)

        await post_two_month_calendar()
        th_month = TH_MONTHS[self.detected_date.month]
        detail_note = f"\n   _{detail}_" if detail else ""
        await interaction.followup.send(
            f"✅ เพิ่ม **{name}** ({self.detected_date.day} {th_month} "
            f"{self.detected_date.year + 543}) ลงปฏิทินแล้ว{detail_note}",
            ephemeral=True,
        )


class DateButton(discord.ui.Button):
    def __init__(self, detected_date: date, context: str):
        th_month = TH_MONTHS[detected_date.month]
        label = f"{detected_date.day} {th_month} {detected_date.year + 543}"
        super().__init__(label=label[:80], style=discord.ButtonStyle.primary, emoji="🗓️")
        self.detected_date = detected_date
        self.context = context

    async def callback(self, interaction: discord.Interaction):
        if not any(r.name == "Admin" for r in interaction.user.roles):
            await interaction.response.send_message(
                "เฉพาะ Admin เท่านั้นที่เพิ่มกิจกรรมได้", ephemeral=True)
            return
        await interaction.response.send_modal(AddEventModal(self.detected_date))


class DateSelectionView(discord.ui.View):
    def __init__(self, dates: list):
        super().__init__(timeout=600)   # buttons live for 10 minutes
        for d, ctx in dates[:5]:        # Discord allows max 5 buttons per action row
            self.add_item(DateButton(d, ctx))



# =============================================
#  Daily events embed: list of today's events for the calendar channel.
#  Posted underneath the daily calendar refresh on school days.
# =============================================
def build_daily_events_embed(today_bkk: date):
    """Build the "@everyone" today's-events embed for the calendar channel.

    Returns (content_text, discord.Embed) — or None when today has no
    actionable (non-holiday) events to surface."""
    today_str = today_bkk.strftime("%Y-%m-%d")

    events = load_events(EVENTS_FILE)
    today_events = [
        e for e in events
        if e.get("date") and (
            e["date"] == today_str
            or (e.get("end_date") and e["date"] <= today_str <= e["end_date"])
        )
    ]
    for rec in load_recurring():
        for ev in expand_recurring_for_month(rec, today_bkk.year, today_bkk.month):
            if ev["date"] == today_str:
                today_events.append(ev)

    # Filter out holiday-category events — only post actionable school events
    today_events = [e for e in today_events if e.get("cat", "") != "holiday"]
    if not today_events:
        return None

    th_month   = TH_MONTHS[today_bkk.month]
    cat_labels = load_cat_labels()
    cat_ansi   = load_cat_ansi()

    desc_lines: list[str] = []
    for ev in today_events:
        cat_key = ev.get("cat", "")
        code    = cat_ansi.get(cat_key, "37")
        cat_lbl = cat_labels.get(cat_key, cat_key)
        detail  = ev.get("detail", "")
        desc_lines.append(_ansi(f"  ●  {ev['name']}", code, bold=True))
        if ev.get("end_date") and ev["end_date"] != ev["date"]:
            ev_s = datetime.strptime(ev["date"], "%Y-%m-%d").date()
            ev_e = datetime.strptime(ev["end_date"], "%Y-%m-%d").date()
            if ev_s.month == ev_e.month:
                date_label = f"{ev_s.day}–{ev_e.day} {TH_MONTHS[ev_s.month]}"
            else:
                date_label = f"{ev_s.day} {TH_MONTHS[ev_s.month]} – {ev_e.day} {TH_MONTHS[ev_e.month]}"
        else:
            date_label = f"{today_bkk.day} {th_month}"
        desc_lines.append(_ansi(f"     {cat_lbl}  ·  {date_label}", code, dim=True))
        if detail:
            desc_lines.append(_ansi(f"     ↳  {detail}", code, dim=True))
        desc_lines.append("")

    embed = discord.Embed(
        title=f"📅  กิจกรรมวันนี้  —  {today_bkk.day} {th_month} {today_bkk.year + 543}",
        description="```ansi\n" + "\n".join(desc_lines) + "```",
        color=cat_color_int(today_events[0].get("cat", "")),
    )
    embed.set_footer(text="ข้อความนี้จะถูกลบอัตโนมัติเมื่อสิ้นสุดวัน")
    return "@everyone", embed



# =============================================
#  Reminder Type 1b: Delete daily reminder at midnight Bangkok time
#  (runs at 17:00 UTC = 00:00 Bangkok time)
# =============================================
@tasks.loop(time=dtime(hour=17, minute=0, tzinfo=timezone.utc))
async def delete_daily_reminder():
    """At midnight UTC+7, delete the morning daily reminder message to keep the channel clean."""
    state = load_state()
    info  = state.get("daily_reminder_msg")
    if not info:
        return

    today_str = datetime.now(BANGKOK_TZ).date().strftime("%Y-%m-%d")
    if info.get("date") != today_str:
        state.pop("daily_reminder_msg", None)
        save_state(state)
        return

    channel = client.get_channel(info["channel_id"])
    if not channel:
        return

    try:
        msg = await channel.fetch_message(info["message_id"])
        await msg.delete()
        print(f"[daily_reminder] Deleted end-of-day reminder message {info['message_id']}")
    except discord.NotFound:
        print(f"[daily_reminder] Reminder message {info['message_id']} already deleted")
    except Exception as e:
        print(f"[daily_reminder] Could not delete reminder message: {e}")
    finally:
        # Clear the stored ID regardless of outcome
        state.pop("daily_reminder_msg", None)
        save_state(state)


# =============================================
#  Reminder Type 2: Check DM reminders every 5 minutes
# =============================================
@tasks.loop(minutes=5)
async def check_dm_reminders():
    """Send DMs to users whose personal reminder time has arrived."""
    now = datetime.now(timezone.utc)
    reminders = load_reminders()
    changed = False

    for r in reminders:
        if r.get("sent"):
            continue
        remind_at = datetime.fromisoformat(r["remind_at"])
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=timezone.utc)
        if now < remind_at:
            continue

        # Time to send the DM
        try:
            user = await client.fetch_user(int(r["user_id"]))
            ev_date    = datetime.strptime(r["event_date"], "%Y-%m-%d").date()
            th_month   = TH_MONTHS[ev_date.month]
            detail     = r.get("event_detail", "")
            cat_labels = load_cat_labels()
            cat_label  = cat_labels.get(r.get("event_cat", ""), r.get("event_cat", ""))

            cat_ansi_map = load_cat_ansi()
            code         = cat_ansi_map.get(r.get("event_cat", ""), "37")
            th_date_full = f"{ev_date.day} {th_month} {ev_date.year + 543}"
            dm_lines     = [
                _ansi(f"  ●  {r['event_name']}", code, bold=True),
                _ansi(f"     {cat_label}  ·  {th_date_full}", code, dim=True),
            ]
            if detail:
                dm_lines.append(_ansi(f"     ↳  {detail}", code, dim=True))

            dm_embed = discord.Embed(
                title="🔔  แจ้งเตือนกิจกรรม",
                description="```ansi\n" + "\n".join(dm_lines) + "\n```",
                color=cat_color_int(r.get("event_cat", "")),
            )

            await user.send(embed=dm_embed)
            r["sent"] = True
            changed = True
            print(f"[dm_reminder] Sent DM to user {r['user_id']} for event '{r['event_name']}'")
        except discord.Forbidden:
            print(f"[dm_reminder] Cannot DM user {r['user_id']} — DMs disabled")
            r["sent"] = True   # Mark sent so we don't retry forever
            changed = True
        except Exception as e:
            print(f"[dm_reminder] Error sending to {r['user_id']}: {e}")

    if changed:
        save_reminders(reminders)


# =============================================
#  Daily auto-post: re-render calendar + (on school days) today's events embed.
#  School day  -> 06:00 BKK (23:00 UTC)
#  Real holiday-> 09:00 BKK (02:00 UTC)
# =============================================
async def post_daily_calendar(today: date) -> None:
    """Re-render the 2-month calendar and (on school days) append the today's-events embed.

    `post_two_month_calendar` purges the channel before posting so the calendar
    image is always the top message. The events embed is sent right after and
    its message id is stored for end-of-day cleanup."""
    await post_two_month_calendar(today.year, today.month)

    is_hol, _ = is_holiday_or_weekend(today)
    if not is_hol:
        reminder = build_daily_events_embed(today)
        if reminder is not None:
            text, embed = reminder
            channel = client.get_channel(CHANNEL_ID)
            if not channel:
                try:
                    channel = await client.fetch_channel(CHANNEL_ID)
                except Exception as e:
                    print(f"[daily_calendar] Cannot find channel {CHANNEL_ID}: {e}")
                    channel = None
            if channel is not None:
                msg = await channel.send(text, embed=embed)
                state = load_state()
                state["daily_reminder_msg"] = {
                    "message_id": msg.id,
                    "channel_id": channel.id,
                    "date": today.strftime("%Y-%m-%d"),
                }
                save_state(state)
                print(f"[daily_calendar] Posted events embed for {today}")

    state = load_state()
    state["last_calendar_post"] = today.strftime("%Y-%m-%d")
    save_state(state)


@tasks.loop(time=dtime(hour=23, minute=0, tzinfo=timezone.utc))
async def daily_calendar_school():
    """06:00 BKK — daily calendar refresh on school days only."""
    today = datetime.now(BANGKOK_TZ).date()
    if load_state().get("last_calendar_post") == today.strftime("%Y-%m-%d"):
        print(f"[daily_calendar] Already posted for {today}, skipping school slot")
        return
    is_hol, _ = is_holiday_or_weekend(today)
    if is_hol:
        return
    await post_daily_calendar(today)


@tasks.loop(time=dtime(hour=2, minute=0, tzinfo=timezone.utc))
async def daily_calendar_holiday():
    """09:00 BKK — daily calendar refresh on real holidays only."""
    today = datetime.now(BANGKOK_TZ).date()
    if load_state().get("last_calendar_post") == today.strftime("%Y-%m-%d"):
        print(f"[daily_calendar] Already posted for {today}, skipping holiday slot")
        return
    is_hol, _ = is_holiday_or_weekend(today)
    if not is_hol:
        return
    await post_daily_calendar(today)


# =============================================
#  Dress-code: daily post at 06:00 UTC+7 on weekdays
# =============================================
@tasks.loop(time=dtime(hour=23, minute=2, tzinfo=timezone.utc))
async def daily_dress_reminder():
    """At 06:02 UTC+7 each school day, post today's and tomorrow's dress code."""
    today_bkk = datetime.now(BANGKOK_TZ).date()
    is_hol, _ = is_holiday_or_weekend(today_bkk)
    if is_hol:
        print(f"[dress] Skipping — today ({today_bkk}) is a holiday/weekend")
        return
    await post_dress_code(today_bkk)


async def build_two_month_calendar(year: int, month: int, test_mode: bool = False):
    """Render two-month calendar image + return (embed, discord.File).
    Used by post_two_month_calendar (live) and /test-calendar (test channel)."""
    y2, m2 = next_month(year, month)
    events  = get_combined_events_range(year, month, y2, m2)
    img_path = await generate_two_month_image(year, month, events,
                                              output=str(BASE_DIR / "calendar_2m.png"))
    evts_m1 = get_month_events(events, year, month)
    evts_m2 = get_month_events(events, y2, m2)
    description = format_event_list(
        [(year, month, evts_m1), (y2, m2, evts_m2)],
        cat_ansi=load_cat_ansi(),
    )

    today_str = datetime.now(BANGKOK_TZ).date().strftime("%Y-%m-%d")
    all_sorted = sorted(evts_m1 + evts_m2, key=lambda e: e["date"])
    next_ev = next((e for e in all_sorted if e["date"] >= today_str), None)
    if test_mode:
        embed_color = 0x95A5A6
    else:
        embed_color = cat_color_int(next_ev["cat"]) if next_ev else 0x534AB7

    test_tag = "[TEST] " if test_mode else ""
    if y2 != year:
        cal_title = f"{test_tag}ปฏิทินกิจกรรม — {TH_MONTHS[month]} {year + 543} - {TH_MONTHS[m2]} {y2 + 543}"
    else:
        cal_title = f"{test_tag}ปฏิทินกิจกรรม — {TH_MONTHS[month]} - {TH_MONTHS[m2]} {year + 543}"

    embed = discord.Embed(title=cal_title, description=description, color=embed_color)
    embed.set_image(url="attachment://calendar_2m.png")
    embed.set_footer(text=f"{test_tag}อัปเดตล่าสุด: {datetime.now(BANGKOK_TZ).strftime('%d/%m/%Y %H:%M')} น.")
    file = discord.File(img_path, filename="calendar_2m.png")
    return embed, file


async def post_two_month_calendar(year: int = None, month: int = None):
    """Render the 2-month calendar and replace the calendar channel's contents."""
    channel = client.get_channel(CHANNEL_ID)
    if not channel:
        try:
            channel = await client.fetch_channel(CHANNEL_ID)
        except Exception as e:
            print(f"[calendar] Cannot find channel {CHANNEL_ID}: {e}")
            raise RuntimeError(f"ไม่พบช่อง calendar (ID: {CHANNEL_ID})") from e

    today = datetime.now(BANGKOK_TZ).date()
    year  = year  or today.year
    month = month or today.month
    embed, file = await build_two_month_calendar(year, month)

    await clear_channel_messages(channel)
    msg = await channel.send(file=file, embed=embed)
    print(f"[calendar] Posted 2-month calendar starting {TH_MONTHS[month]} {year + 543} → msg {msg.id}")



# =============================================
#  Slash command: /calendar
# =============================================
@tree.command(
    name="calendar",
    description="ดูปฏิทินกิจกรรม 2 เดือน พร้อมรายการกิจกรรม",
)
@app_commands.describe(
    month="เดือนเริ่มต้น (1-12) ถ้าไม่ระบุ = เดือนปัจจุบัน",
    year="ปี ค.ศ. ถ้าไม่ระบุ = ปีปัจจุบัน",
)
async def show_calendar(
    interaction: discord.Interaction,
    month: int = None,
    year: int = None,
):
    today = datetime.now(BANGKOK_TZ).date()
    month = month or today.month
    year  = year  or today.year

    if not (1 <= month <= 12):
        await interaction.response.send_message("เดือนต้องอยู่ระหว่าง 1-12", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        await post_two_month_calendar(year, month)
        await interaction.followup.send("✅ ปฏิทินอัปเดตแล้วในช่อง calendar แล้วครับ", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาด: {e}", ephemeral=True)


# =============================================
#  Slash command: /add-event
# =============================================
@tree.command(
    name="add-event",
    description="เพิ่มกิจกรรมลงปฏิทิน (Admin เท่านั้น)",
)
@app_commands.describe(
    date_str="วันที่เริ่ม รูปแบบ YYYY-MM-DD เช่น 2026-05-20",
    end_date_str="วันที่สิ้นสุด (ถ้าเป็นกิจกรรมหลายวัน) รูปแบบ YYYY-MM-DD (ไม่บังคับ)",
    cat="หมวดหมู่ (เลือกจากรายการ)",
    name="ชื่อกิจกรรม",
    detail="รายละเอียดเพิ่มเติม (ไม่บังคับ)",
)
@app_commands.autocomplete(cat=category_autocomplete)
@app_commands.checks.has_role("Admin")
async def add_event(
    interaction: discord.Interaction,
    date_str: str,
    cat: str,
    name: str,
    end_date_str: str = None,
    detail: str = None,
):
    try:
        start_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        await interaction.response.send_message(
            "รูปแบบวันที่เริ่มไม่ถูกต้อง ใช้ YYYY-MM-DD เช่น 2026-05-20", ephemeral=True)
        return

    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
        except ValueError:
            await interaction.response.send_message(
                "รูปแบบวันที่สิ้นสุดไม่ถูกต้อง ใช้ YYYY-MM-DD เช่น 2026-05-22", ephemeral=True)
            return
        if end_date < start_date:
            await interaction.response.send_message(
                "วันที่สิ้นสุดต้องไม่อยู่ก่อนวันที่เริ่ม", ephemeral=True)
            return

    cat = cat.strip().lower()
    if cat not in load_categories():
        valid = ", ".join(sorted(load_categories()))
        await interaction.response.send_message(
            f"หมวดหมู่ไม่ถูกต้อง ใช้ได้: {valid}", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    events = load_events(EVENTS_FILE)
    event = {"date": date_str, "name": name, "cat": cat}
    if end_date_str:
        event["end_date"] = end_date_str
    if detail and detail.strip():
        event["detail"] = detail.strip()
    events.append(event)
    save_events(events)

    date_label = f"{date_str} ถึง {end_date_str}" if end_date_str else date_str
    detail_note = f"\n   _{detail.strip()}_" if detail and detail.strip() else ""
    try:
        await post_two_month_calendar()
        await interaction.followup.send(
            f"✅ เพิ่มกิจกรรม **{name}** วันที่ {date_label} แล้วครับ{detail_note}",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ บันทึกกิจกรรมแล้ว แต่อัปเดตปฏิทินไม่สำเร็จ: {e}", ephemeral=True)


# =============================================
#  Slash command: /remove-event
# =============================================
async def all_event_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Return one-off events AND recurring series as autocomplete choices for removal."""
    choices = []
    # One-off events (prefix "single|")
    events_sorted = sorted(load_events(EVENTS_FILE), key=lambda e: e["date"])
    for e in events_sorted:
        ev_date = datetime.strptime(e["date"], "%Y-%m-%d").date()
        th_month = TH_MONTHS[ev_date.month]
        label = f"{ev_date.day} {th_month} {ev_date.year + 543} — {e['name']}"
        value = f"single|{e['date']}|{e['name']}"
        if current.lower() in label.lower() or current == "":
            choices.append(app_commands.Choice(name=label[:100], value=value[:100]))
    # Recurring series (prefix "series|")
    for rec in load_recurring():
        wd_idx = rec.get("weekday")
        if wd_idx is None or not (0 <= wd_idx < len(TH_WEEKDAYS)):
            continue
        freq = "ทุกสัปดาห์" if rec.get("recurrence") == "weekly" else "ทุก 2 สัปดาห์"
        wd   = TH_WEEKDAYS[wd_idx]
        label = f"🔁 {rec.get('name','?')} ({freq} วัน{wd})"
        value = f"series|{rec.get('id','')}|{rec.get('name','?')}"
        if current.lower() in label.lower() or current == "":
            choices.append(app_commands.Choice(name=label[:100], value=value[:100]))
    return choices[:25]


@tree.command(
    name="remove-event",
    description="ลบกิจกรรม หรือ ลบกิจกรรมประจำทั้งหมด (Admin เท่านั้น)",
)
@app_commands.describe(
    event="เลือกกิจกรรมที่ต้องการลบ (🔁 = กิจกรรมประจำ)",
)
@app_commands.autocomplete(event=all_event_autocomplete)
@app_commands.checks.has_role("Admin")
async def remove_event(
    interaction: discord.Interaction,
    event: str,
):
    parts = event.split("|", 2)
    if len(parts) < 3:
        await interaction.response.send_message(
            "กรุณาเลือกกิจกรรมจากรายการ autocomplete", ephemeral=True)
        return

    prefix, key, name = parts[0], parts[1], parts[2]

    if prefix == "single":
        date_str = key
        events = load_events(EVENTS_FILE)
        filtered = [e for e in events if not (e["date"] == date_str and e["name"] == name)]
        if len(filtered) == len(events):
            await interaction.response.send_message(
                f"ไม่พบกิจกรรม **{name}** วันที่ {date_str}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        save_events(filtered)
        remove_desc = f"\U0001f5d1\ufe0f ลบกิจกรรม **{name}** วันที่ {date_str} แล้วครับ"

    elif prefix == "series":
        rec_id = key
        recurring = load_recurring()
        filtered  = [r for r in recurring if r["id"] != rec_id]
        if len(filtered) == len(recurring):
            await interaction.response.send_message(
                f"ไม่พบกิจกรรมประจำ **{name}**", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        save_recurring(filtered)
        remove_desc = f"\U0001f5d1\ufe0f ลบกิจกรรมประจำ **{name}** (ทุกครั้ง) แล้วครับ"

    else:
        await interaction.response.send_message(
            "กรุณาเลือกกิจกรรมจากรายการ autocomplete", ephemeral=True)
        return

    try:
        await post_two_month_calendar()
        await interaction.followup.send(remove_desc, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ ลบแล้ว แต่อัปเดตปฏิทินไม่สำเร็จ: {e}", ephemeral=True)


# =============================================
#  Slash command: /add-recurring
# =============================================
@tree.command(
    name="add-recurring",
    description="เพิ่มกิจกรรมประจำ เช่น ทุกวันจันทร์ (Admin เท่านั้น)",
)
@app_commands.describe(
    name="ชื่อกิจกรรม",
    cat="หมวดหมู่",
    frequency="ความถี่",
    weekday="วันในสัปดาห์",
    start_date="วันเริ่มต้น YYYY-MM-DD (ไม่ระบุ = วันนี้)",
    end_date="วันสิ้นสุด YYYY-MM-DD (ไม่ระบุ = ไม่มีกำหนด)",
    detail="รายละเอียดเพิ่มเติม (ไม่บังคับ)",
)
@app_commands.autocomplete(cat=category_autocomplete)
@app_commands.choices(
    frequency=[
        app_commands.Choice(name="ทุกสัปดาห์",    value="weekly"),
        app_commands.Choice(name="ทุก 2 สัปดาห์", value="biweekly"),
    ],
    weekday=[
        app_commands.Choice(name="จันทร์",     value="0"),
        app_commands.Choice(name="อังคาร",     value="1"),
        app_commands.Choice(name="พุธ",        value="2"),
        app_commands.Choice(name="พฤหัสบดี",  value="3"),
        app_commands.Choice(name="ศุกร์",      value="4"),
        app_commands.Choice(name="เสาร์",      value="5"),
        app_commands.Choice(name="อาทิตย์",    value="6"),
    ],
)
@app_commands.checks.has_role("Admin")
async def add_recurring(
    interaction: discord.Interaction,
    name: str,
    cat: str,
    frequency: app_commands.Choice[str],
    weekday: app_commands.Choice[str],
    start_date: str = None,
    end_date: str = None,
    detail: str = None,
):
    cat = cat.strip().lower()
    if cat not in load_categories():
        valid = ", ".join(sorted(load_categories()))
        await interaction.response.send_message(
            f"หมวดหมู่ไม่ถูกต้อง ใช้ได้: {valid}", ephemeral=True)
        return

    today = date.today()
    if start_date:
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            await interaction.response.send_message(
                "รูปแบบวันที่เริ่มต้นไม่ถูกต้อง ใช้ YYYY-MM-DD", ephemeral=True)
            return
    else:
        start_date = today.strftime("%Y-%m-%d")

    if end_date:
        try:
            datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            await interaction.response.send_message(
                "รูปแบบวันที่สิ้นสุดไม่ถูกต้อง ใช้ YYYY-MM-DD", ephemeral=True)
            return

    await interaction.response.defer(ephemeral=True)

    rec = {
        "id":             f"rec_{uuid.uuid4().hex[:8]}",
        "name":           name.strip(),
        "cat":            cat,
        "recurrence":     frequency.value,
        "weekday":        int(weekday.value),
        "start_date":     start_date,
        "end_date":       end_date or None,
        "excluded_dates": [],
    }
    if detail and detail.strip():
        rec["detail"] = detail.strip()

    recurring = load_recurring()
    recurring.append(rec)
    save_recurring(recurring)

    freq_label = "ทุกสัปดาห์" if frequency.value == "weekly" else "ทุก 2 สัปดาห์"
    wd_label   = TH_WEEKDAYS[int(weekday.value)]
    try:
        await post_two_month_calendar()
        await interaction.followup.send(
            f"✅ เพิ่มกิจกรรมประจำ **{name}** ({freq_label} วัน{wd_label}) เริ่ม {start_date} แล้วครับ",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ บันทึกแล้ว แต่อัปเดตปฏิทินไม่สำเร็จ: {e}", ephemeral=True)


# =============================================
#  Slash command: /skip-event
# =============================================
async def recurring_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Return all recurring series as autocomplete choices."""
    choices = []
    for rec in load_recurring():
        wd_idx = rec.get("weekday")
        rec_id = rec.get("id")
        if wd_idx is None or not (0 <= wd_idx < len(TH_WEEKDAYS)) or not rec_id:
            continue
        freq  = "ทุกสัปดาห์" if rec.get("recurrence") == "weekly" else "ทุก 2 สัปดาห์"
        wd    = TH_WEEKDAYS[wd_idx]
        label = f"{rec.get('name','?')} ({freq} วัน{wd})"
        if current.lower() in label.lower() or current == "":
            choices.append(app_commands.Choice(name=label[:100], value=rec_id[:100]))
    return choices[:25]


@tree.command(
    name="skip-event",
    description="ข้ามกิจกรรมประจำในวันที่ระบุ เช่น วันหยุดนักขัตฤกษ์ (Admin เท่านั้น)",
)
@app_commands.describe(
    event="เลือกกิจกรรมประจำที่ต้องการข้าม",
    date_str="วันที่ต้องการข้าม (YYYY-MM-DD)",
)
@app_commands.autocomplete(event=recurring_autocomplete)
@app_commands.checks.has_role("Admin")
async def skip_event(
    interaction: discord.Interaction,
    event: str,
    date_str: str,
):
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        await interaction.response.send_message(
            "รูปแบบวันที่ไม่ถูกต้อง ใช้ YYYY-MM-DD", ephemeral=True)
        return

    recurring = load_recurring()
    rec = next((r for r in recurring if r["id"] == event), None)
    if not rec:
        await interaction.response.send_message(
            "ไม่พบกิจกรรมประจำนี้ กรุณาเลือกจาก autocomplete", ephemeral=True)
        return

    if date_str in rec.get("excluded_dates", []):
        await interaction.response.send_message(
            f"วันที่ {date_str} ถูกข้ามไปแล้ว", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    rec.setdefault("excluded_dates", []).append(date_str)
    save_recurring(recurring)

    try:
        await post_two_month_calendar()
        await interaction.followup.send(
            f"⏭️ ข้ามกิจกรรม **{rec['name']}** วันที่ {date_str} แล้วครับ",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ บันทึกแล้ว แต่อัปเดตปฏิทินไม่สำเร็จ: {e}", ephemeral=True)


# =============================================
#  Slash command: /remind  (personal DM reminder)
# =============================================
async def event_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Return future events as autocomplete choices (date|name), sorted ascending by date."""
    events = sorted(load_events(EVENTS_FILE), key=lambda e: e["date"])
    today = datetime.now(BANGKOK_TZ).date()
    choices = []
    for e in events:
        ev_date = datetime.strptime(e["date"], "%Y-%m-%d").date()
        if ev_date < today:
            continue
        th_month = TH_MONTHS[ev_date.month]
        label = f"{ev_date.day} {th_month} — {e['name']}"
        value = f"{e['date']}|{e['name']}"
        if current.lower() in label.lower() or current == "":
            choices.append(app_commands.Choice(name=label[:100], value=value[:100]))
    return choices[:25]


@tree.command(
    name="remind",
    description="ตั้งแจ้งเตือนกิจกรรมผ่าน DM",
)
@app_commands.describe(
    event="เลือกกิจกรรมที่ต้องการแจ้งเตือน",
    timing="แจ้งเตือนก่อนถึงวันกิจกรรมเท่าไหร่",
)
@app_commands.choices(timing=[
    app_commands.Choice(name="1 ชั่วโมงก่อน (05:00 ของวันนั้น)",      value="60"),
    app_commands.Choice(name="3 ชั่วโมงก่อน (03:00 ของวันนั้น)",      value="180"),
    app_commands.Choice(name="1 วันก่อน (06:00 ของวันก่อนหน้า)",       value="1440"),
    app_commands.Choice(name="3 วันก่อน (06:00 สามวันก่อน)",          value="4320"),
    app_commands.Choice(name="1 สัปดาห์ก่อน (06:00 เจ็ดวันก่อน)",    value="10080"),
])
@app_commands.autocomplete(event=event_autocomplete)
async def remind(
    interaction: discord.Interaction,
    event: str,
    timing: app_commands.Choice[str],
):
    # Parse the event value "YYYY-MM-DD|name"
    parts = event.split("|", 1)
    if len(parts) != 2:
        await interaction.response.send_message(
            "กรุณาเลือกกิจกรรมจากรายการที่แสดง ไม่สามารถพิมพ์เองได้", ephemeral=True)
        return

    event_date_str, event_name = parts[0].strip(), parts[1].strip()
    try:
        event_date = datetime.strptime(event_date_str, "%Y-%m-%d").date()
    except ValueError:
        await interaction.response.send_message("รูปแบบวันที่ไม่ถูกต้อง", ephemeral=True)
        return

    # Look up event detail & category from events.json
    events = load_events(EVENTS_FILE)
    event_data = next(
        (e for e in events if e["date"] == event_date_str and e["name"] == event_name), None
    )
    event_detail = event_data.get("detail", "") if event_data else ""
    event_cat    = event_data.get("cat", "")    if event_data else ""

    # Calculate remind_at: X minutes before 06:00 UTC+7 on event day
    minutes = int(timing.value)
    event_6am = datetime(event_date.year, event_date.month, event_date.day, 6, 0, 0,
                         tzinfo=BANGKOK_TZ)
    remind_at = event_6am - timedelta(minutes=minutes)

    # Reject past reminders
    now_bkk = datetime.now(BANGKOK_TZ)
    if remind_at <= now_bkk:
        await interaction.response.send_message(
            "เวลาแจ้งเตือนนั้นผ่านมาแล้ว กรุณาเลือกช่วงเวลาอื่น", ephemeral=True)
        return

    # Guard against duplicates (same user + event + not yet sent)
    reminders = load_reminders()
    duplicate = any(
        r["user_id"] == interaction.user.id
        and r["event_date"] == event_date_str
        and r["event_name"] == event_name
        and not r.get("sent")
        for r in reminders
    )
    if duplicate:
        await interaction.response.send_message(
            f"คุณตั้งแจ้งเตือน **{event_name}** ไว้แล้ว ใช้ `/my-reminders` เพื่อดูหรือยกเลิก",
            ephemeral=True,
        )
        return

    reminders.append({
        "user_id":      interaction.user.id,
        "event_date":   event_date_str,
        "event_name":   event_name,
        "event_cat":    event_cat,
        "event_detail": event_detail,
        "remind_at":    remind_at.isoformat(),
        "sent":         False,
    })
    save_reminders(reminders)

    remind_display = remind_at.strftime("%d/%m/%Y %H:%M")
    th_month = TH_MONTHS[event_date.month]
    await interaction.response.send_message(
        f"✅ ตั้งแจ้งเตือน **{event_name}** ({event_date.day} {th_month} {event_date.year + 543})\n"
        f"บอทจะ DM หาคุณวันที่ **{remind_display} น.** (UTC+7)",
        ephemeral=True,
    )


# =============================================
#  /my-reminders: view + cancel personal reminders
# =============================================
class CancelReminderSelect(discord.ui.Select):
    """Dropdown listing the user's active reminders so they can cancel one."""

    def __init__(self, reminders: list):
        options = []
        for r in reminders:
            ev_date  = datetime.strptime(r["event_date"], "%Y-%m-%d").date()
            th_month = TH_MONTHS[ev_date.month]
            label    = f"❌  {r['event_name'][:55]}"
            value    = f"{r['event_date']}|{r['event_name'][:60]}"
            desc     = f"{ev_date.day} {th_month} {ev_date.year + 543}"
            options.append(discord.SelectOption(
                label=label[:100],
                value=value[:100],
                description=desc[:100],
            ))
        super().__init__(
            placeholder="เลือกการแจ้งเตือนที่ต้องการยกเลิก...",
            options=options[:25],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        parts = self.values[0].split("|", 1)
        if len(parts) != 2:
            await interaction.response.send_message("เกิดข้อผิดพลาด", ephemeral=True)
            return
        ev_date_str, ev_name = parts

        reminders = load_reminders()
        before    = len(reminders)
        reminders = [r for r in reminders if not (
            r["user_id"] == interaction.user.id
            and r["event_date"] == ev_date_str
            and r["event_name"] == ev_name
            and not r.get("sent")
        )]

        if len(reminders) < before:
            save_reminders(reminders)
            await interaction.response.send_message(
                f"✅ ยกเลิกการแจ้งเตือน **{ev_name}** แล้ว", ephemeral=True)
        else:
            await interaction.response.send_message(
                "ไม่พบการแจ้งเตือนนี้ อาจถูกส่งไปแล้ว", ephemeral=True)


class MyRemindersView(discord.ui.View):
    def __init__(self, reminders: list):
        super().__init__(timeout=300)
        self.add_item(CancelReminderSelect(reminders))


@tree.command(
    name="my-reminders",
    description="ดูและยกเลิกการแจ้งเตือนส่วนตัวที่ตั้งไว้",
)
async def my_reminders(interaction: discord.Interaction):
    active = [
        r for r in load_reminders()
        if r["user_id"] == interaction.user.id and not r.get("sent")
    ]

    if not active:
        await interaction.response.send_message(
            "คุณยังไม่มีการแจ้งเตือนที่ตั้งไว้\n"
            "ใช้ `/remind` หรือกดเมนูบนโพสต์กิจกรรมเพื่อเพิ่ม",
            ephemeral=True,
        )
        return

    cat_labels = load_cat_labels()
    embed = discord.Embed(
        title="🔔  การแจ้งเตือนของคุณ",
        description=f"มีการแจ้งเตือนที่รอส่ง **{len(active)}** รายการ",
        color=0x534AB7,
    )
    for r in active:
        ev_date    = datetime.strptime(r["event_date"], "%Y-%m-%d").date()
        th_month   = TH_MONTHS[ev_date.month]
        cat_label  = cat_labels.get(r.get("event_cat", ""), r.get("event_cat", ""))
        remind_at  = datetime.fromisoformat(r["remind_at"])
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=timezone.utc)
        remind_bkk = remind_at.astimezone(BANGKOK_TZ).strftime("%d/%m/%Y %H:%M")
        embed.add_field(
            name=f"📌  {r['event_name']}",
            value=(
                f"🏷️ {cat_label}\n"
                f"📅 {ev_date.day} {th_month} {ev_date.year + 543}\n"
                f"⏰ แจ้งเตือน: **{remind_bkk} น.**"
            ),
            inline=False,
        )
    embed.set_footer(text="ใช้เมนูด้านล่างเพื่อยกเลิกการแจ้งเตือน")

    view = MyRemindersView(active)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# =============================================
#  Slash command: /set-dress-schedule  (weekday default)
# =============================================
@tree.command(
    name="set-dress-schedule",
    description="ตั้งชุดแต่งกายประจำวันในสัปดาห์ (Admin เท่านั้น)",
)
@app_commands.describe(
    weekday="วันในสัปดาห์",
    dress="ชื่อชุดที่ต้องสวมใส่ เช่น ชุดนักเรียน / ชุดพลศึกษา",
    note="หมายเหตุเพิ่มเติม (ไม่บังคับ)",
)
@app_commands.choices(weekday=[
    app_commands.Choice(name="จันทร์",    value="0"),
    app_commands.Choice(name="อังคาร",    value="1"),
    app_commands.Choice(name="พุธ",       value="2"),
    app_commands.Choice(name="พฤหัสบดี", value="3"),
    app_commands.Choice(name="ศุกร์",     value="4"),
])
@app_commands.checks.has_role("Admin")
async def set_dress_schedule(
    interaction: discord.Interaction,
    weekday: app_commands.Choice[str],
    dress: str,
    note: str = None,
):
    dc = load_dresscode()
    dc.setdefault("schedule", {})[weekday.value] = {"dress": dress.strip()}
    if note and note.strip():
        dc["schedule"][weekday.value]["note"] = note.strip()
    save_dresscode(dc)

    wd_label = TH_WEEKDAYS[int(weekday.value)]
    await interaction.response.send_message(
        f"✅ ตั้งชุดวัน**{wd_label}** → **{dress.strip()}** แล้วครับ",
        ephemeral=True,
    )


# =============================================
#  Slash command: /set-dress  (specific date override)
# =============================================
@tree.command(
    name="set-dress",
    description="ตั้งชุดแต่งกายสำหรับวันที่ระบุ ทับค่าประจำสัปดาห์ (Admin เท่านั้น)",
)
@app_commands.describe(
    date_str="วันที่ รูปแบบ YYYY-MM-DD เช่น 2026-05-01",
    dress="ชื่อชุดที่ต้องสวมใส่",
    note="หมายเหตุเพิ่มเติม (ไม่บังคับ)",
)
@app_commands.checks.has_role("Admin")
async def set_dress(
    interaction: discord.Interaction,
    date_str: str,
    dress: str,
    note: str = None,
):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        await interaction.response.send_message(
            "รูปแบบวันที่ไม่ถูกต้อง ใช้ YYYY-MM-DD", ephemeral=True)
        return

    dc = load_dresscode()
    dc.setdefault("overrides", {})[date_str] = {"dress": dress.strip()}
    if note and note.strip():
        dc["overrides"][date_str]["note"] = note.strip()
    save_dresscode(dc)

    await interaction.response.send_message(
        f"✅ ตั้งชุดวันที่ **{fmt_thai_date(d)}** → **{dress.strip()}** แล้วครับ",
        ephemeral=True,
    )


# =============================================
#  Slash command: /post-dress  (manual trigger)
# =============================================
@tree.command(
    name="post-dress",
    description="โพสต์แจ้งเครื่องแต่งกายทันที (Admin เท่านั้น)",
)
@app_commands.checks.has_role("Admin")
async def post_dress(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await post_dress_code(force=True)
        await interaction.followup.send("✅ โพสต์แจ้งเครื่องแต่งกายแล้วครับ", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาด: {e}", ephemeral=True)


# =============================================
#  Slash command: /test-dress  (post to test channel)
# =============================================
@tree.command(
    name="test-dress",
    description="ทดสอบโพสต์แจ้งเครื่องแต่งกายในช่องทดสอบ (Admin เท่านั้น)",
)
@app_commands.checks.has_role("Admin")
async def test_dress(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        test_channel = client.get_channel(TEST_CHANNEL_ID)
        if not test_channel:
            test_channel = await client.fetch_channel(TEST_CHANNEL_ID)

        today = datetime.now(BANGKOK_TZ).date()
        embed = build_dress_embed(today, test_mode=True)
        await test_channel.send(embed=embed)
        await interaction.followup.send("✅ ส่งไปช่องทดสอบแล้วครับ", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาด: {e}", ephemeral=True)


# =============================================
#  Slash command: /test-calendar  (post to test channel)
# =============================================
@tree.command(
    name="test-calendar",
    description="ทดสอบโพสต์ปฏิทินในช่องทดสอบ (Admin เท่านั้น)",
)
@app_commands.describe(
    month="เดือนเริ่มต้น (1-12) ถ้าไม่ระบุ = เดือนปัจจุบัน",
    year="ปี ค.ศ. ถ้าไม่ระบุ = ปีปัจจุบัน",
)
@app_commands.checks.has_role("Admin")
async def test_calendar(
    interaction: discord.Interaction,
    month: int = None,
    year: int = None,
):
    await interaction.response.defer(ephemeral=True)
    try:
        test_channel = client.get_channel(TEST_CHANNEL_ID)
        if not test_channel:
            test_channel = await client.fetch_channel(TEST_CHANNEL_ID)
        today = datetime.now(BANGKOK_TZ).date()
        month = month or today.month
        year  = year  or today.year
        embed, file = await build_two_month_calendar(year, month, test_mode=True)
        await test_channel.send(file=file, embed=embed)
        await interaction.followup.send("✅ ส่งปฏิทินไปช่องทดสอบแล้วครับ", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาด: {e}", ephemeral=True)


def build_agenda_embed(today: date, test_mode: bool = False) -> discord.Embed | None:
    """Build the 14-day agenda embed; returns None when no events in range."""
    end_date = today + timedelta(days=13)

    day_map: dict = {}
    for e in load_events(EVENTS_FILE):
        if not e.get("date"):
            continue
        ev_start = datetime.strptime(e["date"], "%Y-%m-%d").date()
        ev_end   = datetime.strptime(e.get("end_date", e["date"]), "%Y-%m-%d").date()
        if ev_end < today or ev_start > end_date:
            continue
        first_day = max(ev_start, today)
        day_map.setdefault(first_day, []).append(e)

    months = set()
    d = today
    while d <= end_date:
        months.add((d.year, d.month))
        d += timedelta(days=1)
    for rec in load_recurring():
        for y, m in months:
            for ev in expand_recurring_for_month(rec, y, m):
                ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
                if today <= ev_date <= end_date:
                    day_map.setdefault(ev_date, []).append(ev)

    if not day_map:
        return None

    cat_labels = load_cat_labels()
    cat_ansi   = load_cat_ansi()
    lines: list[str] = []
    for d in sorted(day_map.keys()):
        wd          = TH_WEEKDAYS[d.weekday()]
        date_header = f"วัน{wd}ที่ {d.day} {TH_MONTHS[d.month]} {d.year + 543}"
        if d == today:
            date_header += "  ◀ วันนี้"
        lines.append(_ansi(date_header, "37", bold=True))
        lines.append("─" * 34)
        for ev in day_map[d]:
            cat_key = ev.get("cat", "")
            code    = cat_ansi.get(cat_key, "37")
            cat_lbl = cat_labels.get(cat_key, cat_key)
            detail  = ev.get("detail", "")
            if ev.get("end_date") and ev["end_date"] != ev["date"]:
                ev_s = datetime.strptime(ev["date"], "%Y-%m-%d").date()
                ev_e = datetime.strptime(ev["end_date"], "%Y-%m-%d").date()
                if ev_s.month == ev_e.month:
                    date_lbl = f"{ev_s.day}–{ev_e.day} {TH_MONTHS[ev_s.month]}"
                else:
                    date_lbl = f"{ev_s.day} {TH_MONTHS[ev_s.month]} – {ev_e.day} {TH_MONTHS[ev_e.month]}"
            else:
                date_lbl = f"{d.day} {TH_MONTHS[d.month]}"
            lines.append(_ansi(f"  ●  {ev['name']}", code, bold=True))
            lines.append(_ansi(f"     {cat_lbl}  ·  {date_lbl}", code, dim=True))
            if detail:
                lines.append(_ansi(f"     ↳  {detail}", code, dim=True))
        lines.append("")

    description = "```ansi\n" + "\n".join(lines) + "```"
    if len(description) > 4000:
        description = description[:3990] + "\n…```"

    tag = "[TEST] " if test_mode else ""
    title = (
        f"{tag}📋  กิจกรรม 14 วันข้างหน้า  —  "
        f"{today.day} {TH_MONTHS[today.month]} – "
        f"{end_date.day} {TH_MONTHS[end_date.month]} {today.year + 543}"
    )
    color = 0x95A5A6 if test_mode else 0x534AB7
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=f"{tag}อัปเดตล่าสุด: {datetime.now(BANGKOK_TZ).strftime('%d/%m/%Y %H:%M')} น.")
    return embed


# =============================================
#  Slash command: /agenda  (next 14 days)
# =============================================
@tree.command(
    name="agenda",
    description="แสดงกิจกรรมใน 14 วันข้างหน้าแบบละเอียด",
)
async def agenda(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    today    = datetime.now(BANGKOK_TZ).date()
    end_date = today + timedelta(days=13)
    embed = build_agenda_embed(today)
    if embed is None:
        await interaction.followup.send(
            f"ไม่มีกิจกรรมในช่วง {today.day} {TH_MONTHS[today.month]} – "
            f"{end_date.day} {TH_MONTHS[end_date.month]} {today.year + 543}",
            ephemeral=True,
        )
        return
    await interaction.followup.send(embed=embed, ephemeral=True)


# =============================================
#  Slash command: /test-agenda  (post to test channel)
# =============================================
@tree.command(
    name="test-agenda",
    description="ทดสอบโพสต์ agenda ในช่องทดสอบ (Admin เท่านั้น)",
)
@app_commands.checks.has_role("Admin")
async def test_agenda(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        test_channel = client.get_channel(TEST_CHANNEL_ID)
        if not test_channel:
            test_channel = await client.fetch_channel(TEST_CHANNEL_ID)
        today    = datetime.now(BANGKOK_TZ).date()
        end_date = today + timedelta(days=13)
        embed = build_agenda_embed(today, test_mode=True)
        if embed is None:
            await test_channel.send(
                f"[TEST] ไม่มีกิจกรรมในช่วง {today.day} {TH_MONTHS[today.month]} – "
                f"{end_date.day} {TH_MONTHS[end_date.month]} {today.year + 543}"
            )
        else:
            await test_channel.send(embed=embed)
        await interaction.followup.send("✅ ส่งไปช่องทดสอบแล้วครับ", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาด: {e}", ephemeral=True)


# =============================================
#  on_message — auto-process file uploads in resources channel
# =============================================
@client.event
async def on_message(message: discord.Message):
    """When an admin uploads a PDF or image to the resources channel, extract text and thread it."""
    if message.author.bot:
        return
    if not RESOURCES_CHANNEL_ID or message.channel.id != RESOURCES_CHANNEL_ID:
        return
    if not message.attachments:
        return

    # Admin must have the Admin role
    if not any(r.name == "Admin" for r in message.author.roles):
        return

    SUPPORTED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}

    async with aiohttp.ClientSession() as session:
        for attachment in message.attachments:
            ext = Path(attachment.filename).suffix.lower()
            if ext not in SUPPORTED_EXT:
                continue

            # Download file bytes
            async with session.get(attachment.url) as resp:
                if resp.status != 200:
                    continue
                data = await resp.read()

            # Extract text
            if ext == ".pdf":
                content = extract_text_from_pdf(data)
                source = "other"
            else:
                content = ""
                source = "other"

            # Use the admin's message text as title if provided, otherwise filename
            title = message.content.strip() if message.content.strip() else Path(attachment.filename).stem
            date_str = datetime.now(BANGKOK_TZ).strftime("%Y-%m-%d")

            channel = message.channel
            msg_id, thread_id = await post_resource_to_channel(
                channel, title, source, date_str, content,
                filename=attachment.filename, file_data=data,
            )

            resources = load_resources()
            resources.append({
                "id":         str(uuid.uuid4()),
                "title":      title,
                "source":     source,
                "date":       date_str,
                "content":    content,
                "message_id": msg_id,
                "thread_id":  thread_id,
                "filename":   attachment.filename,
                "added_at":   datetime.now(BANGKOK_TZ).isoformat(),
            })
            save_resources(resources)
            print(f"[resources] Processed '{attachment.filename}' → thread {thread_id}")

    # Delete the raw upload so the channel only shows the clean embed cards
    try:
        await message.delete()
    except discord.HTTPException:
        pass


# =============================================
#  Slash command: /help — parent-facing guide
# =============================================
@tree.command(
    name="help",
    description="วิธีใช้งานบอท — คำสั่งที่มีสำหรับผู้ปกครอง",
)
async def help_command(interaction: discord.Interaction):
    is_admin = any(r.name == "Admin" for r in interaction.user.roles)

    embed = discord.Embed(
        title="📖  วิธีใช้งานบอท BCISB",
        description=(
            "บอทนี้ช่วยให้ผู้ปกครองรับข้อมูลกิจกรรมและเครื่องแต่งกาย\n"
            "จากห้องเรียนได้สะดวกขึ้นครับ"
        ),
        color=0x534AB7,
    )

    embed.add_field(
        name="📅  /calendar",
        value="ดูปฏิทินกิจกรรม 2 เดือน (เดือนนี้ + เดือนหน้า)",
        inline=False,
    )
    embed.add_field(
        name="📋  /agenda",
        value="ดูกิจกรรม 14 วันข้างหน้าแบบละเอียด พร้อมรายละเอียดและหมวดหมู่",
        inline=False,
    )
    embed.add_field(
        name="🔔  /remind",
        value="ตั้งแจ้งเตือนกิจกรรมผ่าน DM ส่วนตัว\nเลือกได้ว่าจะให้แจ้งก่อน 1 ชม. / 1 วัน / 3 วัน / 1 สัปดาห์",
        inline=False,
    )
    embed.add_field(
        name="📋  /my-reminders",
        value="ดูรายการแจ้งเตือนที่ตั้งไว้ และยกเลิกได้",
        inline=False,
    )

    if is_admin:
        embed.add_field(name="\u200b", value="**⚙️  คำสั่ง Admin**", inline=False)
        admin_cmds = (
            "`/add-event` — เพิ่มกิจกรรม\n"
            "`/remove-event` — ลบกิจกรรม\n"
            "`/add-recurring` — เพิ่มกิจกรรมประจำ\n"
            "`/skip-event` — ข้ามกิจกรรมประจำ\n"
            "`/set-dress-schedule` — ตั้งชุดประจำวันในสัปดาห์\n"
            "`/set-dress` — ตั้งชุดวันพิเศษ\n"
            "`/post-dress` — โพสต์แจ้งเครื่องแต่งกายทันที"
        )
        embed.add_field(name="รายการ", value=admin_cmds, inline=False)

        test_cmds = (
            "`/test-dress` — ทดสอบโพสต์แจ้งเครื่องแต่งกายในช่องทดสอบ\n"
            "`/test-calendar` — ทดสอบโพสต์ปฏิทินในช่องทดสอบ\n"
            "`/test-agenda` — ทดสอบโพสต์ agenda ในช่องทดสอบ"
        )
        embed.add_field(name="🧪  คำสั่งทดสอบ", value=test_cmds, inline=False)

    embed.set_footer(text="บอทจะส่งแจ้งเตือนกิจกรรมทุกเช้า 06:00 น. อัตโนมัติ")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# =============================================
#  on_member_join — welcome DM for new parents
# =============================================
@client.event
async def on_member_join(member: discord.Member):
    """Send a welcome DM when a new member joins the server."""
    if member.bot:
        return

    welcome_text = (
        f"สวัสดีครับ/ค่ะ **{member.display_name}** 🙏\n"
        f"ยินดีต้อนรับสู่ห้อง **{member.guild.name}**!\n\n"
        "ช่องทางนี้ใช้สำหรับรับข้อมูลจากห้องเรียนครับ\n\n"
        "📅  **ปฏิทินกิจกรรม** — ดูได้ในช่อง calendar\n"
        "      บอทจะส่งรายการกิจกรรมวันนี้ทุกเช้า 06:00 น.\n\n"
        "👗  **เครื่องแต่งกาย** — ดูได้ในช่องชุดนักเรียน\n"
        "      บอทจะบอกชุดวันนี้และพรุ่งนี้ทุกเช้า\n\n"
        "🔔  พิมพ์ `/remind` เพื่อตั้งแจ้งเตือนส่วนตัวก่อนถึงวันกิจกรรม\n"
        "📋  พิมพ์ `/my-reminders` เพื่อดูแจ้งเตือนที่ตั้งไว้\n"
        "📖  พิมพ์ `/help` เพื่อดูคำสั่งทั้งหมด\n\n"
        "❓  มีคำถามพิมพ์ได้ในช่องถามตอบครับ"
    )
    try:
        await member.send(welcome_text)
        print(f"[welcome] Sent welcome DM to {member.display_name} ({member.id})")
    except discord.Forbidden:
        print(f"[welcome] Cannot DM {member.display_name} ({member.id}) — DMs disabled")
    except Exception as e:
        print(f"[welcome] Error sending DM to {member.id}: {e}")


# =============================================
#  Global error handler — friendly Thai messages
# =============================================
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Catch unexpected slash-command errors and show a friendly message."""
    if isinstance(error, app_commands.MissingRole):
        msg = "คุณต้องมี role **Admin** จึงจะใช้คำสั่งนี้ได้"
    else:
        print(f"[error] Command /{interaction.command.name if interaction.command else '?'}: {error}")
        msg = "❌ เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้ง หากยังมีปัญหา แจ้ง Admin ได้เลยครับ"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass  # If we can't respond at all, just log it


# =============================================
#  on_ready — sync to guild only (no duplicates)
# =============================================
@client.event
async def on_ready():
    print(f"Bot online: {client.user}")

    # Find the guild from the bot's guild list (doesn't rely on channel cache)
    guild = None
    if client.guilds:
        guild = client.guilds[0]

    if guild:
        # Sync all commands to the guild instantly (guild syncs take effect immediately)
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        print(f"Synced {len(synced)} command(s) to guild: {guild.name}")
    else:
        synced = await tree.sync()
        print(f"Synced {len(synced)} command(s) globally")

    for loop in (daily_calendar_school, daily_calendar_holiday,
                 delete_daily_reminder, check_dm_reminders, daily_dress_reminder):
        if not loop.is_running():
            loop.start()

    # Catch-up: if we missed today's auto-post window (or it's the first run
    # after rolling over midnight Bangkok time), post immediately.
    now_bkk = datetime.now(BANGKOK_TZ).date()
    today_key = now_bkk.strftime("%Y-%m-%d")
    last_post = load_state().get("last_calendar_post")
    if last_post != today_key:
        try:
            await post_daily_calendar(now_bkk)
            print(f"[on_ready] Catch-up calendar posted for {today_key}")
        except Exception as e:
            print(f"[on_ready] Catch-up calendar failed: {e}")


# ---- Run ----
client.run(TOKEN)
