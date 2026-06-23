"""
coat_audit.py  --  COAT audit

One browser, one login, all rows. For each row: open the COAT app (via the
Microsoft MyApps launcher), search the ROOFTOP_ACCOUNT_CAID, open the matching
entity (double-click the entity-name cell), scroll to the business-operation
section, and read the value(s) in the ids-react-box element(s). The combined
value is written to the "COAT status" column. Saves after every row; resumes
if it crashes. Accounts are cached per run so a repeated CAID is only scraped
once.

Logs every step to the console AND to coat_log.txt.

SETUP (once):  pip install playwright   then:  playwright install chromium
RUN:           python coat_audit.py
"""

import csv
import sys
import time
import random
import logging
from pathlib import Path

# ======================================================================
# CONFIG
# ======================================================================
CSV_PATH = "audit.csv"
KEY_COLUMN = "ROOFTOP_ACCOUNT_CAID"          # column G, e.g. "CA11252814"

LOOKUP_COLUMNS = ["COAT status"]
JUDGMENT_COLUMNS = []

PROFILE_DIR = "coat_profile"                 # separate from the VinSolutions one
HEADLESS = False
WINDOW = {"width": 1920, "height": 1080}

# MyApps launcher link used for the FIRST Microsoft sign-in (SSO redirect).
COAT_URL = ("https://launcher.myapps.microsoft.com/api/signin/"
            "27bc914f-bfdb-4510-a676-0847856274e8"
            "?tenantId=7c7fea3f-e205-448e-b10a-701c54916e39")
# The COAT app home (search page). Each row navigates here for a fresh search,
# which avoids re-running the launcher's signin API on every lookup.
COAT_APP_URL = "https://coat2.coxautoinc.com/"

# Value written when there is no business-operation value to report.
COAT_NOT_FOUND_VALUE = "Coat no found"

# ---- Selectors (tune here after the first live run) ----
# Search box: the MUI text field. The css-* class is build-generated and may
# change, so we try several increasingly-generic selectors and use the first.
SEARCH_INPUT_SELECTORS = [
    "input.MuiInputBase-input",
    ".MuiInputBase-adornedEnd input",
    ".MuiInputBase-adornedStart input",
    ".MuiOutlinedInput-root input",
    ".css-1bt8r33 input",
    "input[type='text']",
]
# Magnifying-glass icon: matched by the start of its SVG path "d" attribute.
SEARCH_ICON_PATH = "path[d^='M6.80407 1.44974']"
# Search result cell to double-click to open the entity.
ENTITY_CELL = "td.ant-table-cell.entity-name"
# Section header and the value box(es) underneath it.
BIZ_OP_HEADER = "h4.business-operation-type-name"
VALUE_BOX = "div.ids-react-box"

LOG_FILE = "coat_log.txt"
_APP_HOME = None        # resolved app URL after login (avoids re-hitting signin)
_CACHE = {}             # CAID -> COAT status (one scrape per account per run)

# ======================================================================
# LOGGING
# ======================================================================
log = logging.getLogger("coat")


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s",
                            datefmt="%H:%M:%S")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.handlers.clear()
    log.addHandler(fh)
    log.addHandler(sh)


# ======================================================================
# COAT
# ======================================================================
def lookup_coat(page, key, row):
    """Return {'COAT status': value} for one ROOFTOP_ACCOUNT_CAID."""
    if key in _CACHE:
        log.info("[%s] cached -> %r", key, _CACHE[key])
        return {"COAT status": _CACHE[key]}

    log.info("[%s] navigating to COAT", key)
    page.goto(_APP_HOME or COAT_APP_URL, wait_until="domcontentloaded")

    # Readiness / auth check: the search box only exists once SSO is through.
    if _find_visible(page, SEARCH_INPUT_SELECTORS, timeout=20000) is None:
        log.error("[%s] search box not visible - not logged in?", key)
        raise SystemExit("COAT app not ready (login?). Log in, then re-run.")

    if not _search_account(page, key):
        return {}

    if not _open_entity(page, key):
        status = COAT_NOT_FOUND_VALUE
    else:
        status = _read_coat_status(page, key)

    _CACHE[key] = status
    log.info("[%s] COAT status=%r", key, status)
    return {"COAT status": status}


def _search_account(page, key):
    """Type the CAID into the search box and trigger the search."""
    box = _find_visible(page, SEARCH_INPUT_SELECTORS, timeout=15000)
    if box is None:
        log.error("[%s] search box not found", key)
        return False
    box.click()
    try:
        box.fill("")
    except Exception:
        pass
    _human_type(box, key)
    page.wait_for_timeout(600)
    _click_search(page, key)
    page.wait_for_timeout(1200)
    return True


def _click_search(page, key):
    """Click the magnifying-glass icon; fall back to pressing Enter."""
    try:
        icon = page.query_selector(SEARCH_ICON_PATH)
        if icon:
            handle = icon.evaluate_handle(
                "e => e.closest('button') || e.closest('[role=button]') "
                "|| e.closest('svg')")
            el = handle.as_element() if handle else None
            if el:
                el.click()
                log.info("[%s] clicked search icon", key)
                return True
    except Exception as e:
        log.warning("[%s] search icon click failed: %s", key, e)
    try:
        box = _find_visible(page, SEARCH_INPUT_SELECTORS, timeout=4000)
        if box:
            box.press("Enter")
            log.info("[%s] pressed Enter to search", key)
            return True
    except Exception as e:
        log.warning("[%s] Enter search failed: %s", key, e)
    return False


def _open_entity(page, key):
    """Double-click the first entity-name result cell to open the detail."""
    try:
        page.wait_for_selector(ENTITY_CELL, state="visible", timeout=15000)
    except Exception:
        log.error("[%s] no result row (entity-name cell) found", key)
        return False
    cells = page.query_selector_all(ENTITY_CELL)
    log.info("[%s] %d entity result(s)", key, len(cells))
    if not cells:
        return False
    try:
        cells[0].dblclick()
    except Exception as e:
        log.warning("[%s] dblclick failed (%s); trying single click", key, e)
        try:
            cells[0].click()
        except Exception as e2:
            log.error("[%s] could not open entity: %s", key, e2)
            return False
    page.wait_for_timeout(1500)
    return True


def _read_coat_status(page, key):
    """Find the business-operation section and join its ids-react-box value(s).

    Walks up from the header to the nearest container that holds ids-react-box
    elements, so we read the values that belong to that section. Returns the
    comma-joined values, or COAT_NOT_FOUND_VALUE when there are none.
    """
    ctx = _ctx_with(page, BIZ_OP_HEADER, total_wait=15000)
    if ctx is None:
        log.warning("[%s] business-operation header NOT found", key)
        return COAT_NOT_FOUND_VALUE

    # Scroll the header into view in case the values are lazily rendered.
    try:
        h = ctx.query_selector(BIZ_OP_HEADER)
        if h:
            h.scroll_into_view_if_needed()
            page.wait_for_timeout(600)
    except Exception:
        pass

    result = ctx.evaluate(
        """(sel) => {
            const h = document.querySelector(sel.header);
            if (!h) return {found: false, values: [], html: ""};
            let cont = h.parentElement;
            for (let i = 0; i < 6 && cont; i++) {
                const boxes = cont.querySelectorAll(sel.box);
                if (boxes.length) {
                    return {
                        found: true,
                        values: Array.from(boxes)
                            .map(b => (b.innerText || "").trim())
                            .filter(Boolean),
                        html: cont.outerHTML.slice(0, 1500)
                    };
                }
                cont = cont.parentElement;
            }
            return {found: true, values: [],
                    html: (h.parentElement || h).outerHTML.slice(0, 1500)};
        }""",
        {"header": BIZ_OP_HEADER, "box": VALUE_BOX})

    log.info("[%s] section HTML (first 1500): %s", key, result.get("html", ""))
    values = result.get("values") or []
    log.info("[%s] ids-react-box values: %r", key, values)
    if not values:
        return COAT_NOT_FOUND_VALUE
    return ", ".join(values)


# ======================================================================
# HELPERS
# ======================================================================
def _human_type(element, text):
    for ch in str(text):
        element.type(ch, delay=random.randint(70, 170))


def _find_visible(page, selectors, timeout=8000):
    for sel in selectors:
        try:
            page.wait_for_selector(sel, state="visible", timeout=timeout)
            el = page.query_selector(sel)
            if el:
                return el
        except Exception:
            continue
    return None


def _ctx_with(page, selector, total_wait=15000):
    """Return the page or the frame that contains the selector (or None)."""
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


def wait_for_login(page):
    global _APP_HOME
    page.goto(COAT_URL, wait_until="domcontentloaded")
    while True:
        ans = input(
            "\n--------------------------------------------------\n"
            "1) Log into COAT (Microsoft sign-in) in the browser window.\n"
            "2) When you see the COAT search page, type 'yes' and press\n"
            "   Enter (or 'q' to quit): "
        ).strip().lower()
        if ans == "q":
            sys.exit("Quit.")
        if ans != "yes":
            print("   -> type 'yes' once the COAT search page is loaded.")
            continue
        box = _find_visible(page, SEARCH_INPUT_SELECTORS, timeout=8000)
        if box:
            # Prefer the known app URL; fall back to wherever SSO landed.
            _APP_HOME = COAT_APP_URL if "coat2" in (page.url or "") else page.url
            log.info("login confirmed; app home=%s", _APP_HOME)
            return
        print("   -> Can't see the COAT search box yet. Finish loading, "
              "then type 'yes' again.")


SYSTEMS = [lookup_coat]

# ======================================================================
# ENGINE
# ======================================================================
def _detect_delimiter(path):
    """Guess the CSV delimiter from the header line (comma/semicolon/tab/pipe)."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            first = f.readline()
    except Exception:
        return ","
    counts = {d: first.count(d) for d in [",", ";", "\t", "|"]}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","


def read_rows(path):
    delim = _detect_delimiter(path)
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=delim)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    log.info("CSV delimiter=%r; columns found: %s", delim, fieldnames)
    return rows, fieldnames, delim


def write_rows(path, rows, fieldnames, delimiter=","):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)


def row_is_done(row):
    if not LOOKUP_COLUMNS:
        return False
    return all((row.get(c) or "").strip() for c in LOOKUP_COLUMNS)


def fill_row(page, row):
    key = (row.get(KEY_COLUMN) or "").strip()
    if not key:
        log.warning("row missing %s, skipping", KEY_COLUMN)
        return row
    for system in SYSTEMS:
        try:
            found = system(page, key, row) or {}
        except SystemExit:
            raise
        except Exception as e:
            log.exception("[%s] unexpected error: %s", key, e)
            found = {}
        for col, val in found.items():
            if col in JUDGMENT_COLUMNS:
                continue
            if col in row:
                row[col] = val
    return row


def main():
    setup_logging()
    log.info("=== COAT audit run started ===")

    path = Path(CSV_PATH)
    if not path.exists():
        sys.exit(f"CSV not found: {CSV_PATH}")

    rows, fieldnames, delim = read_rows(path)

    if KEY_COLUMN not in fieldnames:
        log.error("Key column %r NOT in CSV. Columns are: %s",
                  KEY_COLUMN, fieldnames)
        sys.exit(f"'{KEY_COLUMN}' column not found - check the headers logged "
                 f"above (wrong delimiter or different column name?).")

    nonempty = sum(1 for r in rows if (r.get(KEY_COLUMN) or "").strip())
    log.info("%d rows total; %d have a %s value (%d blank)",
             len(rows), nonempty, KEY_COLUMN, len(rows) - nonempty)

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
                log.info("[%d/%d] %s already done, skipping", i, total, key)
                continue
            log.info("[%d/%d] === processing %s ===", i, total, key)
            fill_row(page, row)
            write_rows(path, rows, fieldnames, delim)
            time.sleep(0.5)

        ctx.close()
    log.info("=== COAT audit run finished ===")


if __name__ == "__main__":
    main()
