"""
audit_automation.py  --  VinSolutions audit (Phases 1-4)

One browser, one login, all rows. For each row: open dealer search, select
the dealer, read status + name, go to CRM Admin Settings and pull values,
then Dealer Features and check the product against the enabled list (with a
CSV mapping fallback). Saves after every row; resumes if it crashes.

Logs every step to the console AND to audit_log.txt.

SETUP (once):  pip install playwright
               (no 'playwright install' needed - channel="chrome" drives
                the real Chrome already installed, not a bundled build)
RUN:           python audit_automation.py
"""

import csv
import re
import shutil
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
# When MAPPING_PFA_ID is blank, use this column's value instead (only if
# MAPPING_PFA_ID itself is empty - it never overrides a present value).
FALLBACK_KEY_COLUMN = "ASSET_PFA_ID"

LOOKUP_COLUMNS = [
    "VIN Account Status",
    "Vin Name",
    "ILM Status",
    "Full Crm Status",
    "Max Number of Users",
    "Desking",
    "AIS Status",
    "Rates & Residuals as Enabled",
    "Vin Feature Enabled",
    "Inventory",
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
# Meaning of a BARE COMMA LIST in "Dealer Feature ID(s)", e.g.
# "SVC-VINSCAN,SVC-VINATTACH" (no explicit AND/OR anywhere in the cell):
#   False -> OR  (any one of the codes enabled is enough)   <-- business rule
#   True  -> AND (every code must be enabled)
# Cells that spell out AND / OR are always honoured as written; this only
# decides what a comma on its own means.
MATCH_ALL_FEATURE_IDS = False

# Who decides "Vin Feature Enabled" when BOTH the mapping CSV and a hardcoded
# PRODUCT_RULES entry cover the same product:
#   True  -> the CSV wins; PRODUCT_RULES is only a fallback for products that
#            are NOT in the CSV. The file is the single source of truth.
#   False -> the old behaviour: PRODUCT_RULES overrides the CSV.
MAPPING_FILE_WINS = True

# The mapping CSV is now the source of truth, so running without it produces
# 100% wrong output ("Check manually" everywhere) instead of a few gaps.
# True = stop immediately with a clear error instead of auditing with no
# mapping. Set to False only if you deliberately want the old silent fallback.
REQUIRE_MAPPING_FILE = True

# ---- Reliability ------------------------------------------------------
# A dealer page that times out used to leave the row blank and move on, which
# is why rows came back as "Not audited" and needed a human check. Now each
# row is retried inside the pass, and the whole CSV gets extra sweeps at the
# end for whatever is still incomplete (transient timeouts usually clear on a
# later pass, once the session has settled).
ROW_ATTEMPTS = 2              # tries per row within a single pass
RETRY_BACKOFF_SECONDS = 3     # wait between tries, multiplied by the try number
FINAL_SWEEPS = 2              # extra passes over rows that are still not done
# Written into "VIN Account Status" when a row still fails after every retry,
# so validate_audit.py flags it as "Review" instead of leaving it silently
# blank as "Not audited". Set to "" to keep the old blank behaviour.
AUDIT_FAILED_VALUE = "Audit failed"

# Values written to the "Vin Feature Enabled" column:
FEATURE_ENABLED_VALUE = "Yes"           # the feature is enabled
FEATURE_NOT_FOUND_VALUE = "Not found"   # checked, but not enabled
FEATURE_CHECK_VALUE = "Check manually"  # no rule and not found in the mapping CSV

# Hardcoded per-product rules. The FIRST rule whose "match" text appears in
# PRODUCT_NAME wins. With MAPPING_FILE_WINS these are only a FALLBACK for
# products missing from the mapping CSV - UNLESS the rule sets
# "override_mapping": True, which makes it a documented business exception
# that beats the CSV. Kinds:
#   columns     -> decided from settings already read (all must equal value)
#   min_numeric -> a settings value must be greater than the given number
#   codes       -> decided from the Dealer Features list (mode AND or OR)
# A rule may combine "columns" and "min_numeric" (all conditions must hold).
# Matching is FIRST-WINS by substring, so list the most specific products
# first (e.g. "CRM/ILM Limited Users..." before the generic "CRM/ILM").
# If NO rule matches, the product is looked up by name in the mapping CSV and
# its "Dealer Feature ID(s)" expression is evaluated (see _determine_feature_
# enabled). Not in the mapping -> "Check manually".
PRODUCT_RULES = [
    # ----- Phase 4 additions (specific products first) -----
    # CRM/ILM Limited Users (10 Users Max): ILM on + CRM on + at least one
    # licensed user (Max Number of Users > 0).
    {"match": "CRM/ILM Limited Users (10 Users Max)",
     "columns": [("ILM Status", "Yes"), ("Full Crm Status", "Yes")],
     "min_numeric": [("Max Number of Users", 0)]},
    # Customer Texting Unlimited MMS Texts: enabled feature code on the
    # Dealer Features list.
    {"match": "Customer Texting Unlimited MMS Texts",
     "codes": ["SVC-TXTMMSUNLMTD"], "codes_mode": "OR"},
    # Automotive Marketing Platform (Direct Email Campaign).
    # BUSINESS EXCEPTION: the mapping CSV asks for
    #   SVC-TARGETELITEWMS AND SVC-TARGETPRO AND SVC-DESKING1
    # but only SVC-TARGETELITEWMS is actually required for this product to
    # count as enabled. "override_mapping" keeps this rule ahead of the CSV.
    {"match": "Automotive Marketing Platform powered by VinSolutions "
              "(Direct Email Campaign)",
     "codes": ["SVC-TARGETELITEWMS"], "codes_mode": "OR",
     "override_mapping": True},
    # ----- existing rules -----
    {"match": "CRM/ILM",
     "columns": [("Full Crm Status", "Yes"), ("ILM Status", "Yes")]},
    {"match": "AIS CRM Integration",
     "columns": [("AIS Status", "Yes")]},
    {"match": "Desking",
     "columns": [("Desking", "VinDesking (New Desking Only)")]},
    {"match": "Rates and Residuals powered by DealerTrack",
     "columns": [("Rates & Residuals as Enabled", "Yes")]},
    {"match": "Vinessa",
     "codes": ["SVC-VINESSA", "SVC-VINESSA-BETA-PILOT"], "codes_mode": "OR"},
]

# ---- Mapping expression vocabulary -------------------------------------
# The mapping CSV "Dealer Feature ID(s)" column is a boolean expression whose
# tokens are either feature codes (SVC-*, MKT-*) or one of these STATE tokens,
# which are tested against the columns we already captured. Spacing varies in
# the data ("ILM Enabled" vs "ILMEnabled"), so we compare with spaces removed.
DESKING_ENABLED_TEXT = "VinDesking (New Desking Only)"
STATE_TOKENS = {
    "ILMEnabled": ("ILM Status", "yes"),
    "FullCRMEnabled": ("Full Crm Status", "yes"),
    "AISEnabled": ("AIS Status", "yes"),
    "RatesAndResidualsEnabled": ("Rates & Residuals as Enabled", "yes"),
    "DeskingAccessEnabled": ("Desking", "desking"),
    "MaxNumberOfUserGreaterThan0": ("Max Number of Users", "gt0"),
}
_STATE_NORM = {k.replace(" ", "").lower(): v for k, v in STATE_TOKENS.items()}

LOG_FILE = "audit_log.txt"
_MAPPING = []


def get_chrome_executable():
    """Return the installed Chrome executable for this machine."""
    candidates = [
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise FileNotFoundError(
        "Google Chrome was not found. Install Chrome or add it to PATH."
    )

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
        page.wait_for_timeout(2000)
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
    rr = ctx.query_selector("#MainContent_chkRREnabled")
    result["Rates & Residuals as Enabled"] = (
        "Yes" if (rr is not None and rr.is_checked()) else "No")
    result["Max Number of Users"] = (
        _value(ctx, "#ctl00_MainContent_m_txt_MaxNumberOfUsers")
        or _value(ctx, "#ctl00_MainContent_m_txt_MaxNumberOfUsers_ClientState")
    )
    result["Desking"] = _selected_text(ctx, "#MainContent_m_DeskingAccess")
    log.info("[%s] ILM=%s FullCRM=%s AIS=%s R&R=%s MaxUsers=%r Desking=%r", key,
             result["ILM Status"], result["Full Crm Status"],
             result["AIS Status"], result["Rates & Residuals as Enabled"],
             result["Max Number of Users"], result["Desking"])

    # ----- Phase 2: determine "Vin Feature Enabled" -----
    _determine_feature_enabled(page, key, row, result, selected_dealer)

    # ----- Phase 3: Vehicle Settings > Inventory Access -----
    result["Inventory"] = _read_inventory_access(page, key, selected_dealer)

    return result


def _determine_feature_enabled(page, key, row, result, selected_dealer):
    """Set result['Vin Feature Enabled'].

    With MAPPING_FILE_WINS (the default):
      1. Look the product up by name in the mapping CSV and evaluate its
         "Dealer Feature ID(s)" boolean expression. The file governs.
      2. Only if the product is NOT in the file, fall back to a hardcoded
         PRODUCT_RULES entry - settings columns, or explicit feature codes.
      3. Neither -> "Check manually".

    Expression tokens are either CRM-column STATE tokens (tested against the
    values we already read) or feature codes (tested against the Dealer
    Features enabled list). Operators: AND (all), OR (any); a bare comma list
    follows MATCH_ALL_FEATURE_IDS. Unparseable -> "Check manually".
    """
    product_name = (row.get("PRODUCT_NAME") or "").strip()
    rule = _match_rule(product_name)
    expr = _lookup_expression(product_name)

    # The CSV governs unless the product is missing from it, or a rule is
    # flagged as a documented business exception.
    exception = bool(rule and rule.get("override_mapping"))
    use_mapping = bool(expr) and (MAPPING_FILE_WINS or not rule) and not exception
    if exception:
        log.info("[%s] rule %r is a business EXCEPTION - overriding mapping "
                 "%r for %r", key, rule["match"], expr, product_name)
    if use_mapping:
        source = "mapping"
        if rule:
            log.info("[%s] mapping CSV overrides rule %r for %r",
                     key, rule["match"], product_name)
    elif not rule:
        result["Vin Feature Enabled"] = FEATURE_CHECK_VALUE
        log.info("[%s] no rule and no mapping for %r -> %s",
                 key, product_name, FEATURE_CHECK_VALUE)
        return

    # (a) hardcoded settings rule -> decide from columns already read; no page
    if not use_mapping and rule and ("columns" in rule or "min_numeric" in rule):
        col_checks = [(result.get(c, "") == v)
                      for c, v in rule.get("columns", [])]
        num_checks = [(_as_number(result.get(c, "")) > threshold)
                      for c, threshold in rule.get("min_numeric", [])]
        decided = all(col_checks) and all(num_checks)
        log.info("[%s] rule '%s' columns=%s numeric=%s -> %s", key,
                 rule["match"],
                 [(c, result.get(c, ""), "==", v)
                  for c, v in rule.get("columns", [])],
                 [(c, result.get(c, ""), ">", t)
                  for c, t in rule.get("min_numeric", [])],
                 decided)
        result["Vin Feature Enabled"] = (
            FEATURE_ENABLED_VALUE if decided else FEATURE_NOT_FOUND_VALUE)
        log.info("[%s] Vin Feature Enabled=%s (column/numeric rule)",
                 key, result["Vin Feature Enabled"])
        return

    # Not using the CSV -> build the expression from the hardcoded rule.
    if not use_mapping:
        if "codes" not in rule:
            result["Vin Feature Enabled"] = FEATURE_CHECK_VALUE
            log.warning("[%s] rule %r has no usable check for %r -> %s",
                        key, rule["match"], product_name, FEATURE_CHECK_VALUE)
            return
        joiner = " OR " if rule.get("codes_mode", "OR").upper() == "OR" else " AND "
        expr = joiner.join(rule["codes"])
        source = "rule '%s'" % rule["match"]

    mode, tokens = _parse_expression(expr)
    if mode is None or not tokens:
        result["Vin Feature Enabled"] = FEATURE_CHECK_VALUE
        log.warning("[%s] cannot parse expression %r (%s) -> %s",
                    key, expr, source, FEATURE_CHECK_VALUE)
        return

    # Open the Dealer Features page only if a token is a feature code.
    needs_features = any(not _is_state_token(t) for t in tokens)
    enabled_texts = (_read_enabled_features(page, key, selected_dealer)
                     if needs_features else [])

    decided = _eval_expression_tokens(mode, tokens, result, enabled_texts)
    log.info("[%s] expr=%r mode=%s tokens=%s (%s) -> %s",
             key, expr, mode, tokens, source, decided)
    result["Vin Feature Enabled"] = (
        FEATURE_ENABLED_VALUE if decided else FEATURE_NOT_FOUND_VALUE)
    log.info("[%s] Vin Feature Enabled=%s (%s)",
             key, result["Vin Feature Enabled"], source)


def _read_enabled_features(page, key, selected_dealer):
    """Open Admin > Selected Dealer > Dealer Features and return the list of
    enabled-feature texts (used to test feature codes)."""
    log.info("[%s] opening Dealer Features", key)
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
        return []
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
    return enabled_texts


def _parse_expression(expr):
    """Split a mapping expression into (mode, tokens).

    mode is 'AND' or 'OR'. An explicit AND / OR in the cell always wins. A
    BARE comma list (no AND/OR at all) means whatever MATCH_ALL_FEATURE_IDS
    says - by default OR, i.e. any one of the listed codes is enough. Mixed
    AND+OR is not supported -> returns (None, []) so the caller flags it for
    review.
    """
    e = (expr or "").strip()
    if not e:
        return None, []
    has_or = re.search(r"\bOR\b", e) is not None
    has_and = re.search(r"\bAND\b", e) is not None
    if has_or and has_and:
        return None, []
    if has_or:
        parts = re.split(r"\bOR\b|,", e)
        mode = "OR"
    elif has_and:
        parts = re.split(r"\bAND\b|,", e)
        mode = "AND"
    else:                                    # bare comma list, e.g. "A,B"
        parts = e.split(",")
        mode = "AND" if MATCH_ALL_FEATURE_IDS else "OR"
    tokens = [p.strip() for p in parts if p.strip()]
    return mode, tokens


def _is_state_token(token):
    return (token or "").strip().replace(" ", "").lower() in _STATE_NORM


def _eval_token(token, result, enabled_texts):
    """True/False for one token. A known STATE token is checked against the
    captured column; anything else is treated as a feature code."""
    spec = _STATE_NORM.get((token or "").strip().replace(" ", "").lower())
    if spec:
        col, test = spec
        val = result.get(col, "")
        if test == "yes":
            return val == "Yes"
        if test == "gt0":
            return _as_number(val) > 0
        if test == "desking":
            return val == DESKING_ENABLED_TEXT
    return _is_in_enabled(token, enabled_texts)


def _eval_expression_tokens(mode, tokens, result, enabled_texts):
    vals = [_eval_token(t, result, enabled_texts) for t in tokens]
    return all(vals) if mode == "AND" else any(vals)


def _read_inventory_access(page, key, selected_dealer):
    """Navigate Admin > Selected Dealer > Vehicle Settings > Inventory and read
    the inventory-access value. Logs the HTML so we can pin the exact element."""
    log.info("[%s] opening Vehicle Settings", key)
    try:
        page.click("#tab-admin")
        page.wait_for_timeout(800)
        page.wait_for_selector(selected_dealer, state="visible", timeout=10000)
        page.hover(selected_dealer)
        page.wait_for_timeout(600)
        veh = _find_first(page, [
            "#navigation-sub-menu-tab-admin-selected-dealer-vehicle-settings",
            '[data-menu-id="navigation-sub-menus-navigation-sub-menu-'
            'tab-admin-selected-dealer-vehicle-settings"]',
        ])
        if veh is None:
            log.error("[%s] Vehicle Settings not found", key)
            return ""
        try:
            veh.scroll_into_view_if_needed()
        except Exception:
            pass
        veh.click()
        page.wait_for_timeout(1500)
    except Exception as e:
        log.warning("[%s] vehicle settings nav error: %s", key, e)
        return ""

    # Click the Inventory tab link, then read the inventory-access input.
    inv_tab = "a[href*='/settings/Inventory/']"
    vctx = _ctx_with(page, inv_tab, total_wait=8000)
    if vctx is None:
        log.warning("[%s] Inventory tab link NOT found", key)
        return ""
    try:
        vctx.click(inv_tab)
    except Exception as e:
        log.warning("[%s] could not click Inventory tab: %s", key, e)
        return ""

    # Wait for the inventory-access input to appear, then read its value.
    input_sel = "#SettingTypeinventory-access-setting"
    ictx = _ctx_with(page, input_sel, total_wait=8000)
    if ictx is None:
        log.warning("[%s] inventory-access input NOT found", key)
        return ""

    el = ictx.query_selector(input_sel)
    value = ""
    if el is not None:
        try:
            value = (el.input_value() or "").strip()
        except Exception:
            value = (el.get_attribute("value") or "").strip()
    log.info("[%s] inventory value read: %r", key, value)
    return value


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


EXACT_MATCH_CODES = {"svc-vinscan", "svc-target"}

def _is_in_enabled(value, enabled_texts):
    v = str(value).strip().lower()
    if not v:
        return False
    if v in EXACT_MATCH_CODES:
        for t in enabled_texts:
            tl = t.strip().lower()
            if tl == v:
                return True
            if tl.startswith(v) and (len(tl) == len(v) or not tl[len(v)].isalnum()):
                return True
        return False
    return any(v == t.lower() or v in t.lower() for t in enabled_texts)


def _as_number(value):
    """Parse a settings value like '10' to a number; blank/non-numeric -> 0."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def load_mapping():
    """Load the CSV helper once: name -> raw 'Dealer Feature ID(s)' expression."""
    global _MAPPING
    path = Path(MAPPING_FILE)
    if not path.exists():
        msg = (f"mapping file not found: {MAPPING_FILE}\n"
               f"  looked in: {path.resolve().parent}\n"
               f"  Put {MAPPING_FILE} in the folder you run this script FROM\n"
               f"  (the same folder as {CSV_PATH}). Without it every product\n"
               f"  falls back to '{FEATURE_CHECK_VALUE}' and the audit is wrong.")
        if REQUIRE_MAPPING_FILE:
            log.error(msg)
            sys.exit(msg)
        log.warning("%s (fallback disabled)", msg)
        _MAPPING = []
        return
    data = []
    with open(path, newline="", encoding=_read_encoding(path)) as f:
        reader = csv.DictReader(f, delimiter=_detect_delimiter(path))
        cols = reader.fieldnames or []
        if NAME_COL not in cols or FEATURE_IDS_COL not in cols:
            log.error("mapping columns not found. Headers seen: %s", cols)
            _MAPPING = []
            return
        for r in reader:
            name = (r.get(NAME_COL) or "").strip()
            if not name:
                continue
            expr = (r.get(FEATURE_IDS_COL) or "").strip()
            data.append((name, expr))
    _MAPPING = data
    log.info("loaded %d mapping rows from %s", len(_MAPPING), MAPPING_FILE)

    # The same product listed twice with DIFFERENT expressions is a data bug:
    # _lookup_expression returns the first one in file order, so the result
    # depends on row order rather than on the rule. Surface it loudly.
    by_name = {}
    for name, expr in data:
        by_name.setdefault(name.strip().lower(), set()).add(expr.strip())
    for name, exprs in by_name.items():
        if len(exprs) > 1:
            log.warning("mapping conflict: %r has %d different expressions %s "
                        "- using the first one found in the file",
                        name, len(exprs), sorted(exprs))


def _lookup_expression(product_name):
    """Return the raw 'Dealer Feature ID(s)' expression for a product name:
    exact match first, then substring, then a close fuzzy match. '' if none."""
    if not product_name or not _MAPPING:
        return ""
    target = product_name.strip().lower()
    for name, expr in _MAPPING:
        if name.lower() == target:
            return expr
    for name, expr in _MAPPING:
        n = name.lower()
        if target in n or n in target:
            return expr
    names = [name for name, _ in _MAPPING]
    close = difflib.get_close_matches(product_name, names, n=1, cutoff=0.8)
    if close:
        for name, expr in _MAPPING:
            if name == close[0]:
                return expr
    return ""


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
    # Strip leading/trailing whitespace from every header name.
    # Excel sometimes saves headers as "ASSET_PFA_ID " (trailing space), which
    # causes row.get("ASSET_PFA_ID") to silently return None.
    fieldnames = [h.strip() for h in raw_fieldnames]
    if fieldnames != raw_fieldnames:
        renamed = {o: n for o, n in zip(raw_fieldnames, fieldnames) if o != n}
        log.warning("stripped whitespace from %d header(s): %s", len(renamed), renamed)
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


def get_key(row):
    """MAPPING_PFA_ID if present, else fall back to ASSET_PFA_ID."""
    key = (row.get(KEY_COLUMN) or "").strip()
    if key:
        return key
    return (row.get(FALLBACK_KEY_COLUMN) or "").strip()


def missing_columns(row):
    """Which LOOKUP_COLUMNS are still blank on this row."""
    return [c for c in LOOKUP_COLUMNS if not (row.get(c) or "").strip()]


def fill_row(page, row, attempts=None):
    """Fill one row, retrying the whole lookup when the page misbehaves.

    A single timeout used to blank the row for good; now we try again before
    giving up. Returns True when the row came back complete.
    """
    key = get_key(row)
    if not key:
        log.warning("row missing both %s and %s, skipping",
                    KEY_COLUMN, FALLBACK_KEY_COLUMN)
        return False

    attempts = attempts or ROW_ATTEMPTS
    last_error = None
    for attempt in range(1, attempts + 1):
        for system in SYSTEMS:
            try:
                found = system(page, key, row) or {}
                last_error = None
            except SystemExit:
                raise
            except Exception as e:
                last_error = e
                log.warning("[%s] attempt %d/%d failed: %s",
                            key, attempt, attempts, e)
                found = {}
            for col, val in found.items():
                if col in JUDGMENT_COLUMNS:
                    continue
                if col in row:
                    row[col] = val

        if row_is_done(row):
            if attempt > 1:
                log.info("[%s] recovered on attempt %d", key, attempt)
            return True
        if attempt < attempts:
            wait = RETRY_BACKOFF_SECONDS * attempt
            log.info("[%s] incomplete (missing %s), retrying in %ds",
                     key, missing_columns(row), wait)
            time.sleep(wait)

    if last_error:
        log.error("[%s] gave up after %d attempt(s): %s",
                  key, attempts, last_error)
    else:
        log.warning("[%s] still incomplete after %d attempt(s), missing %s",
                    key, attempts, missing_columns(row))
    return False


def report_incomplete(rows):
    """Mark and list every row that never came back complete, so no row is
    silently blank. Returns the rows still missing data."""
    nokey = [r for r in rows if not get_key(r)]
    if nokey:
        log.warning("%d row(s) have neither %s nor %s - never audited",
                    len(nokey), KEY_COLUMN, FALLBACK_KEY_COLUMN)

    stuck = [r for r in rows if get_key(r) and not row_is_done(r)]
    audited = len(rows) - len(nokey)
    if not stuck:
        log.info("COVERAGE: %d/%d rows with a key completed (100%%)",
                 audited, audited)
        return stuck

    pct = 100.0 * (audited - len(stuck)) / audited if audited else 0.0
    log.warning("COVERAGE: %d/%d rows completed (%.1f%%); %d still incomplete:",
                audited - len(stuck), audited, pct, len(stuck))
    for r in stuck:
        log.warning("   %s missing %s", get_key(r), missing_columns(r))
        if AUDIT_FAILED_VALUE and not (r.get("VIN Account Status") or "").strip():
            r["VIN Account Status"] = AUDIT_FAILED_VALUE
    return stuck


def main():
    setup_logging()
    log.info("=== audit run started ===")

    path = Path(CSV_PATH)
    if not path.exists():
        sys.exit(f"CSV not found: {CSV_PATH}")

    rows, fieldnames, delim = read_rows(path)

    if KEY_COLUMN not in fieldnames:
        log.error("Key column %r NOT in CSV. Columns are: %s",
                  KEY_COLUMN, fieldnames)
        sys.exit(f"'{KEY_COLUMN}' column not found - check the headers logged "
                 f"above (wrong delimiter or different column name?).")
    if FALLBACK_KEY_COLUMN not in fieldnames:
        log.warning("Fallback key column %r not in CSV; rows with blank %s "
                    "will be skipped instead of falling back",
                    FALLBACK_KEY_COLUMN, KEY_COLUMN)

    nonempty = sum(1 for r in rows if get_key(r))
    via_fallback = sum(1 for r in rows
                       if not (r.get(KEY_COLUMN) or "").strip() and get_key(r))
    log.info("%d rows total; %d have a usable key (%d via %s fallback); %d blank",
             len(rows), nonempty, via_fallback, FALLBACK_KEY_COLUMN,
             len(rows) - nonempty)

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
            channel="chrome",          # real Chrome, not bundled Chromium
            headless=HEADLESS,
            viewport=WINDOW,
            args=[f"--window-size={WINDOW['width']},{WINDOW['height']}"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        wait_for_login(page)

        total = len(rows)
        for i, row in enumerate(rows, 1):
            key = get_key(row)
            if row_is_done(row):
                log.info("[%d/%d] %s already done, skipping", i, total, key)
                continue
            log.info("[%d/%d] === processing %s ===", i, total, key)
            fill_row(page, row)
            write_rows(path, rows, fieldnames, delim)
            time.sleep(0.5)

        # Extra sweeps over whatever is still incomplete. A dealer page that
        # timed out mid-run very often loads fine on a later pass, so this is
        # what turns "Not audited" rows into real results.
        for sweep in range(1, FINAL_SWEEPS + 1):
            pending = [r for r in rows if get_key(r) and not row_is_done(r)]
            if not pending:
                break
            log.info("=== sweep %d/%d: retrying %d incomplete row(s) ===",
                     sweep, FINAL_SWEEPS, len(pending))
            for j, row in enumerate(pending, 1):
                log.info("[sweep %d - %d/%d] === %s ===",
                         sweep, j, len(pending), get_key(row))
                fill_row(page, row)
                write_rows(path, rows, fieldnames, delim)
                time.sleep(0.5)

        report_incomplete(rows)
        write_rows(path, rows, fieldnames, delim)
        ctx.close()
    log.info("=== audit run finished ===")


if __name__ == "__main__":
    main()
