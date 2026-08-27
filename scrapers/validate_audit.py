"""
validate_audit.py  --  post-audit validations for audit.csv  (no browser)

Run this AFTER audit_automation.py has filled the Vin* columns. It reads
audit.csv and adds four columns, then writes the file back:

  Name Check   -> does ROOFTOP_ACCOUNT_NAME match the scraped "Vin Name"?
                  "OK", "Mismatch | CSV: ... | Vin: ...", or a note.
  Status Check -> does the SFX-scraped "Asset Status 2" match ASSET_SUBSTATUS?
                  "OK", "Mismatch | SFX: ... | CSV: ...", or a note. When they
                  disagree the substatus is not trusted and the row is flagged
                  "Review" instead of being classified.
  Category     -> billing category from ASSET_SUBSTATUS + Vin Feature Enabled
                  + VIN Account Status (only when Status Check confirms the
                  substatus).
  Implementation Notes -> the structured note described in the glossary.

The rules and wording follow "VIN Solutions - Audit Glossary". In particular:

  * VIN Account Status: "Active" and "In Setup" are both live; "Inactive" means
    the account is fully shut down (glossary section 1).
  * Category: Underbilling / Overbilling / No Billing Issue (section 2).
    "Review" and "Not audited" are operational states for rows we could not
    conclude on - they are not billing conclusions.
  * Implementation Notes are built from up to four " | "-separated segments,
    in this order (section 3 and the section 4 diagram):
        SFX asset as "<substatus>"
      | Vin feature as <enabled|disabled>
      | Different Name in Vin "<name>"      (only when the names differ)
      | Comments: <ranked findings>
      | COAT not found                      (only when COAT has no record)
    The name flag precedes the comment block, so the reader knows which dealer
    the finding is about before reading it. The COAT segment is always last.
  * Comment phrases come from the section 5 glossary and nothing else. When
    more than one applies they are joined most-important-first.
  * Hand-written analyst notes (section 6) are permanent: a row whose
    Implementation Notes value was not produced by this script is left alone.

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

# --- Validation 2: category labels (glossary section 2) ---
CATEGORY_NO_ISSUE = "No Billing Issue"
CATEGORY_UNDERBILLING = "Underbilling"      # used but not billed
CATEGORY_OVERBILLING = "Overbilling"        # billed but not used
# Not glossary categories - operational states for rows we cannot conclude on.
CATEGORY_REVIEW = "Review"
CATEGORY_NOT_AUDITED = "Not audited"

# Account statuses treated as "live" (same as Active) for the billing rules
# (glossary section 1: "In Setup" is treated the same as Active for billing).
# Anything containing "inactive" counts as inactive; anything else -> Review.
ACTIVE_STATUSES = {"active", "in setup"}

# --- ASSET_SUBSTATUS meaning (glossary section 3, part 1) ---
# "Installed"      -> the dealer is being billed
# "Pending Cancel" -> the dealer is being billed, but the asset is coming down
# "Obsolete"       -> the dealer is not being billed
BILLED_SUBSTATUSES = {"installed", "pending cancel"}
NOT_BILLED_SUBSTATUSES = {"obsolete"}

# --- Comment phrases (glossary section 5). Edit wording here. ---
COMMENT_UNABLE = ('Unable to access Vin: '
                  '"There was a problem loading the report"')
COMMENT_INACTIVE_BILLED = "IMPORTANT: Vin account fully inactive"
COMMENT_INACTIVE_NOT_BILLED = ("IMPORTANT: No billing issue, "
                               "Vin account fully inactive")
COMMENT_UPDATE = "Update asset/feature if applicable"
COMMENT_NONE = "No Comments"
# Not a glossary phrase - used for rows that fall outside every rule.
COMMENT_REVIEW = "Review manually"

# The comment block is ranked most-important to least-important (section 4
# diagram). Anything not listed sorts last, in the order it was added.
COMMENT_RANK = [
    COMMENT_UNABLE,
    COMMENT_INACTIVE_BILLED,
    COMMENT_INACTIVE_NOT_BILLED,
    COMMENT_REVIEW,
    COMMENT_UPDATE,
    COMMENT_NONE,
]
COMMENT_JOINER = "; "

# Text that means VinSolutions could not be reached at all. Matched
# case-insensitively against every scraped column.
VIN_UNREACHABLE_TEXT = "there was a problem loading the report"

# "Enabled" means the feature column is exactly this. Anything else -
# "Not found", "Check manually" or blank - is written as "disabled", because
# the glossary note only has the two words (section 3, part 2).
FEATURE_ENABLED_TEXT = "Yes"

# --- Manual exception notes (glossary section 6) ---
# A note this script did not write is an analyst's hand-written override and is
# treated as permanent: the row's Category and Implementation Notes are left
# exactly as they are. Set to False to overwrite everything on every run.
PRESERVE_MANUAL_NOTES = True
# Openings that identify a note as machine-generated (anything else is manual).
GENERATED_NOTE_PREFIXES = (
    'SFX asset as "',
    'SFX asset status "',
    "No VinSolutions data on this row yet.",
)

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


def _is_active(status):
    return (status or "").strip().lower() in ACTIVE_STATUSES


def _is_inactive(status):
    return "inactive" in (status or "").strip().lower()


def _is_billed(substatus):
    """True for "Installed" / "Pending Cancel" - the dealer is being billed."""
    return (substatus or "").strip().lower() in BILLED_SUBSTATUSES


def _is_not_billed(substatus):
    """True for "Obsolete" - the dealer is not being billed."""
    return (substatus or "").strip().lower() in NOT_BILLED_SUBSTATUSES


def _vin_unreachable(row):
    """True when any scraped column carries the Vin "problem loading the
    report" error, i.e. we could not read VinSolutions at all."""
    for col in (COL_ACCOUNT_STATUS, COL_VIN_NAME, COL_FEATURE):
        if VIN_UNREACHABLE_TEXT in (row.get(col) or "").strip().lower():
            return True
    return False


def _classify(substatus, feature_enabled, account_status):
    """Returns (category, [comment, ...]) using the glossary rules.

    ACTIVE accounts (Active / In Setup) use the 4-case billed/enabled matrix.
    An INACTIVE account "wins": the dealer has shut VinSolutions down, so
    not-billed is correct (No Billing Issue) and still-billed is Overbilling.
    When an inactive account still shows the feature enabled in Vin, the note
    carries the matching "IMPORTANT:" phrase. Anything else -> Review.
    """
    billed = _is_billed(substatus)
    not_billed = _is_not_billed(substatus)
    enabled = _eq(feature_enabled, FEATURE_ENABLED_TEXT)   # only "Yes" counts
    active = _is_active(account_status)                    # Active or In Setup
    inactive = _is_inactive(account_status)

    if inactive:                                           # inactive voids feature
        if not_billed:
            # Correctly not billing a dead account. The "IMPORTANT" phrase only
            # applies while the feature still shows enabled in Vin.
            return CATEGORY_NO_ISSUE, [COMMENT_INACTIVE_NOT_BILLED if enabled
                                       else COMMENT_NONE]
        if billed:
            # Still billing a dead account.
            return CATEGORY_OVERBILLING, [COMMENT_INACTIVE_BILLED if enabled
                                          else COMMENT_UPDATE]
        return CATEGORY_REVIEW, [COMMENT_REVIEW]

    if active:
        if not_billed and not enabled:
            return CATEGORY_NO_ISSUE, [COMMENT_NONE]       # not billed, not used
        if not_billed and enabled:
            return CATEGORY_UNDERBILLING, [COMMENT_UPDATE]  # used, not billed
        if billed and not enabled:
            return CATEGORY_OVERBILLING, [COMMENT_UPDATE]  # billed, not used
        if billed and enabled:
            return CATEGORY_NO_ISSUE, [COMMENT_NONE]       # billed and used
    # Unexpected status / substatus.
    return CATEGORY_REVIEW, [COMMENT_REVIEW]


def _feature_word(feature_enabled):
    """'enabled' / 'disabled' for the note (anything but 'Yes' = disabled)."""
    return "enabled" if _eq(feature_enabled, FEATURE_ENABLED_TEXT) else "disabled"


def _coat_missing(coat_status):
    """True when the COAT audit had no record for this account."""
    return (coat_status or "").strip().lower() in ("coat no found", "coat not found")


def _rank_comments(comments):
    """Order the comment block most-important to least-important, de-duped."""
    seen, unique = set(), []
    for c in comments:
        if c and c not in seen:
            seen.add(c)
            unique.append(c)

    def key(item):
        try:
            return (0, COMMENT_RANK.index(item))
        except ValueError:
            return (1, unique.index(item))

    return sorted(unique, key=key)


def _build_notes(lead, feature_enabled, comments, coat_status,
                 name_mismatch=False, vin_name=""):
    """Join the note segments with " | ", e.g.:

    SFX asset as "Obsolete" | Vin feature as enabled
    | Different Name in Vin "JOE COOPER" | Comments: Update asset/feature
    if applicable | COAT not found

    'lead' is the first segment. The name and COAT segments are optional.
    "Different Name in Vin" comes BEFORE the comment block so the reader knows
    which dealer the finding is about before reading the finding. COAT is
    always last (glossary section 4 diagram).
    """
    segments = [lead]
    if feature_enabled is not None:
        segments.append(f"Vin feature as {_feature_word(feature_enabled)}")
    if name_mismatch and vin_name:
        segments.append(f'Different Name in Vin "{vin_name}"')
    segments.append("Comments: " + COMMENT_JOINER.join(_rank_comments(comments)))
    if _coat_missing(coat_status):
        segments.append("COAT not found")
    return " | ".join(segments)


def _is_manual_note(note):
    """A note this script did not write is an analyst override (section 6)."""
    note = (note or "").strip()
    return bool(note) and not note.startswith(GENERATED_NOTE_PREFIXES)


def validate_row(row):
    """Return (name_check, status_check, category, notes) for one CSV row."""
    name_check = _name_check(row.get(COL_ROOFTOP_NAME, ""),
                             row.get(COL_VIN_NAME, ""))
    substatus = (row.get(COL_SUBSTATUS, "") or "").strip()
    sf_status = (row.get(COL_SF_STATUS, "") or "").strip()
    coat = row.get(COL_COAT, "")
    name_mismatch = name_check.startswith("Mismatch")
    vin_name = (row.get(COL_VIN_NAME, "") or "").strip()

    # Gate: the SFX-scraped Asset Status 2 must agree with the CSV
    # ASSET_SUBSTATUS before we trust the substatus for the billing rules.
    # If they explicitly disagree, we don't classify - we flag for review.
    status_check, status_matched = _status_check(sf_status, substatus)
    if status_matched is False:
        lead = (f'SFX asset status "{sf_status}" does not match '
                f'CSV ASSET_SUBSTATUS "{substatus}"')
        notes = _build_notes(lead, None, [COMMENT_REVIEW], coat,
                             name_mismatch, vin_name)
        return name_check, status_check, CATEGORY_REVIEW, notes

    status = (row.get(COL_ACCOUNT_STATUS, "") or "").strip()
    feature = (row.get(COL_FEATURE, "") or "").strip()
    lead = f'SFX asset as "{substatus}"'

    # Vin was unreachable - we have no feature state to judge against.
    if _vin_unreachable(row):
        notes = _build_notes(lead, feature, [COMMENT_UNABLE], coat,
                             name_mismatch, vin_name)
        return name_check, status_check, CATEGORY_REVIEW, notes

    # A row with no scraped result yet shouldn't be judged as underbilling.
    if not status and not feature:
        return (name_check, status_check, CATEGORY_NOT_AUDITED,
                "No VinSolutions data on this row yet.")

    cat, comments = _classify(substatus, feature, status)
    notes = _build_notes(lead, feature, comments, coat, name_mismatch, vin_name)
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
    manual_kept = 0
    for r in rows:
        nc, sc, cat, notes = validate_row(r)
        r[COL_NAME_CHECK] = nc
        r[COL_STATUS_CHECK] = sc
        # Glossary section 6: an analyst's hand-written note is permanent.
        if PRESERVE_MANUAL_NOTES and _is_manual_note(r.get(COL_NOTES)):
            manual_kept += 1
            cat = r.get(COL_CATEGORY) or cat
        else:
            r[COL_NOTES] = notes
        r[COL_CATEGORY] = cat
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
    if manual_kept:
        log.info("kept %d manual exception note(s) untouched", manual_kept)
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
