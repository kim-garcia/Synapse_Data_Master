"""
audit_automation.py
--------------------
Semi-automated audit helper.

WHAT IT DOES
  - Reads your audit CSV.
  - For each row, opens each browser system, looks up values, and fills the
    LOOKUP columns automatically.
  - NEVER touches the JUDGMENT columns -- those are left blank for you to fill
    with your own comments.
  - Saves after every row, so if it crashes you just run it again and it
    resumes where it left off (rows already filled are skipped).

WHAT YOU MUST DO
  1. Install:  pip install playwright   then:  playwright install chromium
  2. Fill in the CONFIG section below (columns + the key you look up by).
  3. Fill in each function in the SYSTEMS section with the real navigation
     and selectors for your systems (the only part I can't see for you).
  4. Run it once -- a browser window opens, you log into all systems by hand,
     then press Enter in the terminal. Your login is saved in ./browser_profile
     so future runs stay logged in.

RUN:  python audit_automation.py
"""

import csv
import sys
import time
from pathlib import Path

# ======================================================================
# CONFIG  -- edit this section to match your CSV
# ======================================================================

CSV_PATH = "audit.csv"            # your file
KEY_COLUMN = "account_id"          # the column you use to look things up
                                   # (whatever identifies each row in the systems)

# Columns the script fills automatically. Leave the list empty for any
# system you're not automating yet.
LOOKUP_COLUMNS = ["status_a", "plan_b", "balance_c"]

# Columns you fill yourself with judgment/comments. The script will NEVER
# write to these -- they're just listed so it knows to leave them alone.
JUDGMENT_COLUMNS = ["risk_assessment", "auditor_comment"]

PROFILE_DIR = "browser_profile"   # keeps you logged in between runs
HEADLESS = False                  # keep False so you can watch / log in

# ======================================================================
# SYSTEMS  -- one function per system. Fill in the real steps + selectors.
# Each function receives the open `page` and the row's key value, and
# returns a dict of {column_name: value} for whatever it found.
# Return {} (or skip a key) to leave a column blank.
# ======================================================================

def lookup_system_a(page, key):
    """EXAMPLE PATTERN -- replace with your real system A steps."""
    # page.goto("https://system-a.example.com/search")
    # page.fill("#searchBox", key)              # <-- find these selectors
    # page.click("#searchButton")               #     via right-click >
    # page.wait_for_selector("#resultStatus")   #     Inspect in Chrome
    # status = page.inner_text("#resultStatus").strip()
    # return {"status_a": status}
    return {}


def lookup_system_b(page, key):
    """Replace with your real system B steps."""
    # page.goto("https://system-b.example.com")
    # ...
    # plan = page.inner_text(".plan-name").strip()
    # return {"plan_b": plan}
    return {}


def lookup_system_c(page, key):
    """Replace with your real system C steps."""
    return {}


# Register every system here, in the order you want them run per row.
SYSTEMS = [
    lookup_system_a,
    lookup_system_b,
    lookup_system_c,
]

# ======================================================================
# ENGINE  -- you normally don't need to touch anything below here.
# ======================================================================

def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader), reader.fieldnames


def write_rows(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def row_is_done(row):
    """A row is 'done' when every lookup column already has a value."""
    if not LOOKUP_COLUMNS:
        return False
    return all((row.get(c) or "").strip() for c in LOOKUP_COLUMNS)


def fill_row(page, row):
    """Run every system for one row and merge results into the row.
    Judgment columns are never written."""
    key = (row.get(KEY_COLUMN) or "").strip()
    if not key:
        print(f"  ! row missing {KEY_COLUMN}, skipping")
        return row
    for system in SYSTEMS:
        try:
            found = system(page, key) or {}
        except Exception as e:
            print(f"  ! {system.__name__} failed for {key}: {e}")
            found = {}
        for col, val in found.items():
            if col in JUDGMENT_COLUMNS:
                continue  # safety: never overwrite a judgment column
            if col in row:
                row[col] = val
    return row


def main():
    path = Path(CSV_PATH)
    if not path.exists():
        sys.exit(f"CSV not found: {CSV_PATH}")

    rows, fieldnames = read_rows(path)

    # make sure all expected columns exist in the header
    for col in LOOKUP_COLUMNS + JUDGMENT_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
            for r in rows:
                r.setdefault(col, "")

    from playwright.sync_api import sync_playwright  # imported here so the
    # CSV logic stays testable without Playwright installed

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=HEADLESS
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        input(
            "\nA browser window is open. Log into ALL your systems now, "
            "then press Enter here to start the audit...\n"
        )

        total = len(rows)
        for i, row in enumerate(rows, 1):
            key = (row.get(KEY_COLUMN) or "").strip()
            if row_is_done(row):
                print(f"[{i}/{total}] {key} already done, skipping")
                continue
            print(f"[{i}/{total}] processing {key} ...")
            fill_row(page, row)
            write_rows(path, rows, fieldnames)  # save after every row
            time.sleep(0.5)  # be gentle on the systems

        ctx.close()

    print("\nDone. Lookup columns filled; judgment columns left for you.")


if __name__ == "__main__":
    main()
