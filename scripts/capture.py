"""Screenshots and an animated GIF of the running dashboards for the README (headless browser via Playwright).

Uses an installed Edge or Chrome (`--channel msedge|chrome`) or Playwright's own Chromium (`--channel chromium`
after `playwright install chromium`). Dashboards are opened anonymously in kiosk mode.

Usage:
    pip install -e ".[capture]"
    python scripts/capture.py shots --out docs/img
    python scripts/capture.py gif --uid fleet-overview --frames 40 --every 3 --out docs/img/replay.gif
"""
from __future__ import annotations

import argparse
import io
import logging
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

log = logging.getLogger("capture")

DASHBOARDS = {"fleet-overview": "", "wind-turbine": "&var-turbine=KWF4", "pv-plant": "", "alarms": ""}


def _page(p, args):
    browser = p.chromium.launch(channel=None if args.channel == "chromium" else args.channel)
    page = browser.new_page(viewport={"width": args.width, "height": args.height}, device_scale_factor=1)
    page.inflight = set()   # panel queries in progress
    page.on("request", lambda r: "/api/ds/query" in r.url and page.inflight.add(r))
    page.on("requestfinished", lambda r: page.inflight.discard(r))
    page.on("requestfailed", lambda r: page.inflight.discard(r))
    return browser, page


def _open(page, args, uid: str, extra: str = "") -> None:
    page.goto(f"{args.url}/d/{uid}?kiosk&from={args.time_from}&to={args.time_to}{extra}", wait_until="networkidle")
    # ready when no panel plugin is loading and no query has been in flight for 2 s
    deadline, quiet = time.monotonic() + 120, 0
    while time.monotonic() < deadline and quiet < 4:
        idle = not page.inflight and not page.get_by_text("Loading plugin panel").count()
        quiet = quiet + 1 if idle else 0
        page.wait_for_timeout(500)
    page.wait_for_timeout(args.settle * 1000)


def shots(args) -> None:
    args.out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser, page = _page(p, args)
        for uid, extra in DASHBOARDS.items():
            if args.only and uid not in args.only:
                continue
            _open(page, args, uid, extra)
            path = args.out / f"{uid}.png"
            page.screenshot(path=str(path), full_page=True)
            log.info("saved %s", path)
        browser.close()


def gif(args) -> None:
    from PIL import Image
    frames = []
    with sync_playwright() as p:
        browser, page = _page(p, args)
        _open(page, args, args.uid, DASHBOARDS.get(args.uid, ""))
        for i in range(args.frames):
            png = page.screenshot(full_page=False)
            frames.append(Image.open(io.BytesIO(png)).convert("RGB"))
            log.info("frame %d/%d", i + 1, args.frames)
            time.sleep(args.every)
        browser.close()
    w = args.gif_width
    small = [f.resize((w, round(f.height * w / f.width)), Image.LANCZOS).quantize(colors=128, dither=Image.Dither.NONE)
             for f in frames]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    small[0].save(args.out, save_all=True, append_images=small[1:], duration=args.frame_ms, loop=0, optimize=True)
    log.info("saved %s (%d frames, %.1f MB)", args.out, len(small), args.out.stat().st_size / 1e6)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["shots", "gif"])
    ap.add_argument("--url", default="http://127.0.0.1:3000")
    ap.add_argument("--channel", default="msedge", help="msedge, chrome or chromium")
    ap.add_argument("--time-from", default="now-1h")
    ap.add_argument("--time-to", default="now")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1180)
    ap.add_argument("--settle", type=float, default=3, help="extra seconds to let panels render")
    ap.add_argument("--out", type=Path, default=Path("docs/img"))
    ap.add_argument("--only", nargs="*", help="dashboard uids for shots")
    ap.add_argument("--uid", default="fleet-overview", help="dashboard for the gif")
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--every", type=float, default=3.0, help="seconds between gif frames")
    ap.add_argument("--frame-ms", type=int, default=250)
    ap.add_argument("--gif-width", type=int, default=1100)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    shots(args) if args.mode == "shots" else gif(args)


if __name__ == "__main__":
    main()
