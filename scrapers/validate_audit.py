"""
validate_audit.py  --  post-audit validations for audit.csv  (no browser)

Run this AFTER audit_automation.py has filled the Vin* columns. It reads
audit.csv and adds three columns, then writes the file back:

  Name Check   -> does ROOFTOP_ACCOUNT_NAME match the scraped "Vin Name"?
                  "OK", "Mismatch | CSV: ... | Vin: ...", or a note.
  Status Check -> does the SFX-scraped "Asset Status 2" match ASSET_SUBSTATUS?
                  "OK", "Mismatch | SFX: ... | CSV: ...", or a note. When they
                  disagree the substatus is not trusted and the row is flagged
                  "Review" instead of being classified.
  Category     -> billing category from ASSET_SUBSTATUS + Vin Feature Enabled
                  + VIN Account Status (only when Status Check confirms the
                  substatus).
  Comment      -> short plain-English reason for the category.

No login, no Playwright - it only uses columns already in the CSV, so it is
fast and safe to re-run any time. Tweak the rules / wording in the CONFIG block.

RUN:   python validate_audit.py
"""

import re
import csv
import sys
import difflib
import logging
from pathlib import Path

# ======================================================================
# CONFIG  (edit here)
# ======================================================================
CSV_PATH = "audit.csv"

# --- source columns (must already exist in audit.csv) ---
COL_ROOFTOP_NAME = "ROOFTOP_ACCOUNT_NAME"   # col H, from the CSV
COL_SUBSTATUS = "ASSET_SUBSTATUS"           # col Q, from the CSV
COL_SF_STATUS = "Asset Status 2"            # scraped by sf_audit.py (SF grid Status)
COL_VIN_NAME = "Vin Name"                   # scraped by audit_automation
COL_FEATURE = "Vin Feature Enabled"         # scraped (Yes / Not found / Check manually)
COL_ACCOUNT_STATUS = "VIN Account Status"   # scraped (Active / Inactive / In Setup ...)
COL_COAT = "COAT Status"                    # written by coat_audit.py (optional)

# sf_audit.py writes this into "Asset Status 2" when it found no matching asset.
SF_NOT_FOUND_VALUE = "SF not found"

# --- output columns (added if missing) ---
COL_NAME_CHECK = "Name Check"
COL_STATUS_CHECK = "Status Check"           # SFX Asset Status 2 vs ASSET_SUBSTATUS
COL_CATEGORY = "Category"
COL_NOTES = "Implementation Notes"
NEW_COLUMNS = [COL_NAME_CHECK, COL_STATUS_CHECK, COL_CATEGORY, COL_NOTES]

# --- Validation 1: name matching ---
# Names with a similarity (0..1) below this are flagged as a mismatch.
# Lower threshold = only VERY different names get flagged.
NAME_MATCH_THRESHOLD = 0.50
# Filler words ignored when comparing dealer names.
NAME_STOPWORDS = {"the", "of", "and", "inc", "llc", "co", "corp", "auto",
                  "automotive", "group", "motors", "motor", "dealership", "dba"}
# Alignment sanity-check: if this share (0..1) or more of the *comparable* names
# mismatch, warn that the columns may be out of alignment (e.g. a stray
# single-column sort in Excel detached Vin Name from its rows).
MISMATCH_ALERT_RATIO = 0.40
MISMATCH_ALERT_MIN = 5      # need at least this many comparable rows to bother

# --- Validation 2: category labels (rename wording here) ---
CATEGORY_NO_ISSUE = "No Billing Issue"
CATEGORY_UNDERBILLING = "Underbilling"      # used but not billed
CATEGORY_OVERBILLING = "Overbilling"        # billed but not used
CATEGORY_REVIEW = "Review"
CATEGORY_NOT_AUDITED = "Not audited"

# Account statuses treated as "live" (same as Active) for the billing rules.
# Anything containing "inactive" counts as inactive; anything else -> Review.
ACTIVE_STATUSES = {"active", "in setup"}

# Action phrases shown in Implementation Notes (edit wording here).
ACTION_UPDATE = "Update asset/feature if applicable"      # billing vs feature mismatch
ACTION_BOTH_OFF = "Update account & asset if applicable"  # not billed + feature off
ACTION_MATCH = "NA"
ACTION_REVIEW = "Review manually"

# "Enabled" means the feature column is exactly this (per your choice:
# anything else - Not found / Check manually / blank - counts as NOT enabled).
FEATURE_ENABLED_TEXT = "Yes"

log = logging.getLogger("validate")


def setup_logging():
    log.setLevel(logging.INFO)
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    log.handlers.clear()
    log.addHandler(h)


# ======================================================================
# VALIDATION 1 - name check
# ======================================================================
def _norm_name(s):
    """Lower-case, strip punctuation, drop filler words."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    toks = [t for t in s.split() if t and t not in NAME_STOPWORDS]
    return " ".join(toks)


def _name_similarity(a, b):
    """0..1 similarity, or None if either name is empty. Treats one name being
    contained in the other (abbreviations) and word reordering as a match."""
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return None
    if na == nb or na in nb or nb in na:
        return 1.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return max(ratio, overlap)


def _name_check(rooftop_name, vin_name):
    if not (vin_name or "").strip():
        return "No Vin name"
    sim = _name_similarity(rooftop_name, vin_name)
    if sim is None:
        return "No name to compare"
    if sim >= NAME_MATCH_THRESHOLD:
        return "OK"
    return f"Mismatch | CSV: {rooftop_name} | Vin: {vin_name}"


# ======================================================================
# VALIDATION 2 - SFX status vs CSV substatus
# ======================================================================
def _status_check(sf_status, substatus):
    """Compare the SFX-scraped 'Asset Status 2' against the CSV
    ASSET_SUBSTATUS.  Returns (verdict, matched) where matched is:
      True  -> the two statuses agree (case-insensitive) -> trust the substatus
      False -> they disagree                             -> flag for review
      None  -> cannot compare (one side missing / 'SF not found')
    """
    sf = (sf_status or "").strip()
    sub = (substatus or "").strip()
    if not sf or sf.lower() == SF_NOT_FOUND_VALUE.lower():
        return "No SFX status", None
    if not sub:
        return "No CSV substatus", None
    if sf.lower() == sub.lower():
        return "OK", True
    return f"Mismatch | SFX: {sf} | CSV: {sub}", False


# ======================================================================
# VALIDATION 3 - billing category
# ======================================================================
def _eq(value, target):
    return (value or "").strip().lower() == target.lower()


def _contains(value, sub):
    return sub.lower() in (value or "").strip().lower()


def _is_active(status):
    return (status or "").strip().lower() in ACTIVE_STATUSES


def _is_inactive(status):
    return "inactive" in (status or "").strip().lower()


def _classify(substatus, feature_enabled, account_status):
    """Returns (category, action_text).

    ACTIVE accounts (Active / In Setup) use the 4-case matrix. An INACTIVE
    account "wins": it voids a live feature, so Obsolete -> No Billing Issue
    and Installed -> Overbilling (still billing an inactive dealer). Anything
    else (odd status / substatus) -> Review.
    """
    obsolete = _eq(substatus, "Obsolete")
    installed = _eq(substatus, "Installed")
    enabled = _eq(feature_enabled, FEATURE_ENABLED_TEXT)   # only "Yes" counts
    active = _is_active(account_status)                    # Active or In Setup
    inactive = _is_inactive(account_status)
    not_enabled = not enabled

    if inactive:                                           # inactive voids feature
        if obsolete:
            return CATEGORY_NO_ISSUE, ACTION_BOTH_OFF
        if installed:
            return CATEGORY_OVERBILLING, ACTION_UPDATE     # billing an inactive dealer
        return CATEGORY_REVIEW, ACTION_REVIEW

    if active:
        if obsolete and not_enabled:
            return CATEGORY_NO_ISSUE, ACTION_BOTH_OFF      # not billed, not used
        if obsolete and enabled:
            return CATEGORY_UNDERBILLING, ACTION_UPDATE    # used, not billed
        if installed and not_enabled:
            return CATEGORY_OVERBILLING, ACTION_UPDATE     # billed, not used
        if installed and enabled:
            return CATEGORY_NO_ISSUE, ACTION_MATCH         # billed and used
    # Unexpected status / substatus.
    return CATEGORY_REVIEW, ACTION_REVIEW


def _feature_word(feature_enabled):
    """'enabled' / 'disabled' for the note (anything but 'Yes' = disabled)."""
    return "enabled" if _eq(feature_enabled, FEATURE_ENABLED_TEXT) else "disabled"


def _coat_suffix(coat_status):
    """Append ' | COAT not found' when the COAT audit found nothing."""
    c = (coat_status or "").strip().lower()
    return " | COAT not found" if c in ("coat no found", "coat not found") else ""


def _build_notes(substatus, feature_enabled, action, coat_status,
                 name_mismatch=False, vin_name=""):
    """Implementation Notes, e.g.:
    SFX asset as "Obsolete" | Vin feature as enabled | Comments: Update
    asset/feature if applicable - Different Name in Vin "JOE COOPER" | COAT not found

    The 'Different Name in Vin' part is only added when the names really differ.
    """
    feat = _feature_word(feature_enabled)
    name_part = f' - Different Name in Vin "{vin_name}"' if name_mismatch else ""
    return (f'SFX asset as "{substatus}" | Vin feature as {feat} '
            f'| Comments: {action}{name_part}{_coat_suffix(coat_status)}')


def validate_row(row):
    """Return (name_check, status_check, category, notes) for one CSV row."""
    name_check = _name_check(row.get(COL_ROOFTOP_NAME, ""),
                             row.get(COL_VIN_NAME, ""))
    substatus = row.get(COL_SUBSTATUS, "")
    sf_status = row.get(COL_SF_STATUS, "")

    # Gate: the SFX-scraped Asset Status 2 must agree with the CSV
    # ASSET_SUBSTATUS before we trust the substatus for the billing rules.
    # If they explicitly disagree, we don't classify — we flag for review.
    status_check, status_matched = _status_check(sf_status, substatus)
    if status_matched is False:
        notes = (f'SFX asset status "{(sf_status or "").strip()}" does not match '
                 f'CSV ASSET_SUBSTATUS "{(substatus or "").strip()}" '
                 f'| Comments: {ACTION_REVIEW}')
        return name_check, status_check, CATEGORY_REVIEW, notes

    status = (row.get(COL_ACCOUNT_STATUS, "") or "").strip()
    feature = (row.get(COL_FEATURE, "") or "").strip()
    # A row with no scraped result yet shouldn't be judged as underbilling.
    if not status and not feature:
        return (name_check, status_check, CATEGORY_NOT_AUDITED,
                "No VinSolutions data on this row yet.")
    cat, action = _classify(substatus, feature, status)
    name_mismatch = name_check.startswith("Mismatch")
    vin_name = (row.get(COL_VIN_NAME, "") or "").strip()
    notes = _build_notes(substatus, feature, action, row.get(COL_COAT, ""),
                         name_mismatch, vin_name)
    return name_check, status_check, cat, notes


# ======================================================================
# CSV ENGINE  (same encoding handling as audit_automation.py)
# ======================================================================
_ENC_CACHE = {}


def _read_encoding(path):
    """UTF-8 (with optional BOM) if the file decodes cleanly, else Windows-1252.
    Excel often re-saves CSVs as Windows-1252 (byte 0xA0 is not valid UTF-8)."""
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
    return rows, fieldnames, delim


def write_rows(path, rows, fieldnames, delimiter=","):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)


# ======================================================================
# MAIN
# ======================================================================
def main():
    setup_logging()
    path = Path(CSV_PATH)
    if not path.exists():
        sys.exit(f"CSV not found: {CSV_PATH}")

    rows, fieldnames, delim = read_rows(path)

    for col in [COL_ROOFTOP_NAME, COL_SUBSTATUS, COL_SF_STATUS, COL_VIN_NAME,
                COL_FEATURE, COL_ACCOUNT_STATUS]:
        if col not in fieldnames:
            log.warning("source column %r not found - its values will be blank", col)

    for col in NEW_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
            for r in rows:
                r.setdefault(col, "")

    counts, name_mismatches, name_comparable = {}, 0, 0
    status_mismatches, status_comparable = 0, 0
    for r in rows:
        nc, sc, cat, notes = validate_row(r)
        r[COL_NAME_CHECK] = nc
        r[COL_STATUS_CHECK] = sc
        r[COL_CATEGORY] = cat
        r[COL_NOTES] = notes
        counts[cat] = counts.get(cat, 0) + 1
        if nc == "OK" or nc.startswith("Mismatch"):   # both names present
            name_comparable += 1
        if nc.startswith("Mismatch"):
            name_mismatches += 1
        if sc == "OK" or sc.startswith("Mismatch"):   # both statuses present
            status_comparable += 1
        if sc.startswith("Mismatch"):
            status_mismatches += 1

    write_rows(path, rows, fieldnames, delim)

    log.info("validated %d rows", len(rows))
    log.info("category counts: %s", dict(sorted(counts.items())))
    log.info("name mismatches: %d of %d comparable", name_mismatches, name_comparable)
    log.info("status mismatches (SFX vs substatus): %d of %d comparable",
             status_mismatches, status_comparable)
    log.info("wrote columns %s back to %s", NEW_COLUMNS, CSV_PATH)

    # Alignment sanity-check: a high mismatch rate usually means the columns got
    # shuffled (e.g. a single-column sort in Excel), not that names really differ.
    if (name_comparable >= MISMATCH_ALERT_MIN
            and name_mismatches >= MISMATCH_ALERT_RATIO * name_comparable):
        pct = 100 * name_mismatches / name_comparable
        log.warning("ALIGNMENT CHECK: %.0f%% of compared names mismatch - that is "
                    "unusually high. The Vin Name column may be out of alignment "
                    "with the other columns (e.g. a single-column sort in Excel). "
                    "Verify the CSV before trusting these results.", pct)


if __name__ == "__main__":
    main()
