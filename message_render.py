"""Render a PowerSchool teacher message as a readable PNG image.

Discord's embed description is capped at 4096 characters, and the client
picks a small, fixed font size for it — neither is something a bot can
override through the Embed API. Running the same text through the same
Playwright HTML->screenshot pipeline calendar_render.py uses sidesteps both:
font size is ours to set, and an image has no character cap, so one render
holds a message of any length.
"""
import html as _html
from playwright.async_api import async_playwright

_CSS = """
* { box-sizing:border-box; margin:0; padding:0; font-family:'Sarabun',sans-serif; }
body { background:#fff; padding:32px 36px; width:680px; }
.subject { font-size:24px; font-weight:600; color:#1a1a1a; margin-bottom:8px; }
.meta { font-size:15px; color:#888; margin-bottom:22px; }
.body { font-size:21px; line-height:1.7; color:#1f1f1f; white-space:pre-wrap;
        word-wrap:break-word; }
"""


def _render_html(subject: str, body: str, author: str, posted_at: str) -> str:
    meta = " · ".join(_html.escape(p) for p in (author, posted_at) if p)
    meta_html = f'<div class="meta">{meta}</div>' if meta else ""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Sarabun:wght@400;500;600&display=swap" rel="stylesheet">
<style>{_CSS}</style></head><body>
<div class="subject">{_html.escape(subject)}</div>
{meta_html}
<div class="body">{_html.escape(body)}</div>
</body></html>"""


async def render_message_image(subject: str, body: str, author: str = "", posted_at: str = "") -> bytes:
    """PNG bytes of the message at readable size. full_page=True rather than a
    fixed clip (unlike calendar_render.py) because height varies with length."""
    html_str = _render_html(subject, body, author, posted_at)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 680, "height": 100})
        await page.set_content(html_str, wait_until="networkidle")
        png = await page.screenshot(full_page=True)
        await browser.close()
    return png
