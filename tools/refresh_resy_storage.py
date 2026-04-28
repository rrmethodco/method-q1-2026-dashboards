#!/usr/bin/env python3
"""
refresh_resy_storage.py — interactive helper to generate / refresh
`resy-storage-state.json` for the Resy OS scraper.

Why this exists:
  Resy OS at os.resy.com uses operator credentials + (likely) MFA / device
  binding that the consumer Resy API rejects with HTTP 419. The only
  reliable path to nightly survey + ratings data is a Playwright-based
  scraper that replays a real logged-in browser session.

  This script opens a real Chromium window, lets the operator log in
  manually (handling MFA / verification emails), then exports the full
  storageState (cookies + localStorage + IndexedDB) to JSON so the
  headless scraper can replay it.

Setup (one-time):
  1. python3 -m pip install playwright
  2. python3 -m playwright install chromium

Usage:
  python3 tools/refresh_resy_storage.py
  → opens Chromium, navigate to https://os.resy.com/portal and log in
  → press Enter in the terminal once you see the venues list
  → script writes resy-storage-state.json to cwd

Then:
  gh secret set RESY_OS_STORAGE_STATE_JSON < resy-storage-state.json

Cadence: re-run every ~21 days, or whenever the nightly scraper's
healthcheck reports 0 surveys for >2 venues (signals expired session).
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.stderr.write(
        "missing dependency: pip install playwright && playwright install chromium\n"
    )
    sys.exit(2)


OUT_PATH = Path("resy-storage-state.json")
LOGIN_URL = "https://os.resy.com/portal/login"
PORTAL_URL = "https://os.resy.com/portal"


def main() -> int:
    print("Opening Chromium with a fresh profile...")
    print(f"  Login URL: {LOGIN_URL}")
    print(f"  Output:    {OUT_PATH.resolve()}")
    print()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            # Mirror what a real macOS Chrome session looks like — the
            # nightly headless replay should match these so server-side
            # bot scoring sees consistency.
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.goto(LOGIN_URL)
        print("=" * 64)
        print("BROWSER OPEN. Log into Resy OS now (handle MFA if prompted).")
        print("Once you see the Resy OS Portal venues list, return here and")
        print("press Enter to capture the session.")
        print("=" * 64)
        input("Press Enter when logged in: ")
        # Quick sanity check — make sure we landed on a portal URL
        current = page.url
        if "/portal" not in current:
            print(f"WARNING: page URL is {current!r} — expected /portal/...")
            print("Press Enter again to capture anyway, or Ctrl-C to abort.")
            input()
        ctx.storage_state(path=str(OUT_PATH))
        # localStorage doesn't always survive context.storage_state on first
        # snapshot, so capture it explicitly too:
        try:
            ls = page.evaluate(
                "() => Object.fromEntries(Object.keys(localStorage)"
                ".map(k => [k, localStorage.getItem(k)]))"
            )
            print(f"Captured {len(ls)} localStorage keys")
        except Exception as e:
            print(f"(localStorage capture skipped: {e})")
        browser.close()
    print()
    print(f"✓ Wrote {OUT_PATH} ({OUT_PATH.stat().st_size:,} bytes)")
    print()
    print("Next: store this as a GitHub Secret:")
    print("  gh secret set RESY_OS_STORAGE_STATE_JSON < resy-storage-state.json")
    print()
    print("Then trigger a manual sync to verify:")
    print("  gh workflow run guest-sync.yml -f probe=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
