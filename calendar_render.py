import calendar, json, os
from datetime import date, datetime
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Load categories from categories.json (same directory as this file).
# Format: [{ "key": "holiday", "label": "วันหยุด", "bg": "#EAF3DE", "text": "#27500A" }, ...]
# Edit that file to add, remove, or recolor categories — no code change needed.
# ---------------------------------------------------------------------------
_CATEGORIES_FILE = os.path.join(os.path.dirname(__file__), "categories.json")

def _load_categories():
    with open(_CATEGORIES_FILE, encoding="utf-8") as f:
        data = json.load(f)
    colors = {c["key"]: {"bg": c["bg"], "text": c["text"]} for c in data}
    labels = {c["key"]: c["label"] for c in data}
    return colors, labels

CAT_COLORS, CAT_LABELS = _load_categories()
# Fallback color used when an event's category key isn't found in CAT_COLORS.
# (Avoids a KeyError crash when categories.json is edited and old events still exist.)
_CAT_FALLBACK = next(iter(CAT_COLORS.values())) if CAT_COLORS else {"bg": "#E0E0E0", "text": "#555555"}

TH_MONTHS = ["","มกราคม","กุมภาพันธ์","มีนาคม","เมษายน","พฤษภาคม",
             "มิถุนายน","กรกฎาคม","สิงหาคม","กันยายน","ตุลาคม","พฤศจิกายน","ธันวาคม"]


def load_events(path="events.json"):
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def build_event_map(events, year, month):
    from datetime import timedelta
    result = {}
    for e in events:
        start = datetime.strptime(e["date"], "%Y-%m-%d").date()
        end = datetime.strptime(e["end_date"], "%Y-%m-%d").date() if e.get("end_date") else start
        d = start
        while d <= end:
            if d.year == year and d.month == month:
                result.setdefault(d.day, []).append(e)
            d += timedelta(days=1)
    return result


def next_month(year, month):
    if month == 12:
        return year + 1, 1
    return year, month + 1


# =============================================
#  Shared CSS
# =============================================
BASE_CSS = """
* { box-sizing:border-box; margin:0; padding:0; font-family:'Sarabun',sans-serif; }
body { background:#fff; }
.cal-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:3px; }
.wd { text-align:center; font-size:14px; color:#888; font-weight:600; padding:5px 0; }
.cell { background:#fafafa; border:0.5px solid #e5e5e5; border-radius:6px;
        padding:5px 6px; height:82px; overflow:hidden; }
.cell.empty { background:transparent; border:none; }
.cell.today { border:2px solid #534AB7; }
.day-num { font-size:14px; font-weight:500; color:#555; margin-bottom:3px; }
.cell.today .day-num { color:#534AB7; font-weight:700; }
.evts { display:flex; flex-direction:column; gap:2px; }
.evt { font-size:10px; padding:2px 4px; border-radius:3px; white-space:nowrap;
       overflow:hidden; text-overflow:ellipsis; font-weight:500; }
.more { font-size:10px; color:#aaa; padding:1px 3px; }
.legend { display:flex; gap:14px; flex-wrap:wrap; }
.leg { display:flex; align-items:center; gap:5px; font-size:12px; color:#555; }
.dot { width:10px; height:10px; border-radius:50%; }
"""

WEEKDAY_CELLS = '<div class="wd">อา</div><div class="wd">จ</div><div class="wd">อ</div><div class="wd">พ</div><div class="wd">พฤ</div><div class="wd">ศ</div><div class="wd">ส</div>'


def _legend_html():
    return "".join([
        f'<div class="leg"><div class="dot" style="background:{CAT_COLORS[k]["bg"]};'
        f'border:1.5px solid {CAT_COLORS[k]["text"]}"></div>{label}</div>'
        for k, label in CAT_LABELS.items()
    ])


def _month_cells(events, year, month):
    """Generate grid cells for a single month (weekday headers + day cells in one grid)."""
    event_map = build_event_map(events, year, month)
    cal = calendar.Calendar(firstweekday=6).monthdayscalendar(year, month)
    today = date.today()

    # Weekday headers are part of the same grid
    cells = WEEKDAY_CELLS

    for week in cal:
        for day in week:
            if day == 0:
                cells += '<div class="cell empty"></div>'
                continue
            is_today = (date(year, month, day) == today)
            day_evts = event_map.get(day, [])
            evts_html = ""
            for ev in day_evts[:2]:
                c = CAT_COLORS.get(ev["cat"], _CAT_FALLBACK)
                evts_html += f'<div class="evt" style="background:{c["bg"]};color:{c["text"]}">{ev["name"]}</div>'
            if len(day_evts) > 2:
                evts_html += f'<div class="more">+{len(day_evts)-2}</div>'
            today_cls = " today" if is_today else ""
            cells += f'<div class="cell{today_cls}"><div class="day-num">{day}</div><div class="evts">{evts_html}</div></div>'
    return cells


# =============================================
#  Single month (used by post_calendar / auto)
# =============================================
def render_html(year, month, events):
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Sarabun:wght@400;500;600&display=swap" rel="stylesheet">
<style>{BASE_CSS}
body {{ padding:20px; width:760px; }}
h2 {{ font-size:24px; font-weight:600; color:#1a1a1a; margin-bottom:14px; }}
.legend {{ margin-top:14px; }}
</style></head><body>
<h2>ปฏิทินกิจกรรม — {TH_MONTHS[month]} {year + 543}</h2>
<div class="cal-grid">{_month_cells(events, year, month)}</div>
<div class="legend">{_legend_html()}</div>
</body></html>"""


async def generate_calendar_image(year, month, events, output="calendar.png"):
    html = render_html(year, month, events)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 800, "height": 750})
        await page.set_content(html, wait_until="networkidle")
        await page.screenshot(path=output, clip={"x": 0, "y": 0, "width": 760, "height": 660})
        await browser.close()
    return output


# =============================================
#  Two-month view (used by /calendar command)
# =============================================
def render_two_month_html(year, month, events):
    y2, m2 = next_month(year, month)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Sarabun:wght@400;500;600&display=swap" rel="stylesheet">
<style>{BASE_CSS}
body {{ padding:20px; width:900px; }}
.two-col {{ display:flex; gap:20px; }}
.month-block {{ flex:1; }}
.month-title {{ font-size:18px; font-weight:600; color:#1a1a1a; margin-bottom:10px; text-align:center; }}
.cell {{ height:76px; }}
.wd {{ font-size:13px; }}
.evt {{ font-size:10px; }}
.legend {{ margin-top:14px; justify-content:center; }}
</style></head><body>
<div class="two-col">
  <div class="month-block">
    <div class="month-title">{TH_MONTHS[month]} {year + 543}</div>
    <div class="cal-grid">{_month_cells(events, year, month)}</div>
  </div>
  <div class="month-block">
    <div class="month-title">{TH_MONTHS[m2]} {y2 + 543}</div>
    <div class="cal-grid">{_month_cells(events, y2, m2)}</div>
  </div>
</div>
<div class="legend">{_legend_html()}</div>
</body></html>"""


async def generate_two_month_image(year, month, events, output="calendar_2m.png"):
    html = render_two_month_html(year, month, events)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 940, "height": 720})
        await page.set_content(html, wait_until="networkidle")
        await page.screenshot(path=output, clip={"x": 0, "y": 0, "width": 900, "height": 660})
        await browser.close()
    return output
