"""PowerSchool teacher-message poller.

Reads the homeroom teacher's messages out of PowerSchool Messaging and returns
them for the bot to relay into Discord.

The messages are NOT in the classic /guardian/ parent portal. They live in
PowerSchool Messaging (a Sendbird chat) surfaced through MyPowerHub, reached
by SSO from the same SIS session. Full route in MESSAGING FLOW below.

Why Playwright and not aiohttp: the sign-in form at /public/home.html carries a
hidden `dbpw` field that PowerSchool's own JavaScript fills with a hash of the
password before submitting. A plain HTTP POST of account/pw therefore does not
authenticate — the real form has to run. Playwright and Chromium are already
dependencies of this project (calendar_render.py), so this adds nothing new.

Confirmed against https://bcisb.powerschool.com on 2026-08-28:
  form#LoginForm  POST -> /guardian/home.html
  input[name=account]  (#fieldAccount)
  input[name=pw]       (#fieldPassword)
  button#btn-enter-sign-in
  signin-guardian-saml-login = 0  -> guardians use plain user/pass, no SSO.
"""

import os
import re
import json
import time
import asyncio
import hashlib
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_BASE_URL = "https://bcisb.powerschool.com"

# Read lazily, never at import time: bot.py imports this module before it calls
# load_dotenv(), so module-level os.getenv() would capture None.
def ps_base_url() -> str:
    return os.getenv("POWERSCHOOL_URL", DEFAULT_BASE_URL).rstrip("/")

def login_url() -> str:
    return f"{ps_base_url()}/public/home.html"

def home_url() -> str:
    return f"{ps_base_url()}/guardian/home.html"

# Playwright's default 30s is tight for a school portal on a home uplink.
NAV_TIMEOUT_MS = 45_000

# The messages do NOT live in the classic /guardian/ portal. They live in
# PowerSchool Messaging, reached through MyPowerHub, which single-signs-on from
# the same SIS session. See MESSAGING FLOW below.
def hub_url() -> str:
    return os.getenv("POWERSCHOOL_HUB_URL",
                     "https://bcisb.guardian.powerschool.com").rstrip("/")

# Selectors confirmed against the live app on 2026-08-28. PowerSchool Messaging
# is a Sendbird chat wrapped in PowerSchool's own "messenger-inbox" classes.
SEL_MESSAGING_BTN = "button[aria-label='Messaging']"
SEL_MSG_LIST      = ".messenger-inbox__conversation__message-list"
SEL_MESSAGE       = ".messenger-inbox__message-content"
SEL_SENDER        = ".messenger-inbox__message-content__middle__sender-name"
SEL_BODY          = ".messenger-inbox__message-content__middle__body-container"
SEL_CREATED_AT    = ".messenger-inbox__message-content__middle__body-container__created-at"
SEL_DATE_SEP      = ".messenger-inbox__conversation__date-separator__label"
SEL_ADMIN_MSG     = ".messenger-inbox__admin-message, .sendbird-admin-message"

# Pages worth checking even when nothing links to them, for explore().
CANDIDATE_PATHS = [
    "/guardian/home.html",
    "/guardian/bulletin.html",            # "School Bulletin" — likeliest home
    "/guardian/messages.html",
    "/guardian/teachercomments.html",
    "/guardian/school_information.html",
    "/guardian/myschedule.html",
    "/guardian/email_notifications.html",
]


class PowerSchoolError(RuntimeError):
    """Raised for login failures and unexpected portal states."""


# ---------------------------------------------------------------------------
#  Login
# ---------------------------------------------------------------------------
async def _page_complaint(page) -> str:
    """Best-effort snippet of whatever the page is saying, for error messages."""
    for sel in (".feedback-alert", ".error", "#login-error", "[role='alert']"):
        try:
            el = await page.query_selector(sel)
            if el:
                txt = (await el.inner_text()).strip()
                if txt:
                    return txt[:200]
        except Exception:
            pass
    try:
        return (await page.inner_text("body"))[:200].replace("\n", " ")
    except Exception:
        return "<no page text>"


async def _login(page) -> None:
    """Sign in to the guardian portal. Raises PowerSchoolError on failure."""
    user = os.getenv("POWERSCHOOL_USER")
    password = os.getenv("POWERSCHOOL_PASS")
    if not user or not password:
        raise PowerSchoolError(
            "POWERSCHOOL_USER / POWERSCHOOL_PASS not set in .env")

    await page.goto(login_url(), timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    await page.fill("input[name='account']", user)
    await page.fill("input[name='pw']", password)
    await page.click("#btn-enter-sign-in")

    # Do NOT trust the URL alone: the form's own action is /guardian/home.html,
    # so a REJECTED login also lands on a /guardian/ URL and would look like
    # success. Since this runs unattended nightly, that mistake would show up
    # only as "posted 0 new message(s)" forever. Require both that we are in
    # /guardian/ and that the sign-in form is gone.
    deadline = time.monotonic() + NAV_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        try:
            in_portal = "/guardian/" in page.url
            still_asking = await page.query_selector("input[name='pw']") is not None
        except Exception:
            continue          # mid-navigation; try again
        if in_portal and not still_asking:
            return

    raise PowerSchoolError(
        f"Login not confirmed (url={page.url!r}, sign-in form still present) — "
        f"check POWERSCHOOL_USER/POWERSCHOOL_PASS. Page said: "
        f"{await _page_complaint(page)!r}")


# ---------------------------------------------------------------------------
#  Discovery helper
#
#  The user sees the homeroom teacher's message in the PowerSchool mobile app
#  but could not find it in the web portal, so the page that carries it is not
#  yet known. Rather than guess at selectors, this dumps the authenticated
#  portal so the real location can be identified from evidence.
#
#  It crawls TWICE — once as a desktop browser, once as an iPhone — because the
#  app/web split is often just responsive rendering: PowerSchool serves
#  different content to a mobile user agent. Diff the two dumps.
#
#  Run standalone (not from the bot):
#      python powerschool.py explore
#  Output lands in ./ps_dump/{desktop,mobile}/ : index.json plus one .txt/page.
# ---------------------------------------------------------------------------
async def _crawl(context, out_dir: Path) -> dict:
    """Log in inside `context` and dump every reachable portal page."""
    out_dir.mkdir(parents=True, exist_ok=True)
    page = await context.new_page()
    result = {"base": ps_base_url(), "nav": [], "pages": []}

    await _login(page)

    links = await page.eval_on_selector_all(
        "a[href]",
        """els => els.map(a => ({
               text: (a.textContent || '').trim().slice(0, 80),
               href: a.getAttribute('href')
           }))""",
    )

    seen, targets = set(), []
    for l in links:
        href = (l.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        absolute = urljoin(page.url, href)
        if urlparse(absolute).netloc != urlparse(ps_base_url()).netloc:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        result["nav"].append({"text": l["text"], "url": absolute})
        # Never follow sign-out — it would end the session mid-crawl.
        if "logout" in absolute.lower() or "signout" in absolute.lower():
            continue
        targets.append((l["text"], absolute))

    # Add the unlinked candidates.
    for path in CANDIDATE_PATHS:
        absolute = ps_base_url() + path
        if absolute not in seen:
            seen.add(absolute)
            targets.append(("(unlinked candidate)", absolute))

    for text, url in targets:
        entry = {"text": text, "url": url}
        try:
            await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            body = await page.inner_text("body")
            entry["title"] = await page.title()
            entry["chars"] = len(body)
            # A page that bounced us back to sign-in isn't a real portal page.
            entry["kicked_to_login"] = "/public/" in page.url
            entry["final_url"] = page.url
            slug = re.sub(r"[^A-Za-z0-9]+", "_", urlparse(url).path).strip("_")
            fname = out_dir / f"{slug or 'root'}.txt"
            fname.write_text(f"# {text}\n# {url}\n# final: {page.url}\n\n{body}",
                             encoding="utf-8")
            entry["dump"] = fname.name
        except Exception as e:
            entry["error"] = str(e)[:200]
        result["pages"].append(entry)
        # Cheap insurance against tripping portal rate limiting.
        await asyncio.sleep(0.75)

    (out_dir / "index.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


async def explore(out_dir: Path = None) -> dict:
    """Crawl the portal as both a desktop and a mobile client."""
    out_dir = out_dir or (BASE_DIR / "ps_dump")
    results = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            profiles = {
                "desktop": {"viewport": {"width": 1280, "height": 900}},
                # The mobile app may simply be the portal's responsive view.
                "mobile": p.devices.get("iPhone 13", {
                    "viewport": {"width": 390, "height": 844},
                    "is_mobile": True,
                }),
            }
            for label, opts in profiles.items():
                context = await browser.new_context(**opts)
                try:
                    results[label] = await _crawl(context, out_dir / label)
                except Exception as e:
                    results[label] = {"error": str(e)[:300], "pages": [], "nav": []}
                finally:
                    await context.close()
        finally:
            await browser.close()

    (out_dir / "summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


# ---------------------------------------------------------------------------
#  MESSAGING FLOW  (confirmed against the live app 2026-08-28)
#
#  The teacher's messages are NOT in the classic /guardian/ portal — that is
#  why they could not be found there. They live in PowerSchool Messaging, a
#  Sendbird chat surfaced through MyPowerHub:
#
#    1. sign in to the SIS portal            (_login)
#    2. go to MyPowerHub                     (SSO off the same session)
#    3. click the header button[aria-label="Messaging"]   <- a BUTTON, not a
#       link, which is why it never appears in a nav-link scan
#    4. expand the "Classes" accordion       <- starts collapsed; the
#       conversations are not in the DOM until it is opened
#    5. click a conversation -> the message list renders and is readable
#
#  Sendbird's REST API sits behind this and would be sturdier than the DOM,
#  but it requires handling a third-party session token; the DOM inside the
#  already-authenticated Playwright session needs no extra credentials.
# ---------------------------------------------------------------------------

BANGKOK_TZ = timezone(timedelta(hours=7))

# Conversation rows in the left pane. Verified against the live DOM: the row is
#   div.messenger-inbox__messenger-channel-preview
#     div.messenger-channel-preview                     <- the visible preview
#     button.messenger-inbox__messenger-channel-preview__button   <- SIBLING
# The button is a *sibling* of the preview, not its ancestor, so the obvious
# "button:has(.messenger-channel-preview)" matches nothing. Measured: 0 hits.
SEL_CONV_BUTTON = "button.messenger-inbox__messenger-channel-preview__button"
# Used only if PowerSchool renames that class — any button inside the list.
SEL_CONV_BUTTON_FALLBACK = "ul.messenger-inbox__conversation-list button"

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# Zero-width characters PowerSchool sprinkles around timestamps.
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)


def _normalise_date_label(label: str, today: date):
    """Turn a date separator ("Today", "August 24") into an absolute date.

    This matters for correctness, not tidiness: the labels are relative, so
    hashing "Today" into message_id() would produce a different id tomorrow and
    the same message would be relayed again every single day.
    """
    l = (label or "").strip().lower()
    if l == "today":
        return today
    if l == "yesterday":
        return today - timedelta(days=1)
    m = re.match(r"([a-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?$", l)
    if not m:
        return None
    month = _MONTHS.get(m.group(1))
    if not month:
        return None
    year = int(m.group(3)) if m.group(3) else today.year
    try:
        d = date(year, month, int(m.group(2)))
    except ValueError:
        return None
    # With no explicit year, a date in the future means it was last year.
    if not m.group(3) and (d - today).days > 1:
        try:
            d = date(year - 1, month, int(m.group(2)))
        except ValueError:
            return None
    return d


def _compose_posted_at(d, time_str: str) -> str:
    """Best-effort ISO-8601 timestamp from a date plus a "5:00 PM" label."""
    if d is None:
        return (time_str or "").strip()
    t = (time_str or "").strip().upper().replace("\u202f", " ")
    m = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)?$", t)
    if not m:
        return d.isoformat()
    hour, minute = int(m.group(1)), int(m.group(2))
    ampm = m.group(3)
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return d.isoformat()
    return datetime(d.year, d.month, d.day, hour, minute,
                    tzinfo=BANGKOK_TZ).isoformat(timespec="seconds")


def _subject_from_body(body: str) -> str:
    """Sendbird messages have no subject; derive one for the embed title.

    Skips bare label lines ("NEWSLETTER AND PICTURES LINK:") and naked URLs,
    which the teacher tends to put first, so the title reads as a headline.
    """
    lines = [l.strip() for l in (body or "").splitlines() if l.strip()]
    for line in lines:
        if line.endswith(":"):
            continue
        if re.match(r"^(https?://|www\.)\S*$", line):
            continue
        return line[:120]
    return lines[0][:120] if lines else "ข้อความจากครู"


# Walks the rendered message list in document order. Sender names appear only
# on the FIRST message of a consecutive run by the same person, so the last
# seen name is carried forward. Date separators are interleaved with messages,
# which is why a single ordered pass is used rather than separate queries.
_EXTRACT_JS = """
() => {
  const LIST='%(list)s', MSG='%(msg)s', SENDER='%(sender)s',
        BODY='%(body)s', CREATED='%(created)s', SEP='%(sep)s', ADMIN='%(admin)s';
  const list = document.querySelector(LIST);
  if (!list) return null;
  const nodes = list.querySelectorAll(SEP + ',' + MSG + ',' + ADMIN);
  const out = [];
  let curDate = '', lastSender = '';
  for (const n of nodes) {
    if (n.matches(SEP)) { curDate = (n.innerText || '').trim(); continue; }
    if (n.matches(ADMIN)) continue;                 // "X and Y joined"
    const se = n.querySelector(SENDER);
    if (se) lastSender = (se.innerText || '').trim();
    const be = n.querySelector(BODY);
    if (!be) continue;
    const ce = n.querySelector(CREATED);
    const time = ce ? (ce.innerText || '').trim() : '';
    let body = (be.innerText || '').trim();
    if (time && body.endsWith(time)) body = body.slice(0, -time.length).trim();
    out.push({author: lastSender, body: body, time: time, date_label: curDate});
  }
  return out;
}
""" % {"list": SEL_MSG_LIST, "msg": SEL_MESSAGE, "sender": SEL_SENDER,
       "body": SEL_BODY, "created": SEL_CREATED_AT, "sep": SEL_DATE_SEP,
       "admin": SEL_ADMIN_MSG}


# Expands the collapsed conversation groups in the Messaging pane.
#
# Matching is done in JS on innerText rather than with get_by_role(name=...):
# the accordion's accessible name is "Classes0 unread messages", not
# "Classes" (measured), so an exact-name role lookup never matches. The unread
# suffix also disappears once expanded, so anchor on the prefix only.
# Raw string: the JS regexes contain backslash escapes that Python must not touch.
_EXPAND_GROUPS_JS = r"""
() => {
  const LABELS = /^\s*(Classes|Groups|Staff|Direct)/i;
  const btns = [...document.querySelectorAll('button[aria-expanded]')]
    .filter(b => LABELS.test((b.innerText || '').trim()));
  const acted = [];
  for (const b of btns) {
    const label = (b.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40);
    const before = b.getAttribute('aria-expanded');
    if (before !== 'true') b.click();
    acted.push({label: label, before: before, after: b.getAttribute('aria-expanded')});
  }
  return acted;
}
"""


async def _open_messaging(page) -> None:
    """From a signed-in SIS session, open MyPowerHub's Messaging pane.

    The pane is a lazily-loaded micro-frontend: after the Messaging button is
    clicked, the group accordions take several seconds to appear (measured ~8s
    on a cold context), and the conversations are not in the DOM at all until a
    group is expanded. Hence poll-with-deadline rather than a single wait.
    """
    await page.goto(hub_url(), timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    try:
        await page.wait_for_selector(SEL_MESSAGING_BTN, timeout=NAV_TIMEOUT_MS)
    except Exception as e:
        raise PowerSchoolError(
            f"MyPowerHub did not show a Messaging button at {hub_url()} — "
            f"SSO may have failed. Page said: {await _page_complaint(page)!r}") from e
    await page.click(SEL_MESSAGING_BTN)

    # Wait for a group accordion to render, then expand it. Re-runs each poll
    # because groups can appear one at a time as the MFE hydrates.
    deadline = time.monotonic() + NAV_TIMEOUT_MS / 1000
    expanded_any = False
    while time.monotonic() < deadline:
        try:
            acted = await page.evaluate(_EXPAND_GROUPS_JS)
        except Exception:
            acted = []
        if acted:
            if not expanded_any:
                print(f"[powerschool] Expanded conversation group(s): "
                      f"{[a['label'] for a in acted]}")
            expanded_any = True
            # Conversations render shortly after the accordion opens.
            try:
                await page.wait_for_selector(SEL_CONV_BUTTON, timeout=15_000)
                return
            except Exception:
                pass
        await asyncio.sleep(2)

    if not expanded_any:
        raise PowerSchoolError(
            "Messaging pane never showed a conversation group (Classes/Groups). "
            f"Page said: {await _page_complaint(page)!r}")
    print("[powerschool] Groups expanded but no conversation rows appeared")


async def fetch_teacher_messages(teacher: str = None) -> list:
    """Return messages from the class conversations, oldest first.

    Each dict: {author, subject, body, posted_at, url}. `teacher` is accepted
    for symmetry but not applied here — callers filter with matches_teacher()
    so that an unfiltered fetch stays available for debugging.
    """
    today = datetime.now(BANGKOK_TZ).date()
    messages = []

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            await _login(page)
            await _open_messaging(page)

            conv_sel = SEL_CONV_BUTTON
            count = await page.locator(conv_sel).count()
            if count == 0:
                conv_sel = SEL_CONV_BUTTON_FALLBACK
                count = await page.locator(conv_sel).count()
                if count:
                    print(f"[powerschool] Primary conversation selector matched "
                          f"nothing; used fallback ({count} row(s)) — the DOM "
                          f"may have changed")
            if count == 0:
                print("[powerschool] No class conversations found in Messaging — "
                      "the Classes group may not have expanded, or the DOM changed")
            for i in range(count):
                btn = page.locator(conv_sel).nth(i)
                try:
                    label = (await btn.inner_text()).strip().splitlines()[0][:80]
                except Exception:
                    label = f"conversation {i}"
                try:
                    await btn.click()
                    await page.wait_for_selector(SEL_MSG_LIST, timeout=NAV_TIMEOUT_MS)
                    await asyncio.sleep(2)      # let the virtualised list settle
                    raw = await page.evaluate(_EXTRACT_JS)
                except Exception as e:
                    print(f"[powerschool] Could not read conversation {label!r}: {e}")
                    continue
                if not raw:
                    continue
                for r in raw:
                    body = (r.get("body") or "").translate(_ZERO_WIDTH).strip()
                    if not body:
                        continue
                    d = _normalise_date_label(r.get("date_label", ""), today)
                    messages.append({
                        "author":    (r.get("author") or "").translate(_ZERO_WIDTH).strip(),
                        "subject":   _subject_from_body(body),
                        "body":      body,
                        "posted_at": _compose_posted_at(d, r.get("time", "")),
                        "url":       hub_url(),
                    })
        finally:
            await browser.close()

    # Rendered oldest-at-top, which already matches the "newest last" contract.
    return messages


def message_id(msg: dict) -> str:
    """Stable id for de-duplication across runs.

    Hashes author+subject+posted_at+body rather than trusting a portal-supplied
    id, because the source page (and whether it exposes one) is not known yet.
    An edited message will read as new — acceptable for a notification relay.
    """
    payload = "\x1f".join([
        (msg.get("author") or "").strip(),
        (msg.get("subject") or "").strip(),
        (msg.get("posted_at") or "").strip(),
        (msg.get("body") or "").strip(),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def matches_teacher(msg: dict, teacher: str) -> bool:
    """Case-insensitive substring match of `teacher` against the author field.

    Matches on author only — matching the body too would relay every message
    that merely mentions the teacher's name.
    """
    if not teacher:
        return True
    return teacher.strip().lower() in (msg.get("author") or "").lower()


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()

    if len(sys.argv) > 1 and sys.argv[1] == "explore":
        out = asyncio.run(explore())
        for label, res in out.items():
            print(f"\n=== {label} ===")
            if res.get("error"):
                print(f"  FAILED: {res['error']}")
                continue
            print(f"  {len(res['nav'])} nav link(s), {len(res['pages'])} page(s)")
            for pg in res["pages"]:
                if pg.get("error"):
                    flag = "ERR "
                elif pg.get("kicked_to_login"):
                    flag = "AUTH"
                else:
                    flag = "    "
                print(f"  {flag} {pg['text'][:34]:34} {pg['url']}")
        print("\nDumps in ./ps_dump/{desktop,mobile}/")
    else:
        print(__doc__)
        print("Usage: python powerschool.py explore")
