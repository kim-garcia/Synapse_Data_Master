"""
audit_automation.py  --  Phase 1: VinSolutions only

One browser, one login, all rows. Logs in once (session is saved in
./browser_profile), then for each row: goes home, opens the dealer search,
types the MAPPING_PFA_ID like a human, selects the dealer, reads status +
name, then Admin > Selected Dealer > CRM Admin Settings and pulls 7 values.
Saves after every row; resumes if it crashes.

SETUP (once):  pip install playwright   then:  playwright install chromium
RUN:           python audit_automation.py
"""

import csv
import sys
import time
import random
from pathlib import Path

# ======================================================================
# CONFIG
# ======================================================================
CSV_PATH = "audit.csv"
KEY_COLUMN = "MAPPING_PFA_ID"

LOOKUP_COLUMNS = [
    "VIN Account Status",
    "Vin Name",
    "ILM Status",
    "Full Crm Status",
    "Max Number of Users",
    "Desking",
    "AIS Status",
]
JUDGMENT_COLUMNS = []

PROFILE_DIR = "browser_profile"
HEADLESS = False
WINDOW = {"width": 1024, "height": 768}
VIN_URL = "https://vinsolutions.app.coxautoinc.com/vinconnect"

# ======================================================================
# VINSOLUTIONS
# ======================================================================
def lookup_vinsolutions(page, key):
    result = {}

    # Start each row from the home page -- login persists, no re-login.
    page.goto(VIN_URL, wait_until="domcontentloaded")

    # AUTH CHECK
    try:
        page.wait_for_selector("#ccrm-header-display-button",
                               state="visible", timeout=15000)
    except Exception:
        raise SystemExit("Not authenticated in VinSolutions. Log in, re-run.")

    # Open the dealer search modal
    page.click("#ccrm-header-display-button")

    # WAIT for the modal to fully load before typing anything
    try:
        page.wait_for_selector("#ccrm-dealer-selector-modal-custom",
                               state="visible", timeout=15000)
    except Exception:
        pass
    page.wait_for_selector("#dealer-selector-dealer-selector-input",
                           state="visible", timeout=15000)
    page.wait_for_timeout(1200)  # settle pause before typing

    # Search; if nothing matches, flip "Active Dealers Only" off and retry
    if not _select_dealer(page, key):
        try:
            page.wait_for_selector("#dealer-selector-active-dealer-toggle-label",
                                   state="visible", timeout=5000)
            page.click("#dealer-selector-active-dealer-toggle-label")  # the label, not hidden checkbox
            page.wait_for_timeout(1500)
        except Exception:
            pass
        if not _select_dealer(page, key):
            print(f"  ! dealer {key} not found")
            return {}

    # Wait for the dealer header to load
    try:
        page.wait_for_selector("span.ccrm-dealer-header-display-title-name",
                               state="visible", timeout=10000)
    except Exception:
        page.wait_for_timeout(2000)

    # Status badge (no badge = Active) + dealer name
    badge = page.query_selector("span[id^='dealer-status-badge']")
    badge_text = badge.inner_text().strip() if badge else ""
    result["VIN Account Status"] = badge_text if badge_text else "Active"
    name = page.query_selector("span.ccrm-dealer-header-display-title-name")
    if name:
        result["Vin Name"] = name.inner_text().strip()

    # Admin > Selected Dealer > CRM Admin Settings (wait before each step)
    page.wait_for_selector("#tab-admin", state="visible", timeout=15000)
    page.click("#tab-admin")
    page.wait_for_timeout(800)  # let the dropdown open
    page.wait_for_selector(
        "#navigation-sub-menus-navigation-sub-menu-tab-admin-selected-dealer",
        state="visible", timeout=10000)
    page.hover(
        "#navigation-sub-menus-navigation-sub-menu-tab-admin-selected-dealer")
    page.wait_for_selector(
        "#navigation-sub-menu-tab-admin-selected-dealer-crm-admin-settings",
        state="visible", timeout=10000)
    page.click(
        "#navigation-sub-menu-tab-admin-selected-dealer-crm-admin-settings")

    # The settings form is legacy ASP.NET and may load inside an iframe
    ctx = _ctx_with(page, "#MainContent__ILMEnabled")
    if ctx is None:
        print("  ! CRM Admin Settings form not found")
        return result

    result["ILM Status"] = _checked(ctx, "#MainContent__ILMEnabled")
    result["Full Crm Status"] = _checked(ctx, "#MainContent__CRMEnabled")
    result["AIS Status"] = _checked(ctx, "#MainContent_m_AISEnabled")
    result["Max Number of Users"] = (
        _value(ctx, "#ctl00_MainContent_m_txt_MaxNumberOfUsers")
        or _value(ctx, "#ctl00_MainContent_m_txt_MaxNumberOfUsers_ClientState")
    )
    result["Desking"] = _selected_text(ctx, "#MainContent_m_DeskingAccess")
    return result


def _human_type(element, text):
    """Type one character at a time with a random human-like delay."""
    for ch in str(text):
        element.type(ch, delay=random.randint(70, 170))


def _select_dealer(page, key):
    """Type key, find the row whose Dealer ID matches, click its select button."""
    box = page.wait_for_selector("#dealer-selector-dealer-selector-input",
                                 state="visible", timeout=15000)
    box.click()
    box.fill("")            # clear any previous text
    _human_type(box, key)   # natural typing
    page.wait_for_timeout(2000)  # let the grid filter

    try:
        page.wait_for_selector(
            "#dealer-selector-table tr.ant-table-row-level-0",
            state="visible", timeout=6000)
    except Exception:
        return False

    rows = page.query_selector_all(
        "#dealer-selector-table tr.ant-table-row-level-0")
    for row in rows:
        tds = row.query_selector_all("td")
        if tds and tds[0].inner_text().strip() == str(key).strip():
            btn = row.query_selector("button")
            if btn:
                btn.click()
                return True
    return False


# ---------- small helpers ----------
def _ctx_with(page, selector, total_wait=15000):
    """Return the page or the frame containing `selector`, else None."""
    waited, step = 0, 500
    while waited <= total_wait:
        if page.query_selector(selector):
            return page
        for fr in page.frames:
            try:
                if fr.query_selector(selector):
                    return fr
            except Exception:
                pass
        page.wait_for_timeout(step)
        waited += step
    return None


def _checked(ctx, selector):
    el = ctx.query_selector(selector)
    if el is None:
        return ""
    return "Yes" if el.is_checked() else "No"


def _value(ctx, selector):
    el = ctx.query_selector(selector)
    if el is None:
        return ""
    try:
        v = el.input_value()
    except Exception:
        v = el.get_attribute("value") or ""
    return (v or "").strip()


def _selected_text(ctx, selector):
    el = ctx.query_selector(selector)
    if el is None:
        return ""
    val = el.input_value()
    opt = ctx.query_selector(f"{selector} option[value='{val}']")
    return opt.inner_text().strip() if opt else ""


def wait_for_login(page):
    """Open VinSolutions and wait until the user confirms they're logged in."""
    page.goto(VIN_URL, wait_until="domcontentloaded")
    while True:
        ans = input(
            "\n--------------------------------------------------\n"
            "1) Log into VinSolutions in the browser window.\n"
            "2) When you see your VinSolutions home page, type 'yes'\n"
            "   and press Enter (or 'q' to quit): "
        ).strip().lower()
        if ans == "q":
            sys.exit("Quit.")
        if ans != "yes":
            print("   -> type 'yes' once you're logged in.")
            continue
        try:
            page.wait_for_selector("#ccrm-header-display-button",
                                   state="visible", timeout=8000)
            print("   -> Login confirmed. Starting the audit.\n")
            return
        except Exception:
            print("   -> Can't see VinSolutions yet. Finish logging in, "
                  "then type 'yes' again.")


SYSTEMS = [lookup_vinsolutions]

# ======================================================================
# ENGINE  (don't need to touch)
# ======================================================================
def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames)


def write_rows(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def row_is_done(row):
    if not LOOKUP_COLUMNS:
        return False
    return all((row.get(c) or "").strip() for c in LOOKUP_COLUMNS)


def fill_row(page, row):
    key = (row.get(KEY_COLUMN) or "").strip()
    if not key:
        print(f"  ! row missing {KEY_COLUMN}, skipping")
        return row
    for system in SYSTEMS:
        found = system(page, key) or {}
        for col, val in found.items():
            if col in JUDGMENT_COLUMNS:
                continue
            if col in row:
                row[col] = val
    return row


def main():
    path = Path(CSV_PATH)
    if not path.exists():
        sys.exit(f"CSV not found: {CSV_PATH}")

    rows, fieldnames = read_rows(path)
    for col in LOOKUP_COLUMNS + JUDGMENT_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
            for r in rows:
                r.setdefault(col, "")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=HEADLESS,
            viewport=WINDOW,
            args=[f"--window-size={WINDOW['width']},{WINDOW['height']}"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        wait_for_login(page)

        total = len(rows)
        for i, row in enumerate(rows, 1):
            key = (row.get(KEY_COLUMN) or "").strip()
            if row_is_done(row):
                print(f"[{i}/{total}] {key} done, skipping")
                continue
            print(f"[{i}/{total}] {key} ...")
            fill_row(page, row)
            write_rows(path, rows, fieldnames)
            time.sleep(0.5)

        ctx.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
