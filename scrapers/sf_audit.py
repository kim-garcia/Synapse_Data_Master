"""
sf_audit.py  --  Salesforce asset status audit

Single browser: Chromium is launched with a CDP port for manual SSO login,
then Playwright attaches to that same window (connect_over_cdp) to automate.
For each row in audit.csv:
  1. Global-search for ASSET_BILLTO_ROOFTOP_CAID
  2. Click the 'Accounts' scope in the results sidebar, then open the
     grid row whose ID column matches the CAID   ->  Asset Name Check 2
  3. Navigate to the Related / Assets tab
  4. Filter panel: Clear All Filters, then Save
  5. Find the grid row whose Asset Name matches PRODUCT_NAME, read Status

Adds two columns to audit.csv (created at the end if missing):
  Asset Name Check 2  --  the grid Asset Name that matched PRODUCT_NAME
  Asset Status        --  that row's Status, e.g. Installed / Obsolete

RUN:   python sf_audit.py
"""

import csv
import os
import re
import shutil
import subprocess
import sys
import time
import logging
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ======================================================================
# CONFIG
# ======================================================================
CSV_PATH        = "audit.csv"
KEY_COLUMN      = "ROOFTOP_ACCOUNT_CAID"   # SF global-search key
PRODUCT_COLUMN  = "PRODUCT_NAME"                # asset to locate in the list
PRICE_COLUMN    = "ASSET_TOTAL_PRICE"           # confirms the matched row
PRICE_TOLERANCE = 0.011                         # <= 1 cent difference is a match

PROFILE_DIR     = "sf_profile"
BROWSER_CHANNEL = "chrome"   # "msedge" (Windows VDI / SSO) or "chrome"
LOG_FILE        = "sf_audit_log.txt"
WINDOW          = {"width": 1920, "height": 1080}
CDP_PORT        = 9222   # fixed port so Playwright can attach to the same browser

SF_HOME_URL     = "https://casfx.lightning.force.com/"

COL_SF_NAME     = "Asset Name Check 2"      # raw SF account name
COL_SF_STATUS   = "Asset Status 2"            # e.g. Installed / Active
COL_SF_PRICE    = "SFX asset Check 2 Price"  # matched asset's SF Total Price
COL_SF_INSTALL  = "SFX Install Date 2"        # matched asset's Install Date
COL_SF_BILLTHRU = "SFX Bill Through Date 2"   # matched asset's Bill Through Date
NEW_COLUMNS     = [COL_SF_NAME, COL_SF_STATUS, COL_SF_PRICE,
                   COL_SF_INSTALL, COL_SF_BILLTHRU]

SF_NOT_FOUND_VALUE = "SF not found"

# Products whose CSV PRODUCT_NAME differs entirely from the Salesforce asset
# name (no shared words, so token/fuzzy scoring can never match them).  Map
# the CSV name to the SF asset name(s) it appears as.  Keys and values are
# matched case-insensitively and comma-insensitively (same norm() used for
# every other name).  Add new lines here as you discover more renames.
PRODUCT_ALIASES = {
    "predictive insights":              ["Automotive Intelligence Package"],
    "predictive insights with gen ai":  ["Automotive Intelligence Package"],
}

STEP_PAUSE      = 2      # seconds to pause after each step (visual check)

# ======================================================================
# LOGGING
# ======================================================================
log = logging.getLogger("sf_audit")


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
# SELECTOR HELPERS
# ======================================================================
def _find_visible(page, selectors, timeout=8000):
    """Return the first visible Locator matching any selector.

    Uses page.locator() (not wait_for_selector) so Playwright's extended
    selector syntax — :has-text(), :text(), etc. — works correctly.
    wait_for_selector only accepts plain CSS/XPath and silently drops
    extended pseudo-classes, causing :has-text() selectors to never match.
    """
    if isinstance(selectors, str):
        selectors = [selectors]
    per = max(500, timeout // max(1, len(selectors)))
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=per)
            return loc
        except Exception:
            pass
    return None


def _try_click(page, selectors, timeout=6000):
    el = _find_visible(page, selectors, timeout)
    if el:
        el.click()
        return True
    return False


# ======================================================================
# CSV ENGINE  (identical pattern to audit_automation.py and coat_audit.py)
# ======================================================================
_ENC_CACHE = {}


def _read_encoding(path):
    key = str(path)
    if key in _ENC_CACHE:
        return _ENC_CACHE[key]
    try:
        with open(path, "rb") as fb:
            if fb.read(4) == b"PK\x03\x04":
                log.error("%s looks like an Excel .xlsx workbook, not a CSV. "
                          "Open it in Excel -> Save As -> CSV UTF-8.", path)
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
    # Strip header whitespace (Excel sometimes adds trailing spaces)
    fieldnames = [h.strip() for h in raw_fieldnames]
    if fieldnames != raw_fieldnames:
        renamed = {o: n for o, n in zip(raw_fieldnames, fieldnames) if o != n}
        log.warning("stripped whitespace from %d header(s): %s", len(renamed), renamed)
        rows = [{h.strip(): v for h, v in r.items()} for r in rows]
    log.info("CSV delimiter=%r; columns: %s", delim, fieldnames)
    return rows, fieldnames, delim


def write_rows(path, rows, fieldnames, delimiter=","):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)


# ======================================================================
# SALESFORCE AUTOMATION
# ======================================================================

# TWO DIFFERENT ELEMENTS in the Salesforce header — do not confuse them:
#
#   SEARCH_BUTTON = the collapsed "Search..." button shown by default.
#                   The real text input DOES NOT EXIST until it is clicked.
#   SEARCH_INPUT  = the GLOBAL search input that appears after:
#                     <input class="slds-input" type="search"
#                            placeholder="Search..." maxlength="100"
#                            aria-controls="suggestionsList-...">
#
# The input selector MUST use aria-controls^='suggestionsList' and the EXACT
# placeholder "Search...":  only the global search input has the suggestions
# dropdown.  Looser selectors (input-container, slds-input, type=search)
# also match the "Search this list..." box of Console list views and the
# script types the CAID into the WRONG field.
SEARCH_BUTTON_SELECTOR = (
    "#oneHeader .slds-global-header__item_search button.search-button")
SEARCH_INPUT_SELECTOR = (
    "input.slds-input[type='search'][placeholder='Search...']"
    "[aria-controls^='suggestionsList']")


def wait_for_login(page):
    """Ensure the attached tab shows Salesforce with the global search bar.

    The tab we attach to over CDP is already logged in and sitting on
    Salesforce, so we only navigate if it's somewhere else entirely.
    """
    if "force.com" not in (page.url or ""):
        log.info("attached tab is at %r — navigating to Salesforce home", page.url)
        try:
            page.goto(SF_HOME_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

    bar = _find_visible(page, [SEARCH_BUTTON_SELECTOR, SEARCH_INPUT_SELECTOR],
                        timeout=20000)

    if bar:
        log.info("Salesforce ready at: %s", page.url)
        return page

    log.warning("search bar not detected; asking user to navigate manually")
    print("\n>>> Salesforce search bar not visible.")
    print("    Navigate to the Salesforce home page in the browser, then come back here.")
    input("    When the search bar is visible, press Enter: ")
    return page


def _sf_global_search(page, caid):
    """
    STEP 1  make sure the search input is open (click the collapsed
            "Search..." button only if the input is not there yet)
    STEP 2  paste the CAID into the input
    STEP 3  press ENTER
    A pause of STEP_PAUSE seconds follows each step so the run can be
    watched live and each step verified in the log.
    """
    inp = page.locator(SEARCH_INPUT_SELECTOR).first

    # ── STEP 1: search input open ────────────────────────────────────────
    try:
        if not inp.is_visible():
            page.locator(SEARCH_BUTTON_SELECTOR).first.click(timeout=8000)
            inp.wait_for(state="visible", timeout=8000)
    except Exception:
        log.warning("[%s] STEP 1 FAILED — search input not available (url=%s)",
                    caid, page.url)
        return False
    log.info("[%s] STEP 1 OK — search input open", caid)
    time.sleep(STEP_PAUSE)

    # ── STEP 2: paste the CAID ───────────────────────────────────────────
    try:
        inp.click()
        inp.fill(caid)
        typed = (inp.input_value() or "").strip()
    except Exception as exc:
        log.warning("[%s] STEP 2 FAILED — could not fill CAID: %s", caid, exc)
        return False
    if typed != caid:
        log.warning("[%s] STEP 2 FAILED — input contains %r instead of the CAID",
                    caid, typed)
        return False
    log.info("[%s] STEP 2 OK — CAID pasted in global search input (value=%r)",
             caid, typed)
    time.sleep(STEP_PAUSE)

    # ── STEP 3: ENTER ────────────────────────────────────────────────────
    inp.press("Enter")
    log.info("[%s] STEP 3 OK — ENTER pressed, search submitted", caid)
    time.sleep(STEP_PAUSE)
    return True


def _click_accounts_scope(page, caid):
    """
    In the search results page, the left nav ("Searchable objects from
    navigation bar") lists scopes: Top Results, Accounts, Chatter, Cases...
    Click the one titled 'Accounts' so the results grid shows only accounts.
    Returns True if clicked (or already active), False if the scope never
    appeared.
    """
    # Wait for the scopes list to render
    try:
        page.locator(".forceSearchScopeItem").first.wait_for(
            state="visible", timeout=15000)
    except Exception:
        log.warning("[%s] search scopes sidebar not visible after 15s", caid)
        return False

    try:
        result = page.evaluate("""
            () => {
                const links = document.querySelectorAll(
                    '.forceSearchScopeItem a.scopesItem, a.slds-nav-vertical__action');
                for (const a of links) {
                    const title = (a.getAttribute('title') || '').trim().toLowerCase();
                    const name  = a.querySelector('.scopesItem_name');
                    const text  = name ? name.textContent.trim().toLowerCase() : '';
                    if (title === 'accounts' || text === 'accounts') {
                        const badge = a.querySelector('.slds-badge');
                        const count = badge ? badge.textContent.trim() : '';
                        if (a.getAttribute('aria-current') === 'page') {
                            return {status: 'already-active', count};
                        }
                        a.click();
                        return {status: 'clicked', count};
                    }
                }
                return {status: 'not-found', count: ''};
            }
        """)
    except Exception as exc:
        log.warning("[%s] JS error clicking Accounts scope: %s", caid, exc)
        result = {"status": "error", "count": ""}

    status = result.get("status")
    count = result.get("count", "")
    log.info("[%s] Accounts scope: %s (badge=%r)", caid, status, count)

    if status in ("clicked", "already-active"):
        if count == "0":
            log.warning("[%s] Accounts scope badge is 0 — no account results", caid)
        return True

    # Fallback: Playwright locator
    if _try_click(page, [
        "a.scopesItem[title='Accounts']",
        ".forceSearchScopeItem a[title='Accounts']",
    ], timeout=5000):
        log.info("[%s] Accounts scope clicked via Playwright fallback", caid)
        return True

    log.warning("[%s] Accounts scope not found in sidebar", caid)
    return False


# Search-results grid rows (Accounts scope).  Comma-selector matches either
# the scoped grid or a bare virtual table.  Playwright locators pierce any
# shadow roots, so this replaces the old document.querySelectorAll scan.
RESULTS_ROW_SELECTOR = (
    ".forceSearchResultsGridLVM table.uiVirtualDataTable tbody tr, "
    "table.uiVirtualDataTable tbody tr")


def _scan_results_grid(page, caid):
    """Scan the search-results grid with Playwright locators for the row
    whose ID cell equals the CAID.  Logs every row's account link and cell
    values so a miss is easy to diagnose.  Returns
    {matched, rowCount, href, title, seen}.
    """
    want = (caid or "").strip().lower()
    rows = page.locator(RESULTS_ROW_SELECTOR)
    try:
        n = rows.count()
    except Exception:
        n = 0
    log.info("[%s] STEP 7 scan — %d result row(s) found", caid, n)

    seen = []
    for idx in range(n):
        row = rows.nth(idx)
        link = row.locator(
            "th a[data-refid='recordId'], th a.forceOutputLookup, "
            "th a[href*='/lightning/r/']").first
        try:
            if not link.count():
                log.info("[%s] STEP 7 row %d — no account link, skipping",
                         caid, idx)
                continue
            href = link.get_attribute("href") or ""
            title = (link.get_attribute("title")
                     or (link.text_content() or "").strip())
        except Exception as exc:
            log.info("[%s] STEP 7 row %d — read error: %s", caid, idx, exc)
            continue

        cells = row.locator("td span.uiOutputText, td span.slds-truncate")
        try:
            cn = cells.count()
        except Exception:
            cn = 0
        row_vals = []
        for cidx in range(cn):
            try:
                val = (cells.nth(cidx).get_attribute("title")
                       or (cells.nth(cidx).text_content() or "")).strip().lower()
            except Exception:
                val = ""
            if val:
                row_vals.append(val)
        log.info("[%s] STEP 7 row %d — account=%r cells=%s",
                 caid, idx, title, row_vals)
        seen.extend(row_vals)
        if want in row_vals:
            log.info("[%s] STEP 7 row %d MATCHES CAID %r", caid, idx, caid)
            return {"matched": True, "rowCount": n,
                    "href": href, "title": title, "seen": seen[:20]}

    return {"matched": False, "rowCount": n, "href": "", "title": "",
            "seen": seen[:20]}


def _open_account_from_results(page, caid, url_before=""):
    """
    After the search is submitted, SF does ONE of two things (both seen in
    the run log):
      a) shows the full search results page (scopes sidebar + grid), or
      b) jumps STRAIGHT to the account record page when the term uniquely
         matches a suggestion (URL /lightning/r/Account/...).
    For (a): click the 'Accounts' scope, wait for the grid, find the row whose
    ID column matches the CAID, open its account link (th a[data-refid]).
    For (b): accept the record page directly.
    Returns the account name string, or None on failure.

    NOTE: never wait on a /lightning/search URL — in Console apps the results
    page URL is /one/one.app#<base64> (confirmed in the run log).
    """
    log.info("[%s] url before results wait: %s", caid, page.url)

    # ── STEP 4: wait for the search navbar (scopes sidebar) ─────────────
    #    ...or a direct jump to the account record, which SF does when the
    #    term uniquely matches a suggestion (seen in run log 16:04).
    outcome = None
    deadline = time.time() + 20
    while time.time() < deadline:
        url = page.url
        if "/lightning/r/" in url and url != url_before:
            outcome = "direct"
            break
        try:
            if page.locator(".forceSearchScopeItem").first.is_visible():
                outcome = "results"
                break
        except Exception:
            pass
        time.sleep(0.5)

    if outcome == "direct":
        time.sleep(3)   # let the record page LWC components boot
        # Page title is "<Account Name> | Salesforce"
        account_name = (page.title() or "").split("|")[0].strip()
        log.info("[%s] STEP 4 OK — search jumped directly to account record: "
                 "%r (%s)", caid, account_name, page.url)
        return account_name or page.url

    if outcome is None:
        log.warning("[%s] STEP 4 FAILED — search navbar never appeared "
                    "in 20s (url=%s)", caid, page.url)
        return None

    log.info("[%s] STEP 4 OK — search navbar visible", caid)
    time.sleep(STEP_PAUSE)

    # ── STEP 5: click 'Accounts' in the navbar ───────────────────────────
    if _click_accounts_scope(page, caid):
        log.info("[%s] STEP 5 OK — Accounts option clicked", caid)
    else:
        log.warning("[%s] STEP 5 WARNING — Accounts option not clicked; "
                    "trying the grid anyway", caid)
    time.sleep(STEP_PAUSE)

    # ── STEP 6: wait for the results grid ───────────────────────────────
    grid_visible = False
    for sel in [
        ".searchScrollerWrapper table.uiVirtualDataTable tbody tr",
        ".forceSearchResultsGridLVM table.uiVirtualDataTable tbody tr",
        "table.uiVirtualDataTable tbody tr",
    ]:
        try:
            page.locator(sel).first.wait_for(state="visible", timeout=12000)
            grid_visible = True
            break
        except Exception:
            pass
    if not grid_visible:
        log.warning("[%s] STEP 6 FAILED — results grid never appeared — url=%s",
                    caid, page.url)
        return None
    log.info("[%s] STEP 6 OK — results grid visible", caid)
    time.sleep(STEP_PAUSE)

    # ── STEP 7: match CAID in the grid ───────────────────────────────────
    #    Scan grid rows: match the CAID against the ID column, grab the
    #    account link from the row-header cell.  Polls for up to 15s because
    #    the grid on screen may still be the PREVIOUS row's results until the
    #    new search finishes rendering.
    result = None
    deadline = time.time() + 15
    scope_retried = False
    attempt = 0
    while True:
        attempt += 1
        log.info("[%s] STEP 7 scan attempt %d", caid, attempt)
        try:
            result = _scan_results_grid(page, caid)
        except Exception as exc:
            log.warning("[%s] row scan error: %s", caid, exc)
            result = None
        if result and result.get("matched"):
            break
        if time.time() > deadline:
            break
        # The new results may have re-rendered the sidebar back to Top Results
        if not scope_retried:
            _click_accounts_scope(page, caid)
            scope_retried = True
        time.sleep(1)

    if not result:
        return None

    if result.get("matched"):
        href = result["href"]
        account_name = result.get("title") or href
        log.info("[%s] STEP 7 OK — CAID matched in grid: account=%r href=%r",
                 caid, account_name, href)
    else:
        log.warning("[%s] STEP 7 FAILED — no row with ID matching CAID "
                    "(rows=%s, cell values seen=%s)",
                    caid, result.get("rowCount"), result.get("seen"))
        return None
    time.sleep(STEP_PAUSE)

    if not href or "/lightning/r/" not in href:
        log.warning("[%s] unexpected account href %r", caid, href)
        return None

    # ── STEP 8: open the account record ─────────────────────────────────
    #    SF record links have a real /lightning/r/... href.  Navigate
    #    directly rather than clicking — the link has target="_blank" which
    #    would open a new tab on click.
    if href.startswith("/"):
        href = SF_HOME_URL.rstrip("/") + href
    page.goto(href, wait_until="domcontentloaded", timeout=20000)

    # Give SF Lightning time to boot its LWC components after domcontentloaded.
    # The tab bar renders asynchronously — without this wait, tab selectors return 0 results.
    time.sleep(3)
    log.info("[%s] STEP 8 OK — account page open: %s", caid, page.url)
    return account_name


def _wait_tabs_ready(page, caid, timeout=30000):
    """Wait until the record page tab bar has finished booting.

    SF first paints placeholder tabs labelled 'Loading...' and only later
    swaps in the real labels (Details, Related, Assets, ...).  If we read the
    tabs during that race we never find 'Assets', fall through every fallback
    and land on STEP 9 with 0 rows (confirmed in the run log for CA11212546).

    Returns when the 'Assets' tab is present, OR when real (non-'Loading')
    tabs exist alongside a 'More' overflow button (Assets may be hidden in
    the overflow).  Returns False on timeout but the caller proceeds anyway.
    """
    deadline = time.time() + timeout / 1000
    last = []
    while time.time() < deadline:
        try:
            labels = page.evaluate("""
                () => Array.from(document.querySelectorAll(
                    'a[role="tab"], .slds-tabs_default__link, '
                    + 'li.slds-tabs_default__item a'
                )).map(t => (t.getAttribute('data-label')
                             || t.textContent || '').trim())
            """)
        except Exception:
            labels = []
        real = [l for l in labels if l and "loading" not in l.lower()]
        if any(l.lower() == "assets" for l in real):
            log.info("[%s] tab bar ready — 'Assets' tab present", caid)
            return True
        if real:
            last = real
            try:
                more = page.locator(
                    "li.slds-tabs_default__overflow-button button, "
                    "button[title='More']").first
                if more.count() and more.is_visible():
                    log.info("[%s] tab bar ready — real tabs + 'More' overflow "
                             "(Assets may be in overflow); tabs: %s", caid, real)
                    return True
            except Exception:
                pass
        time.sleep(0.5)
    log.warning("[%s] tab bar still not ready after %ds (labels seen: %s) — "
                "trying anyway", caid, timeout // 1000, last)
    return False


def _navigate_to_assets_tab(page, caid):
    """
    Click the 'Assets' tab on the Account record page.

    Uses JavaScript as the primary method (immune to SF LWC attribute variations).
    Falls back to Playwright locators, then the 'More' overflow dropdown.
    """
    # Wait for the tab bar to finish booting (past the 'Loading...' race).
    log.info("[%s] waiting for tab bar...", caid)
    _wait_tabs_ready(page, caid, timeout=30000)

    # Dump all tabs to the log so we can see exactly what SF rendered.
    try:
        tabs_info = page.evaluate("""
            () => Array.from(document.querySelectorAll(
                'a[role="tab"], .slds-tabs_default__link, li.slds-tabs_default__item a'
            )).map(t => ({
                text:     t.textContent.trim(),
                label:    t.getAttribute('data-label') || '',
                selected: t.getAttribute('aria-selected') || '',
            }))
        """)
        log.info("[%s] tabs found: %s", caid, tabs_info)
    except Exception:
        pass

    # ── Primary: click via JavaScript ───────────────────────────────────────
    # JS can traverse the actual DOM regardless of which attributes SF used.
    try:
        js_result = page.evaluate("""
            () => {
                const candidates = document.querySelectorAll(
                    'a[role="tab"], .slds-tabs_default__link, li.slds-tabs_default__item a'
                );
                for (const t of candidates) {
                    const text  = t.textContent.trim().toLowerCase();
                    const label = (t.getAttribute('data-label') || '').toLowerCase();
                    if (text === 'assets' || label === 'assets') {
                        if (t.getAttribute('aria-selected') === 'true') {
                            return 'already-selected';
                        }
                        t.click();
                        return 'clicked';
                    }
                }
                return 'not-found';
            }
        """)
        log.info("[%s] JS Assets tab click: %s", caid, js_result)
    except Exception as exc:
        js_result = "error"
        log.warning("[%s] JS tab click error: %s", caid, exc)

    if js_result in ("clicked", "already-selected"):
        time.sleep(2)
        for ready_sel in [
            "button[title='Filter']",
            "button:has-text('Expand All')",
            "button:has-text('Collapse All')",
            "tr[role='row']",
        ]:
            try:
                page.locator(ready_sel).first.wait_for(state="visible", timeout=10000)
                log.info("[%s] Assets grid ready (found %r)", caid, ready_sel)
                break
            except Exception:
                pass
        return

    # ── Fallback A: Playwright locators (direct tab bar) ────────────────────
    log.info("[%s] JS tab click returned %r — trying Playwright locators", caid, js_result)
    found = False
    for sel in ["a[data-label='Assets']", "a[title='Assets']", ".slds-tabs_default__link"]:
        try:
            loc = page.locator(sel)
            for idx in range(loc.count()):
                item = loc.nth(idx)
                label = (
                    item.get_attribute("data-label")
                    or item.get_attribute("title")
                    or (item.text_content() or "")
                ).strip().lower()
                if label == "assets":
                    if item.get_attribute("aria-selected") != "true":
                        item.click()
                        log.info("[%s] clicked Assets tab via Playwright (%r)", caid, sel)
                    else:
                        log.info("[%s] Assets tab already active (Playwright)", caid)
                    found = True
                    break
        except Exception:
            pass
        if found:
            break

    if found:
        time.sleep(2)
        for ready_sel in ["button[title='Filter']", "button:has-text('Expand All')", "tr[role='row']"]:
            try:
                page.locator(ready_sel).first.wait_for(state="visible", timeout=10000)
                log.info("[%s] Assets grid ready (found %r)", caid, ready_sel)
                break
            except Exception:
                pass
        return

    # ── Fallback B: 'More' overflow dropdown ────────────────────────────────
    log.info("[%s] Assets not in direct tab bar; trying 'More' overflow", caid)
    more_btn = _find_visible(page, [
        "li.slds-tabs_default__overflow-button button",
        "button[title='More']",
        "li[role='presentation'] button[aria-haspopup='true']",
    ], timeout=5000)

    if more_btn:
        more_btn.click()
        time.sleep(0.5)
        try:
            overflow_result = page.evaluate("""
                () => {
                    const items = document.querySelectorAll(
                        'lightning-menu-item, [role="menuitem"], .slds-dropdown a');
                    for (const it of items) {
                        if ((it.textContent || '').trim().toLowerCase() === 'assets') {
                            it.click();
                            return 'clicked';
                        }
                    }
                    return 'not-found';
                }
            """)
            log.info("[%s] overflow menu JS click: %s", caid, overflow_result)
            if overflow_result == "clicked":
                time.sleep(2)
                return
        except Exception as exc:
            log.warning("[%s] overflow JS error: %s", caid, exc)

    log.warning("[%s] Assets tab not found in tab bar or overflow — staying on current tab", caid)


# Filter elements on the Assets tab (exact live DOM):
#
#   button: <button class="slds-button slds-button_icon slds-button_icon-brand"
#           title="Filter" type="button">          <- icon-BRAND, title=Filter
#
#   The button is a generic Lightning icon-button: ~39 HIDDEN copies exist
#   in the DOM because the Console keeps background tabs' DOM alive.  Live
#   XPath of the real one:
#     //*[@id="tab-75"]/slot/flexipage-component2/slot/
#       c-asset-hierarchy-l-w-c/div/lightning-card/article/div[2]/slot/
#       div[1]/div[4]/lightning-button-icon/button
#   "tab-75" is a session-generated Console tab id (changes per tab) so it
#   must NOT be hardcoded.  The stable unique anchor from that path is the
#   c-asset-hierarchy-l-w-c component + :visible (only the ACTIVE Console
#   tab's button is visible; background copies are hidden).
#
#   panel:  opens with an <a>Clear All Filters</a> link at the top, a
#           "Status" checkbox group (<input type="checkbox" name="Status"
#           value="Obsolete|Installed|Registered">), a "Business Unit" group,
#           and Cancel / Save buttons at the bottom.
FILTER_BUTTON_SELECTOR = (
    "c-asset-hierarchy-l-w-c lightning-button-icon "
    "button[title='Filter']:visible")

# "Clear All Filters" link at the top of the filter dialog.  Live XPath:
#   //*[@id="tab-10"]/slot/flexipage-component2/slot/c-asset-hierarchy-l-w-c/
#     div/lightning-card/article/div[2]/slot/lightning-layout/slot/
#     lightning-layout-item[2]/slot/div[1]/div[1]/a
# (tab id was tab-75 in one session and tab-10 in the next — confirmed
#  dynamic, never hardcode it.)  Anchored the same way as the button:
#  inside the visible c-asset-hierarchy-l-w-c of the active Console tab.
FILTER_CLEAR_ALL_SELECTOR = (
    "c-asset-hierarchy-l-w-c a:has-text('Clear All Filters'):visible")
FILTER_PANEL_SELECTOR = (
    "c-asset-hierarchy-l-w-c a:has-text('Clear All Filters'):visible")

def _clear_status_filter(page, caid):
    """
    FILTER STEP 1  click the Filter icon button
    FILTER STEP 2  wait for the filter panel ('Clear All Filters' visible)
    FILTER STEP 3  click 'Clear All Filters' (resets every filter at once)
    FILTER STEP 5  click Save (straight after Clear All — no step 4)
    FILTER STEP 6  click the Filter icon again to dismiss the panel — it
                   stays open after Save and covers the grid
    """
    # ── FILTER STEP 1: click the Filter button ──────────────────────────
    filter_btn = page.locator(FILTER_BUTTON_SELECTOR).first
    try:
        filter_btn.wait_for(state="visible", timeout=10000)
    except Exception:
        log.info("[%s] FILTER STEP 1 SKIPPED — Filter button not visible (url=%s)",
                 caid, page.url)
        return
    filter_btn.click()
    log.info("[%s] FILTER STEP 1 OK — Filter button clicked", caid)
    time.sleep(STEP_PAUSE)

    # ── FILTER STEP 2: wait for the filter panel ────────────────────────
    try:
        page.locator(FILTER_PANEL_SELECTOR).first.wait_for(
            state="visible", timeout=10000)
    except Exception:
        log.warning("[%s] FILTER STEP 2 FAILED — filter panel did not open", caid)
        return
    log.info("[%s] FILTER STEP 2 OK — filter panel open", caid)
    time.sleep(STEP_PAUSE)

    # ── FILTER STEP 3: click 'Clear All Filters' ─────────────────────────
    # One click resets every filter (Status, Business Unit, dates, names)
    # instead of unchecking boxes one by one.
    try:
        page.locator(FILTER_CLEAR_ALL_SELECTOR).first.click(timeout=8000)
    except Exception as exc:
        log.warning("[%s] FILTER STEP 3 FAILED — could not click "
                    "'Clear All Filters': %s", caid, exc)
        return
    log.info("[%s] FILTER STEP 3 OK — 'Clear All Filters' clicked", caid)
    time.sleep(STEP_PAUSE)

    # ── FILTER STEP 5: Save ──────────────────────────────────────────────
    save_btn = page.locator("button.slds-button_brand:has-text('Save')").first
    try:
        save_btn.wait_for(state="visible", timeout=8000)
        save_btn.click()
    except Exception:
        log.warning("[%s] FILTER STEP 5 FAILED — Save button not found", caid)
        return
    log.info("[%s] FILTER STEP 5 OK — filter saved", caid)
    time.sleep(STEP_PAUSE)   # list needs to refresh

    # ── FILTER STEP 6: dismiss the panel (it covers the grid) ───────────
    try:
        page.locator(FILTER_BUTTON_SELECTOR).first.click(timeout=8000)
    except Exception as exc:
        log.warning("[%s] FILTER STEP 6 FAILED — could not click Filter "
                    "button to dismiss the panel: %s", caid, exc)
        return
    # confirm the panel is really gone before touching the grid
    try:
        page.locator(FILTER_PANEL_SELECTOR).first.wait_for(
            state="hidden", timeout=8000)
        log.info("[%s] FILTER STEP 6 OK — filter panel dismissed", caid)
    except Exception:
        log.warning("[%s] FILTER STEP 6 WARNING — panel still visible after "
                    "second Filter click", caid)
    time.sleep(STEP_PAUSE)


def _expand_all(page, caid):
    """Click 'Expand All' in the Asset Hierarchy grid so nested asset rows
    (aria-level 2+) are rendered and can be matched against PRODUCT_NAME."""
    btn = page.locator(
        "c-asset-hierarchy-l-w-c button.slds-button_neutral"
        ":has-text('Expand All'):visible").first
    try:
        btn.wait_for(state="visible", timeout=6000)
        btn.click()
        log.info("[%s] Expand All clicked", caid)
        time.sleep(STEP_PAUSE)
    except Exception:
        log.info("[%s] Expand All not found — list may already be flat", caid)


def _parse_price(val):
    """Normalize a price to float: '$1,360.00' / '118.16' / ' 118.16 '
    -> 1360.0 / 118.16.  Returns None when not parseable/empty."""
    s = str(val or "").replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


# Visible Asset Hierarchy rows.  ':visible' keeps us on the ACTIVE Console
# tab (background tabs keep hidden copies of the grid alive).  Playwright
# locators pierce open shadow roots, so this reaches the rows inside the
# <c-asset-hierarchy-l-w-c> LWC — something document.querySelectorAll cannot
# do (that is why the old in-page STEP 10 scan saw zero rows).
ASSET_ROW_SELECTOR = (
    "c-asset-hierarchy-l-w-c table[role='treegrid'] tbody "
    "tr[role='row']:visible")


def _norm_name(s):
    """Lowercase and drop commas — same as the old in-page norm()."""
    return re.sub(",", "", (s or "")).lower()


def _name_tokens(s):
    """Word tokens (alphanumeric runs) of a normalized name."""
    return [t for t in re.split(r"[^a-z0-9]+", _norm_name(s)) if t]


def _score_name(want_name, want_tokens, want_aliases, name):
    """Score one grid Asset Name against the wanted PRODUCT_NAME:
      5 known alias, 4 exact, 3 same words any order, 2 subset/substring,
      1 fuzzy (>=70%).  0 means no match.  want_name is already normalized,
      want_tokens is a set, want_aliases is a set of normalized alias names
      (from PRODUCT_ALIASES) that count as a definite match.
    """
    name_norm = _norm_name(name).strip()
    name_tokens = set(_name_tokens(name))
    if name_norm in want_aliases:
        return 5
    if name_norm == want_name:
        return 4
    common = len(want_tokens & name_tokens)
    same_set = common == len(want_tokens) and common == len(name_tokens)
    subset  = common == len(want_tokens) or common == len(name_tokens)
    substr  = bool(want_name) and (want_name in name_norm
                                   or name_norm in want_name)
    fuzzy   = common / max(len(want_tokens), len(name_tokens), 1) >= 0.7
    if same_set:
        return 3
    if subset or substr:
        return 2
    if fuzzy:
        return 1
    return 0


def _scan_asset_rows(page, caid, product_name, want_price):
    """Read every visible Asset Hierarchy row with Playwright locators and
    score each Asset Name against PRODUCT_NAME here in Python.  Returns a
    dict shaped like the old in-page scanner:
      {match, matchType, nCandidates, rowCount, seen}
    where match is the best candidate (or None).

    Playwright locators pierce the LWC shadow DOM, so this succeeds where
    the old page.evaluate(document.querySelectorAll) found nothing.  Each row
    read is logged so you can watch the Asset Name / Status / Price values
    come through.
    """
    want_name    = _norm_name(product_name).strip()
    want_tokens  = set(_name_tokens(product_name))
    want_aliases = {_norm_name(a).strip()
                    for a in PRODUCT_ALIASES.get(want_name, [])}
    if want_aliases:
        log.info("[%s] product %r has SF alias(es): %s",
                 caid, product_name, sorted(want_aliases))

    rows = page.locator(ASSET_ROW_SELECTOR)
    try:
        n = rows.count()
    except Exception:
        n = 0
    log.info("[%s] STEP 10 scan — %d visible asset row(s) found", caid, n)

    seen = []
    candidates = []
    for idx in range(n):
        row = rows.nth(idx)
        link = row.locator(
            "th[data-label='Asset Name'] lightning-formatted-url a").first
        try:
            if not link.count():
                log.info("[%s] STEP 10 row %d — no Asset Name link, skipping",
                         caid, idx)
                continue
            name = (link.text_content() or "").strip()
        except Exception as exc:
            log.info("[%s] STEP 10 row %d — read error: %s", caid, idx, exc)
            continue
        if not name:
            continue
        seen.append(name)

        # Status: data-cell-value='Installed' (falls back to visible text)
        status = ""
        std = row.locator("td[data-label='Status']").first
        try:
            if std.count():
                status = (std.get_attribute("data-cell-value")
                          or (std.text_content() or "").strip())
        except Exception:
            pass

        # Total Price: data-cell-value='118.16' (visible text is '$118.16')
        grid_price = None
        ptd = row.locator("td[data-label='Total Price']").first
        try:
            if ptd.count():
                raw = (ptd.get_attribute("data-cell-value")
                       or (ptd.text_content() or "").strip())
                grid_price = _parse_price(raw)
        except Exception:
            pass

        # Install Date / Bill Through Date: the visible text is a formatted
        # date like '8/31/2025' (data-cell-value carries the ISO '2025-08-31').
        # Prefer the visible text, fall back to the ISO attribute.
        def _date_cell(label):
            td = row.locator(f"td[data-label='{label}']").first
            try:
                if td.count():
                    return ((td.text_content() or "").strip()
                            or td.get_attribute("data-cell-value") or "")
            except Exception:
                pass
            return ""
        install_date = _date_cell("Install Date")
        bill_through = _date_cell("Bill Through Date")

        score = _score_name(want_name, want_tokens, want_aliases, name)
        log.info("[%s] STEP 10 row %d — name=%r status=%r price=%r "
                 "install=%r billThrough=%r score=%d",
                 caid, idx, name, status, grid_price,
                 install_date, bill_through, score)
        if not score:
            continue

        price_ok = (want_price is not None and grid_price is not None
                    and abs(grid_price - want_price) <= PRICE_TOLERANCE)

        candidates.append({"name": name, "status": status, "score": score,
                           "priceOk": price_ok, "gridPrice": grid_price,
                           "installDate": install_date,
                           "billThrough": bill_through})

    # Best candidate: price confirmation wins, then name score, then order.
    candidates.sort(key=lambda c: (c["priceOk"], c["score"]), reverse=True)
    best = candidates[0] if candidates else None
    types = {5: "alias", 4: "exact", 3: "tokens", 2: "partial", 1: "fuzzy"}
    return {"match": best,
            "matchType": types[best["score"]] if best else "none",
            "nCandidates": len(candidates),
            "rowCount": n,
            "seen": seen[:30]}


def _find_asset_status(page, caid, product_name, total_price=""):
    """
    STEP 9   wait for the Asset Hierarchy grid rows
    STEP 10  find the row whose Asset Name (first column) matches
             PRODUCT_NAME, confirm with ASSET_TOTAL_PRICE, read Status

    Exact live DOM per row (tr[role='row'] in table[role='treegrid']):
      - Asset Name:  th[data-label='Asset Name'] > lightning-formatted-url
        > <a>  -> the <a> TEXT is the asset name (e.g. 'Target')
      - Status:      td[data-label='Status'] with data-cell-value='Installed'
      - Total Price: td[data-label='Total Price'] with data-cell-value='118.16'
        (displayed as '$118.16' — both sides are normalized to numbers)

    Name matching, case-insensitive, strictest-to-loosest:
      exact   -> identical names
      tokens  -> same WORDS in any order
      partial -> one side's words a subset of the other's, or substring
      fuzzy   -> >= 70% of the words in common ('Customer Texting 3,000 MMS
                 Texts' vs 'Customer Texting SMS 3,000 Texts': 4 of 5 words
                 shared -> matches)
    The CSV price breaks ties between candidates and confirms the pick
    (within PRICE_TOLERANCE).  Returns
    (matched_asset_name, status, matched_asset_price), or the
    SF_NOT_FOUND_VALUE triple when nothing matches.  The price is the SF grid
    Total Price of the matched row, formatted like '118.16'.
    """
    # ── STEP 9: wait for the grid rows ───────────────────────────────────
    try:
        page.locator(
            "c-asset-hierarchy-l-w-c table[role='treegrid'] tbody "
            "tr[role='row']:visible").first.wait_for(
                state="visible", timeout=15000)
    except Exception:
        log.warning("[%s] STEP 9 FAILED — no asset grid rows visible", caid)
        return (SF_NOT_FOUND_VALUE,) * 5
    log.info("[%s] STEP 9 OK — asset grid rows visible", caid)
    time.sleep(STEP_PAUSE)

    want_price = _parse_price(total_price)
    log.info("[%s] looking for product %r with price %r", caid, product_name, want_price)
    # ── STEP 10: match PRODUCT_NAME in the Asset Name column ────────────
    #    The Asset Hierarchy grid lives inside the shadow DOM of the
    #    <c-asset-hierarchy-l-w-c> LWC.  document.querySelectorAll cannot
    #    cross shadow-root boundaries, so the old page.evaluate() scan saw
    #    zero rows even though STEP 9 (Playwright locator) found them.  We
    #    now read the rows with Playwright locators — which pierce open
    #    shadow roots — and do the scoring in Python (_scan_asset_rows).
    result = None
    deadline = time.time() + 15   # grid may still be refreshing after Save
    while True:
        try:
            result = _scan_asset_rows(page, caid, product_name, want_price)
        except Exception as exc:
            log.warning("[%s] STEP 10 — row scan error: %s", caid, exc)
            result = None
        if result and result.get("match"):
            break
        if time.time() > deadline:
            break
        time.sleep(1)
    log.info("[%s] STEP 10 scan result: %s", caid, result)
    if not result or not result.get("match"):
        log.warning("[%s] STEP 10 FAILED — product %r not found in %s rows; "
                    "asset names seen: %s", caid, product_name,
                    (result or {}).get("rowCount"),
                    (result or {}).get("seen"))
        return (SF_NOT_FOUND_VALUE,) * 5

    best   = result["match"]
    name   = best["name"]
    status = best["status"] or SF_NOT_FOUND_VALUE
    grid_price = best.get("gridPrice")
    price_str  = f"{grid_price:.2f}" if grid_price is not None else ""
    install_date = best.get("installDate") or ""
    bill_through = best.get("billThrough") or ""

    if want_price is None:
        price_note = "no CSV price to confirm"
    elif best.get("priceOk"):
        price_note = (f"price CONFIRMED grid={best.get('gridPrice')} "
                      f"csv={want_price}")
    else:
        price_note = (f"price MISMATCH grid={best.get('gridPrice')} "
                      f"csv={want_price}")
        log.warning("[%s] STEP 10 — matched %r by name but %s",
                    caid, name, price_note)

    log.info("[%s] STEP 10 OK — %s match: asset %r -> Status %r (%s; "
             "%d candidate(s))", caid, result.get("matchType"), name, status,
             price_note, result.get("nCandidates", 0))
    log.info("[%s] STEP 10 dates — install=%r billThrough=%r",
             caid, install_date, bill_through)
    time.sleep(STEP_PAUSE)
    return name, status, price_str, install_date, bill_through


def lookup_sf(page, caid, product_name, total_price=""):
    """
    Full lookup for one CSV row.
    Returns (matched_asset_name, asset_status, asset_price, install_date,
    bill_through_date) for the CSV columns 'Asset Name Check 2',
    'Asset Status 2', 'SFX asset Check 2 Price', 'SFX Install Date 2' and
    'SFX Bill Through Date 2'.  total_price is the row's ASSET_TOTAL_PRICE,
    used to confirm the matched grid row.
    """
    nf = (SF_NOT_FOUND_VALUE,) * 5
    # The global search header exists on every Lightning page, so each row
    # searches from wherever the previous row left off — no reload needed.
    url_before = page.url
    if not _sf_global_search(page, caid):
        return nf

    sf_name = _open_account_from_results(page, caid, url_before)
    if not sf_name:
        return nf

    _navigate_to_assets_tab(page, caid)
    _clear_status_filter(page, caid)
    _expand_all(page, caid)          # expand tree hierarchy so all rows are visible
    return _find_asset_status(page, caid, product_name, total_price)


# ======================================================================
# ROW HELPERS
# ======================================================================
def row_is_done(row):
    """True if both output columns already have a value (including error values)."""
    return bool(
        (row.get(COL_SF_NAME) or "").strip()
        and (row.get(COL_SF_STATUS) or "").strip()
    )


# ======================================================================
# MAIN
# ======================================================================
def main():
    setup_logging()
    path = Path(CSV_PATH)
    if not path.exists():
        sys.exit(f"CSV not found: {CSV_PATH}")

    rows, fieldnames, delim = read_rows(path)

    if KEY_COLUMN not in fieldnames:
        log.error("Key column %r not in CSV. Headers: %s", KEY_COLUMN, fieldnames)
        sys.exit(f"'{KEY_COLUMN}' column not found — check the CSV headers.")
    if PRODUCT_COLUMN not in fieldnames:
        log.warning("Product column %r not in CSV — status lookup will match any row",
                    PRODUCT_COLUMN)

    for col in NEW_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
            for r in rows:
                r.setdefault(col, "")

    usable = sum(1 for r in rows if (r.get(KEY_COLUMN) or "").strip())
    log.info("%d rows total; %d have %s", len(rows), usable, KEY_COLUMN)

    # ── Phase 1: launch the installed Chrome browser with a CDP port for
    # SSO login so the same profile is reused for automation. ─────────────
    browser_path = get_chrome_executable()

    profile_abs = str(Path(PROFILE_DIR).resolve())

    proc = subprocess.Popen([
        browser_path,
        f"--user-data-dir={profile_abs}",
        f"--window-size={WINDOW['width']},{WINDOW['height']}",
        f"--remote-debugging-port={CDP_PORT}",
        SF_HOME_URL,
    ])

    input(
        "\n" + "=" * 60 + "\n"
        "  PHASE 1 — Log in to Salesforce\n"
        "  A browser window opened at Salesforce.\n"
        "  1. Sign in via SSO if prompted.\n"
        "  2. Navigate until the Salesforce home page is visible\n"
        "     (global search bar at the top).\n"
        "  3. Come back here and press Enter.\n"
        + "=" * 60 + "\n"
        "\n  Press Enter when ready: "
    )

    # ── Phase 2: attach to the SAME browser over CDP (no new window) ──────
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(
            f"http://127.0.0.1:{CDP_PORT}",
            timeout=30000,
        )
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = None
        for pg in ctx.pages:
            if "force.com" in pg.url or "salesforce" in pg.url:
                page = pg
                break
        if page is None:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()
        page = wait_for_login(page)

        total = len(rows)
        for i, row in enumerate(rows, 1):
            caid    = (row.get(KEY_COLUMN) or "").strip()
            product = (row.get(PRODUCT_COLUMN) or "").strip()
            price   = (row.get(PRICE_COLUMN) or "").strip()

            if not caid:
                log.info("[%d/%d] blank %s — skipping", i, total, KEY_COLUMN)
                continue

            if row_is_done(row):
                log.info("[%d/%d] %s already done — skipping", i, total, caid)
                continue

            log.info("[%d/%d] === %s | product: %r ===", i, total, caid, product)

            try:
                (sf_name, asset_status, asset_price,
                 install_date, bill_through) = lookup_sf(
                    page, caid, product, price)
            except Exception as exc:
                log.error("[%s] unexpected error: %s", caid, exc, exc_info=True)
                (sf_name, asset_status, asset_price,
                 install_date, bill_through) = (SF_NOT_FOUND_VALUE,) * 5

            row[COL_SF_NAME]    = sf_name
            row[COL_SF_STATUS]  = asset_status
            row[COL_SF_PRICE]   = asset_price
            row[COL_SF_INSTALL] = install_date
            row[COL_SF_BILLTHRU] = bill_through
            write_rows(path, rows, fieldnames, delim)
            log.info("[%s] saved — name=%r  status=%r  price=%r  "
                     "install=%r  billThrough=%r", caid, sf_name, asset_status,
                     asset_price, install_date, bill_through)

        browser.close()   # detach from CDP; the browser window stays open

    # Close the browser window we launched in Phase 1
    proc.terminate()
    proc.wait()

    done = sum(1 for r in rows
               if (r.get(COL_SF_STATUS) or "").strip() not in ("", SF_NOT_FOUND_VALUE))
    log.info("=== sf_audit complete — %d/%d rows with status ===", done, len(rows))


if __name__ == "__main__":
    main()
