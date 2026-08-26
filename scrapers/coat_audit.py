"""
coat_audit.py  --  COAT audit

One browser, one login, all rows. For each row: open the COAT app (via the
Microsoft MyApps launcher), search the ROOFTOP_ACCOUNT_CAID, open the matching
entity (double-click the entity-name cell), scroll to the business-operation
section, and read the value(s) in the ids-react-box element(s). The combined
value is written to the "COAT Status" column. Saves after every row; resumes
if it crashes. Accounts are cached per run so a repeated CAID is only scraped
once.

Logs every step to the console AND to coat_log.txt.

SETUP (once):  pip install playwright
               (no 'playwright install' needed - this drives the real
                Edge/Chrome already on the machine, not a bundled build)
RUN:           python coat_audit.py
"""

import csv
import os
import shutil
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

LOOKUP_COLUMNS = ["COAT Status"]
JUDGMENT_COLUMNS = []

PROFILE_DIR = "coat_profile"                 # separate from the VinSolutions one
BROWSER_CHANNEL = "msedge"   # "msedge" (Windows VDI / SSO) or "chrome"
CDP_PORT = 9223              # 9222 is sf_audit's; keep them distinct
HEADLESS = False
WINDOW = {"width": 1920, "height": 1080}

# The MyApps portal: sign in here, then click the COAT tile to launch the app.
# (Hitting the launcher link directly, before a portal session exists, fails -
# which is why the old flow didn't open the page.)
MYAPPS_URL = "https://myapps.microsoft.com/"
# The COAT app tile on the portal. We try its exact id, an id-prefix match,
# and finally the launcher link it points to.
COAT_TILE_SELECTORS = [
    "#product-tile-e093ea7a-a94a-4c35-8270-cd0a4bd84ffd-footer",
    "[id^='product-tile-e093ea7a-a94a-4c35-8270-cd0a4bd84ffd']",
    "a[href*='launcher.myapps.microsoft.com/api/signin/27bc914f']",
]
# The launcher link the tile points to (fallback if the tile can't be clicked).
COAT_URL = ("https://launcher.myapps.microsoft.com/api/signin/"
            "27bc914f-bfdb-4510-a676-0847856274e8"
            "?tenantId=7c7fea3f-e205-448e-b10a-701c54916e39")
# The COAT search page is the app root (the search box lives there). Each row
# navigates here for a fresh search, which also avoids re-running the launcher.
COAT_APP_URL = "https://coat2.coxautoinc.com/"

# Value written when there is no business-operation value to report.
COAT_NOT_FOUND_VALUE = "Coat no found"

# ---- Selectors (from the real COAT page) ----
# Search box: exact id, with placeholder/MUI fallbacks just in case.
SEARCH_INPUT_SELECTORS = [
    "#quick-search-box",                       # exact id on the COAT search page
    "input[autocomplete='quickSerach']",       # (their spelling) autocomplete attr
    "input[placeholder*='common org']",
    "input.MuiInputBase-input",
    "input[type='text']",
]
# Magnifying-glass icon: the IDS React component; SVG path is a fallback.
SEARCH_ICON_SELECTORS = [
    "[data-ids-react-component-name='ids-react-MagnifyingGlassIcon']",
    "svg[data-ids-react-component-name='ids-react-MagnifyingGlassIcon']",
    "path[d^='M6.80407 1.44974']",
]
# Search-result rows in the ant-table; double-click the first to open it.
ENTITY_CELL_SELECTORS = [
    "td.ant-table-cell.entity-name",
    ".ant-table-tbody tr.ant-table-row td.entity-name",
    ".ant-table-tbody tr.ant-table-row",
]
# Section header and the value box(es) to read (by IDS React component name).
BIZ_OP_HEADER = "h4.business-operation-type-name"
VALUE_BOX = "[data-ids-react-component-name='ids-react-box']"

LOG_FILE = "coat_log.txt"
_APP_HOME = None        # resolved app URL after login (avoids re-hitting signin)
_CACHE = {}             # CAID -> COAT status (one scrape per account per run)


def get_browser_executable():
    """Return the browser executable to drive, honouring BROWSER_CHANNEL.

    On a Windows VDI, Conditional Access refuses a blank Chrome profile
    because it cannot present device identity. Edge integrates with the
    machine's PRT natively, so it passes where Chrome does not. Set
    BROWSER_CHANNEL = "chrome" to go back to Chrome.
    """
    edge_first = BROWSER_CHANNEL == "msedge"
    names = ["msedge", "chrome"] if edge_first else ["chrome", "msedge"]
    candidates = []
    for name in names:
        if name == "msedge":
            candidates += [shutil.which("msedge")]
            if os.name == "nt":
                roots = [
                    os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                    os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                    os.environ.get("LOCALAPPDATA"),
                ]
                candidates += [
                    str(Path(r) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
                    for r in roots if r
                ]
            elif sys.platform == "darwin":
                candidates += [
                    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                ]
            else:
                candidates += ["/usr/bin/microsoft-edge"]
        else:
            candidates += [
                shutil.which("google-chrome"),
                shutil.which("google-chrome-stable"),
                shutil.which("chrome"),
            ]
            if os.name == "nt":
                roots = [
                    os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                    os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                    os.environ.get("LOCALAPPDATA"),
                ]
                candidates += [
                    str(Path(r) / "Google" / "Chrome" / "Application" / "chrome.exe")
                    for r in roots if r
                ]
            elif sys.platform == "darwin":
                candidates += [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                ]
            else:
                candidates += ["/usr/bin/google-chrome", "/opt/google/chrome/chrome"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise FileNotFoundError(
        "Neither Microsoft Edge nor Google Chrome was found. "
        "Install one or add it to PATH."
    )


# Backwards-compatible alias.
get_chrome_executable = get_browser_executable


def chrome_app_bundle(binary_path):
    """Return the enclosing .app bundle for a Chrome binary, or None."""
    for parent in Path(binary_path).parents:
        if parent.suffix == ".app":
            return str(parent)
    return None

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
        return {"COAT Status": _CACHE[key]}

    log.info("[%s] navigating to COAT", key)
    try:
        page.goto(_APP_HOME or COAT_APP_URL, wait_until="domcontentloaded",
                  timeout=20000)
    except Exception:
        pass

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
    return {"COAT Status": status}


def _search_account(page, key):
    """Put the CAID in the search box (reliably, like a paste) and search."""
    box = _find_visible(page, SEARCH_INPUT_SELECTORS, timeout=15000)
    if box is None:
        log.error("[%s] search box not found", key)
        return False
    try:
        box.click()
        box.fill(key)               # set the whole value at once (paste-like)
    except Exception as e:
        log.warning("[%s] fill failed (%s); typing instead", key, e)
        try:
            box.fill("")
        except Exception:
            pass
        _human_type(box, key)
    page.wait_for_timeout(500)
    try:                            # confirm the value actually landed
        log.info("[%s] search box value=%r", key, box.input_value())
    except Exception:
        pass
    try:
        box.press("Enter")          # Enter is the most reliable trigger
    except Exception as e:
        log.warning("[%s] Enter on search box failed: %s", key, e)
    _click_search(page, key)        # also click the magnifying glass
    page.wait_for_timeout(2500)     # give the results time to load
    return True


def _click_search(page, key):
    """Click the magnifying-glass icon; fall back to pressing Enter."""
    for sel in SEARCH_ICON_SELECTORS:
        icon = page.query_selector(sel)
        if not icon:
            continue
        try:
            handle = icon.evaluate_handle(
                "e => e.closest('button') || e.closest('[role=button]') || e")
            el = handle.as_element() if handle else None
            (el or icon).click()
            log.info("[%s] clicked search icon (%s)", key, sel)
            return True
        except Exception as e:
            log.warning("[%s] icon click failed (%s): %s", key, sel, e)
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
    """Double-click the first result row to open the detail."""
    # Poll up to ~12s for results to render (any of the selectors).
    cells, used, waited = None, None, 0
    while waited < 12 and not cells:
        for sel in ENTITY_CELL_SELECTORS:
            found = page.query_selector_all(sel)
            if found:
                cells, used = found, sel
                break
        if cells:
            break
        page.wait_for_timeout(500)
        waited += 0.5
    if not cells:
        # Save a screenshot + dump HTML so we can SEE the real results structure.
        try:
            page.screenshot(path="coat_debug.png", full_page=True)
            log.info("[%s] saved screenshot -> coat_debug.png", key)
        except Exception as e:
            log.warning("[%s] screenshot failed: %s", key, e)
        try:
            html = page.evaluate(
                "() => {const t = document.querySelector('.ant-table')"
                " || document.querySelector('[role=listbox]')"
                " || document.querySelector('[class*=autocomplete]')"
                " || document.querySelector('[class*=result]')"
                " || document.querySelector('main') || document.body;"
                " return t ? t.outerHTML.slice(0, 3000) : '(no container)';}")
            log.info("[%s] results-area HTML: %s", key, html)
        except Exception:
            pass
        log.error("[%s] no result rows found (tried %s)", key, ENTITY_CELL_SELECTORS)
        return False
    log.info("[%s] %d result(s) via %s", key, len(cells), used)
    try:
        cells[0].scroll_into_view_if_needed()
    except Exception:
        pass
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
    """Find the VinSolutions section under Business Operation IDs and read
    its ids-react-box value(s). Returns comma-joined values, or
    COAT_NOT_FOUND_VALUE when no VinSolutions section / no values."""
    ctx = _ctx_with(page, BIZ_OP_HEADER, total_wait=15000)
    if ctx is None:
        log.warning("[%s] business-operation headers NOT found", key)
        return COAT_NOT_FOUND_VALUE

    result = ctx.evaluate(
        """(sel) => {
            // Find ALL h4.business-operation-type-name, pick "VinSolutions".
            const headers = document.querySelectorAll(sel.header);
            let vinH = null;
            const names = [];
            for (const h of headers) {
                const txt = (h.innerText || "").trim();
                names.push(txt);
                if (txt.toLowerCase().includes("vinsolution")) vinH = h;
            }
            if (!vinH) return {found: false, values: [], names: names, html: ""};
            // The VinSolutions section lives inside a container like
            // div#VIN-boid-assignments-list-item. Walk up to find it,
            // then read all ids-react-box values inside it.
            let cont = vinH.closest('[id*="VIN"][id*="boid"]')
                    || vinH.closest('.ids-list-item')
                    || vinH.closest('.boid-assignments-list-item');
            // Fallback: walk up until we find ids-react-box elements.
            if (!cont) {
                cont = vinH.parentElement;
                for (let i = 0; i < 8 && cont; i++) {
                    if (cont.querySelectorAll(sel.box).length) break;
                    cont = cont.parentElement;
                }
            }
            if (!cont) cont = vinH.parentElement;
            const noAssign = cont.querySelector('[class*="no-assignments"],'
                + '[data-testid*="no-VIN-assignments"]');
            if (noAssign) return {found: true, values: [], names: names,
                    noAssign: true, html: cont.outerHTML.slice(0, 1500)};
            const boxes = cont.querySelectorAll(sel.box);
            return {
                found: true,
                values: Array.from(boxes)
                    .map(b => (b.innerText || "").trim())
                    .filter(Boolean),
                names: names,
                html: cont.outerHTML.slice(0, 1500)
            };
        }""",
        {"header": BIZ_OP_HEADER, "box": VALUE_BOX})

    names = result.get("names") or []
    log.info("[%s] sections found: %s", key, names)
    if not result.get("found"):
        log.warning("[%s] 'VinSolutions' section NOT found among: %s", key, names)
        return COAT_NOT_FOUND_VALUE
    log.info("[%s] VinSolutions HTML (first 1500): %s", key, result.get("html", ""))
    values = result.get("values") or []
    log.info("[%s] VinSolutions ids-react-box values: %r", key, values)
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
    """Navigate to COAT using the SSO cookies saved during the manual Phase 1.
    The user already signed in and opened COAT in a plain browser; now
    Playwright re-opens the same profile and goes straight to the app."""
    global _APP_HOME
    try:
        page.goto(COAT_APP_URL, wait_until="domcontentloaded", timeout=25000)
    except Exception:
        pass
    box = _find_visible(page, SEARCH_INPUT_SELECTORS, timeout=15000)
    if box:
        _APP_HOME = page.url or COAT_APP_URL
        log.info("COAT open; app home=%s", _APP_HOME)
        return page
    log.warning("search box not found at %s; asking user", page.url)
    ctx = page.context
    while True:
        ans = input(
            "\n--------------------------------------------------\n"
            "  Couldn't find the COAT search bar automatically.\n"
            "  In the browser, navigate to the COAT search page,\n"
            "  then type 'yes' and press Enter (or 'q' to quit): "
        ).strip().lower()
        if ans == "q":
            sys.exit("Quit.")
        if ans != "yes":
            continue
        for p in reversed(ctx.pages):
            try:
                b = _find_visible(p, SEARCH_INPUT_SELECTORS, timeout=3000)
                if b:
                    _APP_HOME = p.url or COAT_APP_URL
                    log.info("COAT open; app home=%s", _APP_HOME)
                    return p
            except Exception:
                continue
        print("   -> Still can't find the search box. Try again.")


SYSTEMS = [lookup_coat]

# ======================================================================
# ENGINE
# ======================================================================
_ENC_CACHE = {}


def _read_encoding(path):
    """Return the encoding to read a CSV with: UTF-8 (with optional BOM) when
    it decodes cleanly, otherwise Windows-1252. Excel often re-saves CSVs as
    Windows-1252, where byte 0xA0 (non-breaking space) is NOT valid UTF-8.
    Cached per path so we only probe (and warn) once per run."""
    key = str(path)
    if key in _ENC_CACHE:
        return _ENC_CACHE[key]
    try:
        with open(path, "rb") as fb:
            if fb.read(4) == b"PK\x03\x04":          # ZIP magic = .xlsx, not CSV
                log.error("%s looks like an Excel .xlsx workbook, not a CSV. "
                          "Open it in Excel and use Save As -> CSV UTF-8.", path)
    except Exception:
        pass
    enc = "latin-1"
    for cand in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, encoding=cand) as f:
                f.read()
            enc = cand
            break
        except UnicodeDecodeError:
            continue
    if enc != "utf-8-sig":
        log.warning("%s is not UTF-8; reading as %s", path, enc)
    _ENC_CACHE[key] = enc
    return enc


def _detect_delimiter(path):
    """Guess the CSV delimiter from the header line (comma/semicolon/tab/pipe)."""
    try:
        with open(path, newline="", encoding=_read_encoding(path)) as f:
            first = f.readline()
    except Exception:
        return ","
    counts = {d: first.count(d) for d in [",", ";", "\t", "|"]}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","


def read_rows(path):
    delim = _detect_delimiter(path)
    with open(path, newline="", encoding=_read_encoding(path)) as f:
        reader = csv.DictReader(f, delimiter=delim)
        rows = list(reader)
        raw_fieldnames = list(reader.fieldnames or [])
    fieldnames = [h.strip() for h in raw_fieldnames]
    if fieldnames != raw_fieldnames:
        log.warning("stripped whitespace from header(s): %s",
                    {o: n for o, n in zip(raw_fieldnames, fieldnames) if o != n})
        rows = [{h.strip(): v for h, v in r.items()} for r in rows]
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

    import subprocess
    from playwright.sync_api import sync_playwright

    # Phase 1: open the installed Chrome browser directly so it runs at full
    # speed while the user signs in and navigates to COAT manually.
    browser_path = get_chrome_executable()

    profile_abs = str(Path(PROFILE_DIR).resolve())

    # Phase 1: open the real browser so the user can sign in at full speed.
    # A fixed CDP port lets Phase 2 attach to THIS SAME window instead of
    # killing it and re-opening the profile. That matters on Windows, where
    # the old kill-and-relaunch could not work: "pkill" does not exist, and
    # the launcher process exits immediately after handing off to a detached
    # browser, so proc.terminate() killed nothing and the profile stayed
    # locked. Attaching avoids the whole problem on every platform - and it
    # keeps the signed-in session live rather than relying on cookies having
    # been flushed to disk.
    proc = subprocess.Popen([
        browser_path,
        f"--user-data-dir={profile_abs}",
        f"--window-size={WINDOW['width']},{WINDOW['height']}",
        f"--remote-debugging-port={CDP_PORT}",
        MYAPPS_URL,
    ])

    input(
        "\n--------------------------------------------------\n"
        "  The browser opened myapps.microsoft.com for you.\n"
        "  Now do your thing:\n"
        "    - Sign in\n"
        "    - Open a new tab, paste the COAT link, click the app\n"
        "    - Navigate until you see the COAT search bar\n\n"
        "  Leave the browser OPEN - this script attaches to it.\n"
        "  When the COAT search page is ready, type 'yes' and Enter: "
    )

    # Phase 2: attach to the browser from Phase 1 over CDP.
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(
            f"http://127.0.0.1:{CDP_PORT}",
            timeout=30000,
        )
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = None
        for pg in ctx.pages:
            if "coat" in pg.url.lower():
                page = pg
                break
        if page is None:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()

        page = wait_for_login(page)   # finds COAT via saved session

        total = len(rows)
        for i, row in enumerate(rows, 1):
            key = (row.get(KEY_COLUMN) or "").strip()
            if row_is_done(row):
                log.info("[%d/%d] %s already done, skipping", i, total, key)
                continue
            log.info("[%d/%d] === processing %s ===", i, total, key)
            fill_row(page, row)
            write_rows(path, rows, fieldnames, delim)

        browser.close()   # detach from CDP; the window itself stays open

    proc.terminate()      # close the browser window we launched in Phase 1
    proc.wait()
    log.info("=== COAT audit run finished ===")


if __name__ == "__main__":
    main()
