"""
vin_batch.py — run the VinSolutions audit on the first N pending dealers.

This REUSES your existing audit_automation.py (it does not change it). The only
differences vs. running the original script:

  * you can cap how many dealers to process:   python vin_batch.py --limit 100
  * the login has no terminal question — it just waits until it SEES that you're
    logged in (so it also works when launched from the website), and
  * it writes live progress to run_status.json so the website can show a bar.

Because your browser session is saved in ../browser_profile, after the first
login it usually starts running immediately.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# The scrapers live in ../scrapers, but their data files (audit.csv, the mapping
# file, browser_profile/) live at the repo root. So we run FROM the root (chdir)
# and import the scraper FROM the scrapers/ folder (sys.path).
AUDIT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(AUDIT_ROOT)
sys.path.insert(0, str(AUDIT_ROOT / "scrapers"))

import audit_automation as va  # your script, imported as a library

STATUS_FILE = Path(__file__).resolve().parent / "run_status.json"
LOGIN_SELECTOR = "#ccrm-header-display-button"  # visible only once logged in


def _write_status(**fields) -> None:
    """Merge fields into run_status.json (the website tails this file)."""
    data = {}
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update(fields)
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    STATUS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _wait_for_login(page, timeout_s: int = 300) -> bool:
    """Poll until the VinSolutions home element appears. No terminal input."""
    page.goto(va.VIN_URL, wait_until="domcontentloaded")
    _write_status(state="waiting_login",
                  message="Log in in the browser window…")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            page.wait_for_selector(LOGIN_SELECTOR, state="visible", timeout=3000)
            va.log.info("login confirmed")
            return True
        except Exception:
            _write_status(state="waiting_login",
                          message="Waiting for login… (finish signing in)")
    return False


def run(limit: int) -> None:
    va.setup_logging()
    va.log.info("=== vin_batch started (limit=%d) ===", limit)
    _write_status(state="starting", processed=0, target=limit,
                  current_key="", error=None,
                  started_at=datetime.now().isoformat(timespec="seconds"),
                  message="Preparing…")

    path = Path(va.CSV_PATH)
    if not path.exists():
        _write_status(state="error", error=f"CSV not found: {va.CSV_PATH}")
        sys.exit(1)

    rows, fieldnames, delim = va.read_rows(path)
    # Make sure the output columns exist (same as the original main()).
    for col in va.LOOKUP_COLUMNS + va.JUDGMENT_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
            for r in rows:
                r.setdefault(col, "")
    va.load_mapping()

    from playwright.sync_api import sync_playwright

    processed = 0
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                va.PROFILE_DIR,
                headless=va.HEADLESS,
                viewport=va.WINDOW,
                args=[f"--window-size={va.WINDOW['width']},{va.WINDOW['height']}"],
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            if not _wait_for_login(page):
                _write_status(state="error",
                              error="Login not detected in time.")
                ctx.close()
                return

            _write_status(state="running", message="Processing dealers…")
            for row in rows:
                if processed >= limit:
                    break
                key = va.get_key(row)
                if not key or va.row_is_done(row):
                    continue  # skip blanks and already-audited rows
                processed += 1
                _write_status(state="running", processed=processed,
                              target=limit, current_key=key,
                              message=f"Checking {processed}/{limit}: {key}")
                va.fill_row(page, row)
                va.write_rows(path, rows, fieldnames, delim)  # save after each
                time.sleep(0.5)

            ctx.close()
    except Exception as e:  # never leave the status stuck on "running"
        va.log.exception("vin_batch failed: %s", e)
        _write_status(state="error", error=str(e))
        return

    _write_status(state="done", processed=processed, target=limit,
                  current_key="", message=f"Done: {processed} dealers verified.")
    va.log.info("=== vin_batch finished (%d processed) ===", processed)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the VinSolutions audit on N pending dealers.")
    ap.add_argument("--limit", type=int, default=100, help="max dealers to process")
    run(ap.parse_args().limit)
