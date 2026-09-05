"""Capture a public Canva view link as page JPEGs + a single PDF.

Notes measured against the live viewer (2026-09-05):
  * Class names are hashed and change on every Canva deploy — anchor on the
    controls' aria-labels, and find the page itself by geometry.
  * "Hide controls" in the More menu removes the nav buttons from the DOM, so
    paging then fails. Instead use a viewport much WIDER than the page: the
    chrome sits outside the page's box and the clip excludes it anyway.
  * A public view link offers no Download item, so screenshots are the only
    route; PNG is the wrong codec for a photo newsletter (73 MB for 3 pages).
"""
import argparse, asyncio, re, hashlib
from pathlib import Path
from playwright.async_api import async_playwright

# Measured 2026-09-05: Canva serves photo resolution proportional to DEVICE
# pixels (CSS size x DPR), so rendering bigger fetches genuinely larger sources
# rather than upscaling. Sampling source-pixels-per-captured-pixel across the
# page's photos, the median sits at 1.07 / 1.00 / 0.97 for captures of
# 3394x4800 / 4526x6400 / 5656x8000. Past ~4500x6400 the median drops under 1
# and we are just interpolating, so that is the useful ceiling.
# The old 2200x1600 @2x (2262x3200) sat around 0.85 — visibly softer than the
# Canva viewer, which is the quality gap this fixes.
# Presets trade render size against OUTPUT SIZE, not memory. Measured peak RSS
# of the Playwright chromium tree on a 3-page A4 design: max ~911 MB,
# high ~1036 MB — i.e. indistinguishable, and not ordered the way you would
# expect. Chromium itself plus the decoded source images dominate; the
# screenshot buffer is noise. Budget ~1 GB for a capture whatever the preset.
# The Synology has ~1.7 GB free, so a capture must never overlap the
# PowerSchool relay's own browser.
PRESETS = {
    "max":  {"vw": 4400, "vh": 3200, "scale": 2},   # ~4524x6400  median 1.00
    "high": {"vw": 3300, "vh": 2400, "scale": 2},   # ~3394x4800  median 1.07
    "lite": {"vw": 2200, "vh": 1600, "scale": 2},   # ~2262x3200  median 0.85 (soft)
}
DEFAULT_PRESET = "max"
JPEG_Q    = 95
SETTLE_MS = 11000                             # bigger render, slower first paint
PAGE_MS   = 2200


async def _wait_for_images(page, timeout_ms=30000, stable_polls=3, interval_ms=500):
    """Wait until the page's photos have finished upgrading.

    Canva paints a tiny blurred placeholder first and swaps in the full-size
    image a moment later, so a fixed delay can capture the placeholder — that
    is exactly how one photo on a page came out blurred. Poll until every
    visible image reports complete and the total decoded pixel count has
    stopped growing. Returns the final stats so the caller can warn when an
    image is still obviously under-resolved."""
    js = """() => {
      const vis = [...document.images].filter(i => {
        const r = i.getBoundingClientRect();
        return r.width > 40 && r.height > 40;
      });
      let pending = 0, sig = 0, worst = 99;
      for (const i of vis) {
        if (!i.complete || i.naturalWidth === 0) pending++;
        sig += i.naturalWidth * i.naturalHeight;
        const need = i.getBoundingClientRect().width * devicePixelRatio;
        if (need > 0) worst = Math.min(worst, i.naturalWidth / need);
      }
      return {n: vis.length, pending: pending, sig: sig, worst: +worst.toFixed(2)};
    }"""
    waited, last_sig, stable, stats = 0, None, 0, {}
    while waited < timeout_ms:
        stats = await page.evaluate(js)
        if stats["pending"] == 0 and stats["sig"] == last_sig:
            stable += 1
            if stable >= stable_polls:
                return stats
        else:
            stable = 0
        last_sig = stats["sig"]
        await page.wait_for_timeout(interval_ms)
        waited += interval_ms
    print(f"  (images still settling after {timeout_ms}ms: {stats})")
    return stats


async def _page_box(page):
    """The design page, found by geometry: biggest box clearly narrower than
    the viewport (which excludes the full-bleed app root)."""
    return await page.evaluate("""() => {
      let best = null;
      for (const e of document.querySelectorAll('div,section,figure')) {
        const r = e.getBoundingClientRect();
        if (r.width < 200 || r.height < 200) continue;
        // Skip only boxes that fill BOTH axes (the app root). Guarding on
        // width alone breaks landscape designs: a 16:9 deck fits by width, so
        // the page itself would be skipped and the clip would fall back to the
        // whole viewport with the viewer chrome baked in.
        if (r.width > innerWidth * 0.95 && r.height > innerHeight * 0.95) continue;
        if (r.width > innerWidth + 2 || r.height > innerHeight + 2) continue;
        const a = r.width * r.height;
        if (!best || a > best.a) best = {a, x:r.x, y:r.y, w:r.width, h:r.height};
      }
      return best;
    }""")


async def capture(url: str, outdir: Path, lossless: bool = False, quality: int = JPEG_Q,
                  preset: str = DEFAULT_PRESET):
    cfg = PRESETS[preset]
    viewport = {"width": cfg["vw"], "height": cfg["vh"]}
    scale = cfg["scale"]
    outdir.mkdir(parents=True, exist_ok=True)
    shots, hashes = [], []
    async with async_playwright() as p:
        # --disable-dev-shm-usage: Docker gives /dev/shm only 64 MB by default and
        # a 4500x6400 raster will exhaust it, crashing the tab with no useful
        # error. --no-sandbox is required to run as root inside the container.
        b = await p.chromium.launch(args=[
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ])
        ctx = await b.new_context(viewport=viewport, device_scale_factor=scale)
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(SETTLE_MS)

        title = (await page.title()).strip() or "canva"
        total = 1
        try:
            t = await page.inner_text('[aria-label="Go to page"]')
            m = re.search(r"(\d+)\s*/\s*(\d+)", t.replace("\n", " "))
            if m:
                total = int(m.group(2))
        except Exception as e:
            print(f"  (no page counter, assuming 1 page: {e})")

        for i in range(1, total + 1):
            stats = await _wait_for_images(page)
            if stats.get("worst", 9) < 0.35:
                # A placeholder that never upgraded, or a genuinely small asset.
                print(f"  (page {i}: lowest-res image is {stats['worst']}x of its "
                      f"displayed size — may look soft)")
            box = await _page_box(page)
            if not box:
                # Never fail silently: a full-viewport shot bakes in the viewer
                # chrome and looks plausible enough to ship unnoticed.
                print(f"  !! page {i}: no page box found — capturing full viewport "
                      f"WITH viewer chrome; the geometry heuristic needs revisiting")
            clip = ({"x": box["x"], "y": box["y"], "width": box["w"], "height": box["h"]}
                    if box else None)
            f = outdir / (f"page{i}.png" if lossless else f"page{i}.jpg")
            if lossless:
                await page.screenshot(path=str(f), clip=clip, type="png")
            else:
                await page.screenshot(path=str(f), clip=clip, type="jpeg", quality=quality)
            h = hashlib.md5(f.read_bytes()).hexdigest()[:8]
            dup = " DUPLICATE" if h in hashes else ""
            hashes.append(h)
            shots.append(f)
            px = f"{int(box['w'])*scale}x{int(box['h'])*scale}" if box else "?"
            print(f"  page {i}/{total} -> {f.name}  {px} px  "
                  f"{f.stat().st_size/1024/1024:.1f} MB  {h}{dup}")
            if i < total:
                await page.click('[aria-label="Next page"]')
                await page.wait_for_timeout(PAGE_MS)   # let the slide transition start

        await b.close()

    if len(set(hashes)) != len(hashes):
        print("  !! some pages identical — paging did not advance")
    return title, shots


# A4 long edge in PostScript points. Pages are given a real paper size rather
# than one point per pixel — a 4524x6400 capture would otherwise declare a
# 47x67 inch page, which prints at the wrong scale. The image is embedded at
# full resolution either way, so this costs no detail (~547 dpi on A4).
A4_LONG_PT = 842.0


def to_pdf(shots, pdf_path: Path):
    import fitz                       # PyMuPDF, already a project dependency
    doc = fitz.open()
    for s in shots:
        img = fitz.open(str(s))
        w, h = img[0].rect.width, img[0].rect.height
        if h >= w:
            ph = A4_LONG_PT; pw = A4_LONG_PT * w / h
        else:
            pw = A4_LONG_PT; ph = A4_LONG_PT * h / w
        pg = doc.new_page(width=pw, height=ph)
        pg.insert_image(fitz.Rect(0, 0, pw, ph), filename=str(s))
        img.close()
    doc.save(str(pdf_path), deflate=True)
    doc.close()


async def main():
    ap = argparse.ArgumentParser(description="Capture a public Canva view link.")
    ap.add_argument("url")
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--png", action="store_true",
                    help="lossless PNG pages (much larger; JPEG q95 is visually equivalent)")
    ap.add_argument("--quality", type=int, default=JPEG_Q, help="JPEG quality (default 95)")
    ap.add_argument("--pdf-name", default="design.pdf")
    ap.add_argument("--preset", choices=sorted(PRESETS), default=DEFAULT_PRESET,
                    help="render size / memory tradeoff (default max)")
    a = ap.parse_args()

    title, shots = await capture(a.url, a.outdir, lossless=a.png, quality=a.quality,
                                 preset=a.preset)
    print("  title:", title)
    if shots:
        pdf = a.outdir / a.pdf_name
        to_pdf(shots, pdf)
        print(f"  pdf -> {pdf.name} ({pdf.stat().st_size/1024/1024:.1f} MB)")

if __name__ == "__main__":
    # Guarded so `to_pdf`/`capture` can be imported (e.g. from bot.py) without
    # the CLI parsing argv and exiting — the same trap bot.py still has.
    asyncio.run(main())
