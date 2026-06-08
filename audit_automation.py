"""
audit_automation.py  --  VinSolutions audit (Phase 1 + Phase 2)

One browser, one login, all rows. For each row: open dealer search, select
the dealer, read status + name, go to CRM Admin Settings and pull values,
then Dealer Features and check the product against the enabled list (with a
CSV mapping fallback). Saves after every row; resumes if it crashes.

Logs every step to the console AND to audit_log.txt.

SETUP (once):  pip install playwright   then:  playwright install chromium
RUN:           python audit_automation.py
"""

import csv
import sys
import time
import random
import difflib
import logging
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
    "Vin Feature Enabled",
]
JUDGMENT_COLUMNS = []

PROFILE_DIR = "browser_profile"
HEADLESS = False
WINDOW = {"width": 1920, "height": 1080}
VIN_URL = "https://vinsolutions.app.coxautoinc.com/vinconnect"

# ---- CSV fallback mapping (PRODUCT_NAME -> Dealer Feature ID(s)) ----
MAPPING_FILE = "SFXvsFulfillmentAuditMapping.csv"
NAME_COL = "Name"
FEATURE_IDS_COL = "Dealer Feature ID(s)"
MATCH_ALL_FEATURE_IDS = True   # True = AND (all IDs must be enabled)
                               # False = OR (any one ID enabled is enough)

# Value written to the "Vin Feature Enabled" column:
FEATURE_ENABLED_VALUE = "Yes"        # when the feature is enabled
FEATURE_NOT_FOUND_VALUE = "Not found"  # when it is not

# Per-product rules. The FIRST rule whose "match" text appears in PRODUCT_NAME
# wins. Two kinds:
#   columns -> decided from settings already read (all must equal their value)
#   codes   -> decided from the Dealer Features list (mode AND or OR)
# If no rule matches, the default path runs: PRODUCTCODE direct, else the
# CSV mapping's Dealer Feature ID(s) with MATCH_ALL_FEATURE_IDS.
PRODUCT_RULES = [
    {"match": "CRM/ILM",
     "columns": [("Full Crm Status", "Yes"), ("ILM Status", "Yes")]},
    {"match": "AIS CRM Integration",
     "columns": [("AIS Status", "Yes")]},
    {"match": "Desking",
     "columns": [("Desking", "VinDesking (New Desking Only)")]},
    {"match": "Vinessa",
     "codes": ["SVC-VINESSA", "SVC-VINESSA-BETA-PILOT"], "codes_mode": "OR"},
]

LOG_FILE = "audit_log.txt"
_MAPPING = []

# ======================================================================
# LOGGING
# ======================================================================
log = logging.getLogger("audit")


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
# VINSOLUTIONS
# ======================================================================
def lookup_vinsolutions(page, key, row):
    result = {}

    log.info("[%s] navigating to VinConnect home", key)
    page.goto(VIN_URL, wait_until="domcontentloaded")

    # AUTH CHECK
    try:
        page.wait_for_selector("#ccrm-header-display-button",
                               state="visible", timeout=15000)
        log.info("[%s] authenticated (header visible)", key)
    except Exception:
        log.error("[%s] NOT authenticated - stopping", key)
        raise SystemExit("Not authenticated in VinSolutions. Log in, re-run.")

    # Open the dealer search modal
    log.info("[%s] opening dealer search modal", key)
    page.click("#ccrm-header-display-button")
    try:
        page.wait_for_selector("#ccrm-dealer-selector-modal-custom",
                               state="visible", timeout=15000)
        log.info("[%s] modal loaded", key)
    except Exception:
        log.warning("[%s] modal selector not seen; continuing", key)
    page.wait_for_selector("#dealer-selector-dealer-selector-input",
                           state="visible", timeout=15000)
    page.wait_for_timeout(1200)

    # Search; if nothing, dealer may be INACTIVE -> flip toggle, retry
    log.info("[%s] searching (active dealers)", key)
    if not _select_dealer(page, key):
        log.info("[%s] not found active; flipping 'Active Dealers Only'", key)
        if _toggle_active_dealers(page):
            log.info("[%s] toggle flipped; searching again", key)
            page.wait_for_timeout(1000)
        else:
            log.warning("[%s] could not flip the toggle", key)
        if not _select_dealer(page, key):
            log.error("[%s] dealer NOT FOUND (active or inactive)", key)
            return {}
    log.info("[%s] dealer selected", key)

    # Wait for the dealer header
    try:
        page.wait_for_selector("span.ccrm-dealer-header-display-title-name",
                               state="visible", timeout=10000)
    except Exception:
        page.wait_for_timeout(2000)

    # Status badge (no badge = Active) + name
    badge = page.query_selector("span[id^='dealer-status-badge']")
    badge_text = badge.inner_text().strip() if badge else ""
    result["VIN Account Status"] = badge_text if badge_text else "Active"
    name = page.query_selector("span.ccrm-dealer-header-display-title-name")
    if name:
        result["Vin Name"] = name.inner_text().strip()
    log.info("[%s] status=%r name=%r", key,
             result.get("VIN Account Status"), result.get("Vin Name"))

    # ----- Admin > Selected Dealer > CRM Admin Settings -----
    log.info("[%s] opening Admin > Selected Dealer", key)
    page.wait_for_selector("#tab-admin", state="visible", timeout=15000)
    page.click("#tab-admin")
    page.wait_for_timeout(800)

    selected_dealer = ('[data-menu-id="navigation-sub-menus-'
                       'navigation-sub-menu-tab-admin-selected-dealer"]')
    page.wait_for_selector(selected_dealer, state="visible", timeout=10000)
    page.hover(selected_dealer)
    page.wait_for_timeout(600)

    log.info("[%s] clicking CRM Admin Settings", key)
    if not _click_first(page, [
        "#navigation-sub-menu-tab-admin-selected-dealer-crm-admin-settings",
        '[data-menu-id="navigation-sub-menus-navigation-sub-menu-'
        'tab-admin-selected-dealer-crm-admin-settings"]',
    ]):
        log.error("[%s] could not click CRM Admin Settings", key)
        return result

    ctx = _ctx_with(page, "#MainContent__ILMEnabled")
    if ctx is None:
        log.error("[%s] CRM Admin Settings form NOT found", key)
        return result
    log.info("[%s] CRM Admin Settings form loaded%s", key,
             " (in iframe)" if ctx is not page else "")

    result["ILM Status"] = _checked(ctx, "#MainContent__ILMEnabled")
    result["Full Crm Status"] = _checked(ctx, "#MainContent__CRMEnabled")
    result["AIS Status"] = _checked(ctx, "#MainContent_m_AISEnabled")
    result["Max Number of Users"] = (
        _value(ctx, "#ctl00_MainContent_m_txt_MaxNumberOfUsers")
        or _value(ctx, "#ctl00_MainContent_m_txt_MaxNumberOfUsers_ClientState")
    )
    result["Desking"] = _selected_text(ctx, "#MainContent_m_DeskingAccess")
    log.info("[%s] ILM=%s FullCRM=%s AIS=%s MaxUsers=%r Desking=%r", key,
             result["ILM Status"], result["Full Crm Status"],
             result["AIS Status"], result["Max Number of Users"],
             result["Desking"])

    # ----- Phase 2: determine "Vin Feature Enabled" -----
    product_name = (row.get("PRODUCT_NAME") or "").strip()
    productcode = (row.get("PRODUCTCODE") or "").strip()
    rule = _match_rule(product_name)

    # (a) column rule -> decide from settings already read; no features page
    if rule and "columns" in rule:
        checks = [(result.get(c, "") == v) for c, v in rule["columns"]]
        decided = all(checks)
        log.info("[%s] rule '%s' columns=%s -> %s", key, rule["match"],
                 [(c, result.get(c, ""), v) for c, v in rule["columns"]],
                 decided)
        result["Vin Feature Enabled"] = (
            FEATURE_ENABLED_VALUE if decided else FEATURE_NOT_FOUND_VALUE)
        log.info("[%s] Vin Feature Enabled=%s (column rule)",
                 key, result["Vin Feature Enabled"])
        return result

    # (b) need the Dealer Features list (code rule or default mapping)
    log.info("[%s] opening Dealer Features (PRODUCTCODE=%r)", key, productcode)
    page.click("#tab-admin")
    page.wait_for_timeout(800)
    page.wait_for_selector(selected_dealer, state="visible", timeout=10000)
    page.hover(selected_dealer)
    page.wait_for_timeout(600)

    feat = _find_first(page, [
        "#navigation-sub-menu-tab-admin-selected-dealer-dealer-features",
        '[data-menu-id="navigation-sub-menus-navigation-sub-menu-'
        'tab-admin-selected-dealer-dealer-features"]',
    ])
    if feat is None:
        log.error("[%s] could not find Dealer Features", key)
        return result
    try:
        feat.scroll_into_view_if_needed()
    except Exception:
        pass
    feat.click()

    fctx = _ctx_with(page, "#ctl00_MainContent_rlEnabledFeatures")
    enabled_texts = []
    if fctx:
        spans = fctx.query_selector_all(
            "#ctl00_MainContent_rlEnabledFeatures li span")
        enabled_texts = [sp.inner_text().strip()
                         for sp in spans if sp.inner_text().strip()]
    log.info("[%s] enabled features found: %d", key, len(enabled_texts))

    matched = False
    if rule and "codes" in rule:                       # code rule (AND/OR)
        codes = rule["codes"]
        mode = rule.get("codes_mode", "OR").upper()
        checks = [_is_in_enabled(c, enabled_texts) for c in codes]
        matched = all(checks) if mode == "AND" else any(checks)
        log.info("[%s] rule '%s' codes=%s checks=%s mode=%s -> %s", key,
                 rule["match"], codes, checks, mode, matched)
    elif productcode and _is_in_enabled(productcode, enabled_texts):  # direct
        matched = True
        log.info("[%s] PRODUCTCODE matched directly", key)
    else:                                              # CSV mapping fallback
        feature_ids = _lookup_feature_ids(product_name)
        log.info("[%s] mapping name=%r -> ids=%s", key, product_name,
                 feature_ids)
        if feature_ids:
            checks = [_is_in_enabled(fid, enabled_texts) for fid in feature_ids]
            matched = all(checks) if MATCH_ALL_FEATURE_IDS else any(checks)
            log.info("[%s] id checks=%s mode=%s -> %s", key, checks,
                     "AND" if MATCH_ALL_FEATURE_IDS else "OR", matched)

    result["Vin Feature Enabled"] = (
        FEATURE_ENABLED_VALUE if matched else FEATURE_NOT_FOUND_VALUE)
    log.info("[%s] Vin Feature Enabled=%s", key, result["Vin Feature Enabled"])
    return result


def _toggle_active_dealers(page):
    """Flip the 'Active Dealers Only' switch and confirm aria-checked changed."""
    sw = page.query_selector("#dealer-selector-active-dealer-toggle")
    before = sw.get_attribute("aria-checked") if sw else None
    log.info("toggle: aria-checked before=%s", before)
    for sel in [
        ".ccrm-dealer-selector-ui-active-dealer-toggle .cx-switch__knob",
        ".ccrm-dealer-selector-ui-active-dealer-toggle",
        "#dealer-selector-active-dealer-toggle-label",
    ]:
        el = page.query_selector(sel)
        if not el:
            continue
        try:
            el.click()
        except Exception:
            continue
        page.wait_for_timeout(800)
        sw = page.query_selector("#dealer-selector-active-dealer-toggle")
        after = sw.get_attribute("aria-checked") if sw else None
        log.info("toggle: clicked %s -> aria-checked=%s", sel, after)
        if after != before:
            return True
    return False


def _match_rule(product_name):
    """Return the first PRODUCT_RULES entry whose match text is in the name."""
    pn = (product_name or "").lower()
    for rule in PRODUCT_RULES:
        if rule["match"].lower() in pn:
            return rule
    return None


def _is_in_enabled(value, enabled_texts):
    v = str(value).strip().lower()
    if not v:
        return False
    return any(v == t.lower() or v in t.lower() for t in enabled_texts)


def load_mapping():
    """Load the CSV helper once: name -> list of Dealer Feature IDs."""
    global _MAPPING
    path = Path(MAPPING_FILE)
    if not path.exists():
        log.warning("mapping file not found: %s (fallback disabled)", MAPPING_FILE)
        _MAPPING = []
        return
    data = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        if NAME_COL not in cols or FEATURE_IDS_COL not in cols:
            log.error("mapping columns not found. Headers seen: %s", cols)
            _MAPPING = []
            return
        for r in reader:
            name = (r.get(NAME_COL) or "").strip()
            if not name:
                continue
            ids = r.get(FEATURE_IDS_COL) or ""
            id_list = [x.strip() for x in str(ids).split(",") if x.strip()]
            data.append((name, id_list))
    _MAPPING = data
    log.info("loaded %d mapping rows from %s", len(_MAPPING), MAPPING_FILE)


def _lookup_feature_ids(product_name):
    if not product_name or not _MAPPING:
        return []
    target = product_name.strip().lower()
    for name, ids in _MAPPING:
        if name.lower() == target:
            return ids
    for name, ids in _MAPPING:
        n = name.lower()
        if target in n or n in target:
            return ids
    names = [name for name, _ in _MAPPING]
    close = difflib.get_close_matches(product_name, names, n=1, cutoff=0.8)
    if close:
        for name, ids in _MAPPING:
            if name == close[0]:
                return ids
    return []


def _find_first(page, selectors, timeout=4000):
    for sel in selectors:
        try:
            page.wait_for_selector(sel, state="visible", timeout=timeout)
            el = page.query_selector(sel)
            if el:
                return el
        except Exception:
            continue
    return None


def _click_first(page, selectors, timeout=4000):
    for sel in selectors:
        try:
            page.wait_for_selector(sel, state="visible", timeout=timeout)
            page.click(sel)
            return True
        except Exception:
            continue
    return False


def _human_type(element, text):
    for ch in str(text):
        element.type(ch, delay=random.randint(70, 170))


def _select_dealer(page, key):
    box = page.wait_for_selector("#dealer-selector-dealer-selector-input",
                                 state="visible", timeout=15000)
    box.click()
    box.fill("")
    _human_type(box, key)
    page.wait_for_timeout(2000)
    try:
        page.wait_for_selector(
            "#dealer-selector-table tr.ant-table-row-level-0",
            state="visible", timeout=6000)
    except Exception:
        log.info("  no result rows for %s", key)
        return False
    rows = page.query_selector_all(
        "#dealer-selector-table tr.ant-table-row-level-0")
    log.info("  %d result row(s)", len(rows))
    for row in rows:
        tds = row.query_selector_all("td")
        if tds and tds[0].inner_text().strip() == str(key).strip():
            btn = row.query_selector("button")
            if btn:
                btn.click()
                return True
    return False


def _ctx_with(page, selector, total_wait=15000):
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
        log.warning("  checkbox not found: %s", selector)
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
        log.warning("  select not found: %s", selector)
        return ""
    val = el.input_value()
    opt = ctx.query_selector(f"{selector} option[value='{val}']")
    return opt.inner_text().strip() if opt else ""


def wait_for_login(page):
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
            log.info("login confirmed")
            return
        except Exception:
            print("   -> Can't see VinSolutions yet. Finish logging in, "
                  "then type 'yes' again.")


SYSTEMS = [lookup_vinsolutions]

# ======================================================================
# ENGINE
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
    log.info("=== audit run started ===")

    path = Path(CSV_PATH)
    if not path.exists():
        sys.exit(f"CSV not found: {CSV_PATH}")

    rows, fieldnames = read_rows(path)
    for col in LOOKUP_COLUMNS + JUDGMENT_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
            for r in rows:
                r.setdefault(col, "")

    load_mapping()

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
            write_rows(path, rows, fieldnames)
            time.sleep(0.5)

        ctx.close()
    log.info("=== audit run finished ===")


if __name__ == "__main__":
    main()
