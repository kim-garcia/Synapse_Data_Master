"""
Synapse Data Master — core logic (shared by the script and the website).

This file does three jobs, kept separate on purpose:

  1. anonymize()        -> remove dealer names/addresses BEFORE anything is sent to Gemini
  2. build_gap_summary()-> turn 1,580 messy rows into a short, factual summary
  3. ask_gemini()       -> send that summary (never raw customer data) to Gemini

Nothing here talks to a browser or a website. That keeps the AI logic testable
on its own and reusable from both `diagnose.py` (command line) and `app.py` (website).
"""

from __future__ import annotations

import hashlib
import os
import pandas as pd

# --------------------------------------------------------------------------
# 1. ANONYMIZATION
# --------------------------------------------------------------------------
# Columns that identify a real dealership. These must NEVER leave your machine
# in an API call. We drop the free-text ones and replace the account IDs with a
# stable surrogate like "D0007" so a finding can still be traced back — locally.
PII_DROP = [
    "ULTIMATE_PARENT_NAME",
    "ROOFTOP_ACCOUNT_NAME",
    "ROOFTOP_ADDRESS",
    "ASSET_BILLTO_ROOFTOP_CA_NAME",
    "Asset Name Check 2",
]
PII_HASH = [
    "ULTIMATE_PARENT_CAID",
    "ROOFTOP_ACCOUNT_CAID",
    "ASSET_BILLTO_ROOFTOP_CAID",
]


# Product columns whose real names we can pseudonymize (PROD-001, PROD-002, ...).
PRODUCT_COLS = ["PRODUCT_NAME", "PRODUCTCODE"]


def _surrogate(value: str, salt: str = "") -> str:
    """Turn a real id into a short, non-reversible tag (same id+salt -> same tag).
    A `salt` ('algo extra') makes the code impossible to reverse without it."""
    if not value:
        return ""
    digest = hashlib.sha1((salt + value).encode("utf-8")).hexdigest()
    return "D" + str(int(digest, 16) % 100000).zfill(5)


def anonymize(df: pd.DataFrame, anonymize_products: bool = False,
              salt: str = "") -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Return (safe_df, lookup).

      safe_df -> no names/addresses; account ids replaced by surrogates. OK to send to Gemini.
      lookup  -> surrogate -> real account id/name. Stays on your machine so YOU can trace
                 a finding back to the real dealer. Never sent anywhere.

    anonymize_products=True -> also replace real product names/codes with PROD-001,
                               PROD-002, ... (keeps the grouping, hides the real product).
    salt='...'              -> extra secret mixed into the dealer code so it can't be
                               reversed back to the real CAID.
    """
    safe = df.copy()

    def surro(v):
        return _surrogate(v, salt)

    # Build the local lookup before we strip anything.
    lookup_cols = {}
    if "ROOFTOP_ACCOUNT_CAID" in safe.columns:
        lookup_cols["DEALER_REF"] = safe["ROOFTOP_ACCOUNT_CAID"].map(surro)
        lookup_cols["ROOFTOP_ACCOUNT_CAID"] = safe["ROOFTOP_ACCOUNT_CAID"]
    if "ROOFTOP_ACCOUNT_NAME" in safe.columns:
        lookup_cols["ROOFTOP_ACCOUNT_NAME"] = safe["ROOFTOP_ACCOUNT_NAME"]
    lookup = pd.DataFrame(lookup_cols).drop_duplicates()

    # Add the surrogate the LLM will see, then remove the real identifiers.
    # Idempotent: if the data is already anonymized (has DEALER_REF), replace it
    # instead of crashing on a duplicate insert.
    if "DEALER_REF" in safe.columns:
        safe = safe.drop(columns=["DEALER_REF"])
    if "ROOFTOP_ACCOUNT_CAID" in safe.columns:
        safe.insert(0, "DEALER_REF", safe["ROOFTOP_ACCOUNT_CAID"].map(surro))
    for col in PII_HASH:
        if col in safe.columns:
            safe[col] = safe[col].map(surro)
    safe = safe.drop(columns=[c for c in PII_DROP if c in safe.columns])

    # Optional: replace real product names/codes with stable pseudonyms.
    if anonymize_products:
        for col in PRODUCT_COLS:
            if col in safe.columns:
                uniq = {v: f"PROD-{i:03d}" for i, v in
                        enumerate(sorted(x for x in safe[col].unique() if x), 1)}
                safe[col] = safe[col].map(lambda v: uniq.get(v, ""))

    return safe, lookup


# --------------------------------------------------------------------------
# 1b. READABLE MASKING  (partial CAID + category-coded products)
# --------------------------------------------------------------------------
# Keyword -> category tag. First match wins. Used to turn a real product name
# into a code you can still understand, e.g. "TXT-042" for text messaging.
PRODUCT_CATEGORIES = [
    (("text", "mms", "sms", "messag"), "TXT"),
    (("crm", "ilm", "ais"), "CRM"),
    (("market", "amp", "email", "campaign", "target"), "MKT"),
    (("inventory", "idscan", "vinmobile", "scan"), "INV"),
    (("desking", "desk"), "DSK"),
    (("rate", "residual", "dealertrack"), "RATE"),
    (("vinessa",), "VSA"),
    (("insight", "predictive"), "INS"),
]


def mask_caid(caid: str, keep: int = 7) -> str:
    """Keep the first `keep` chars, mask the rest: CA11212546 -> CA11212XXX."""
    if not caid or len(caid) <= keep:
        return caid
    return caid[:keep] + "X" * (len(caid) - keep)


def product_code(name: str) -> str:
    """Readable masked product code that hints at the category, e.g. 'TXT-042'."""
    if not name:
        return ""
    low = name.lower()
    prefix = "GEN"
    for keys, tag in PRODUCT_CATEGORIES:
        if any(k in low for k in keys):
            prefix = tag
            break
    num = int(hashlib.sha1(name.encode("utf-8")).hexdigest(), 16) % 1000
    return f"{prefix}-{num:03d}"


def mask_dataframe(df: pd.DataFrame, keep: int = 7) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Readable-masked copy of the data (for sharing / demos of REAL scraped data):
      * names/addresses removed
      * CAIDs partially masked (CA11212XXX)
      * product names/codes turned into category codes (TXT-042)
    Returns (masked_df, product_legend) where product_legend maps code -> real
    product name so YOU can still read it. The legend stays on your machine.
    """
    safe = df.copy()
    legend = {}
    if "PRODUCT_NAME" in safe.columns:
        legend = {product_code(v): v for v in safe["PRODUCT_NAME"].unique() if v}

    for col in PII_HASH:  # the CAID columns
        if col in safe.columns:
            safe[col] = safe[col].map(lambda v: mask_caid(v, keep))
    for col in PRODUCT_COLS:
        if col in safe.columns:
            safe[col] = safe[col].map(product_code)
    safe = safe.drop(columns=[c for c in PII_DROP if c in safe.columns])
    if "DEALER_REF" in safe.columns:
        safe = safe.drop(columns=["DEALER_REF"])

    product_legend = pd.DataFrame(
        {"product_code": list(legend.keys()), "real_product_name": list(legend.values())}
    ).sort_values("product_code")
    return safe, product_legend


# --------------------------------------------------------------------------
# 2. SUMMARIZE THE GAPS  (facts only — this is what the AI reasons over)
# --------------------------------------------------------------------------
def _to_money(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0)


def build_gap_summary(df: pd.DataFrame) -> dict:
    """Aggregate the audit into a compact, factual dictionary (safe to show or send)."""
    out: dict = {"total_rows": int(len(df))}

    def counts(col):
        if col in df.columns:
            return df[col].replace("", "(blank)").value_counts().to_dict()
        return {}

    out["gap_type"] = counts("GAP_TYPE")
    out["implementation_status"] = counts("IMPLEMENTATION_STATUS")
    out["business_unit"] = counts("BUSINESS_UNIT__C")
    out["two_way_match"] = counts("TWOWAYMATCH")
    out["three_way_match"] = counts("THREEWAYMATCH")

    # Dollars at stake, split by gap direction.
    if "ASSET_TOTAL_PRICE" in df.columns and "GAP_TYPE" in df.columns:
        money = df.assign(_amt=_to_money(df["ASSET_TOTAL_PRICE"]))
        out["dollars_by_gap_type"] = (
            money.groupby("GAP_TYPE")["_amt"].sum().round(2).to_dict()
        )

    # Which products drive the most gaps.
    name_col = "PRODUCT_NAME" if "PRODUCT_NAME" in df.columns else "PRODUCTCODE"
    if name_col in df.columns:
        out["top_products_by_gap_count"] = (
            df[name_col].replace("", "(blank)").value_counts().head(10).to_dict()
        )

    return out


def summary_to_text(summary: dict) -> str:
    """Render the summary dict as readable lines for the prompt (and for humans)."""
    lines = [f"Total audited rows: {summary.get('total_rows', 0)}", ""]
    labels = {
        "gap_type": "Gap type",
        "implementation_status": "Implementation / fulfillment status",
        "dollars_by_gap_type": "Dollar amount by gap type (USD)",
        "top_products_by_gap_count": "Top products by number of gap rows",
        "two_way_match": "Two-way match",
        "three_way_match": "Three-way match",
    }
    for key, label in labels.items():
        block = summary.get(key)
        if block:
            lines.append(f"{label}:")
            for k, v in block.items():
                val = f"${v:,.2f}" if key == "dollars_by_gap_type" else f"{v}"
                lines.append(f"  - {k}: {val}")
            lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 3. GEMINI  (plain API key)
# --------------------------------------------------------------------------
# Uses the current Google GenAI SDK:  pip install google-genai
# Get a key at https://aistudio.google.com/app/apikey  and set it as an
# environment variable named GEMINI_API_KEY (the website also lets you paste it).
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

_SYSTEM = (
    "You are Synapse Data Master, a data-QA analyst for the 3WM team auditing "
    "VinSolutions dealer billing vs. fulfillment. You are given ONLY anonymized, "
    "aggregated audit figures — never real customer names. Dealers are referred to "
    "by surrogate tags like D01234. Be concrete, cite the numbers you were given, "
    "and never invent data you were not shown."
)


def _client(api_key: str | None = None):
    from google import genai  # imported here so the file loads even before install
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "No Gemini API key. Set GEMINI_API_KEY, or pass it in the website field."
        )
    return genai.Client(api_key=key)


def _generate(client, **kwargs):
    """Call Gemini, retrying ONLY on transient server overload (503/500).

    We deliberately do NOT retry 429 / RESOURCE_EXHAUSTED: that's a quota limit,
    and retrying only burns the quota faster without helping."""
    import time
    transient = ("503", "unavailable", "high demand", "overloaded",
                 "500", "internal", "deadline")
    last = None
    for attempt in range(3):
        try:
            return client.models.generate_content(**kwargs)
        except Exception as e:
            last = e
            if any(t in str(e).lower() for t in transient) and attempt < 2:
                time.sleep(2 * (attempt + 1))  # 2s, 4s backoff
                continue
            raise
    raise last


def available_models(api_key: str | None = None) -> list[str]:
    """Model names this key can use for text generation. [] if it can't list."""
    try:
        client = _client(api_key)
        names = []
        for m in client.models.list():
            # Attribute name differs across SDK versions; tolerate both.
            actions = (getattr(m, "supported_actions", None)
                       or getattr(m, "supported_generation_methods", None) or [])
            # If we can't tell, include it rather than hide it.
            if not actions or "generateContent" in actions:
                names.append(m.name.replace("models/", ""))
        # Put the flash models first — cheapest/fastest for this use.
        names.sort(key=lambda n: (("flash" not in n), n))
        return names
    except Exception:
        return []


def ask_gemini(prompt: str, api_key: str | None = None, model: str = DEFAULT_MODEL) -> str:
    """Single call to Gemini. `prompt` must already be anonymized/aggregated."""
    client = _client(api_key)
    resp = _generate(client, model=model, contents=f"{_SYSTEM}\n\n{prompt}")
    return (resp.text or "").strip()


def diagnose(summary: dict, api_key: str | None = None, model: str = DEFAULT_MODEL) -> str:
    """Steps 5 & 6 of the infographic: AI diagnosis + suggested actions."""
    prompt = (
        "Here is the anonymized audit summary:\n\n"
        f"{summary_to_text(summary)}\n"
        "Write a short structured report with these sections:\n"
        "1. Headline — the single most important finding in one sentence.\n"
        "2. Key patterns — 3-5 bullets, each citing a number above.\n"
        "3. Financial impact — what the over/under-billing totals mean.\n"
        "4. Recommended actions — concrete next steps for the 3WM team.\n"
        "Keep it stakeholder-friendly; avoid jargon."
    )
    return ask_gemini(prompt, api_key=api_key, model=model)


def answer_question(question: str, summary: dict, api_key: str | None = None,
                    model: str = DEFAULT_MODEL) -> str:
    """Chat/agent Q&A grounded in the same anonymized summary."""
    prompt = (
        "Audit summary (anonymized):\n\n"
        f"{summary_to_text(summary)}\n"
        f"Question: {question}\n"
        "Answer using only the figures above. If it isn't in the data, say so."
    )
    return ask_gemini(prompt, api_key=api_key, model=model)


# --------------------------------------------------------------------------
# 4. BILLING ANALYSIS  (exact numbers — computed in Python, never by the LLM)
# --------------------------------------------------------------------------
# The column the VinSolutions script fills in once a dealer has been checked.
PROCESSED_MARKER = "Vin Feature Enabled"


def analyze_billing(df: pd.DataFrame, only_processed: bool = True,
                    head: int | None = None) -> dict:
    """
    Exact over/under-billing figures.

    only_processed=True  -> just the dealers the script actually verified
                            (the ones with a "Vin Feature Enabled" value),
                            which is what "run the script on 100 and tell me"
                            should report on.
    Returns counts, dollar totals, and a confirmed/justified cross-tab:
      - GAP_TYPE = Over Billing  AND  Vin Feature Enabled != "Yes"  -> CONFIRMED
      - GAP_TYPE = Over Billing  AND  Vin Feature Enabled == "Yes"  -> likely justified
    """
    if head is not None:
        df = df.head(int(head))  # demo: scope to exactly the N "just run"
    if only_processed and PROCESSED_MARKER in df.columns:
        scope = df[df[PROCESSED_MARKER].astype(str).str.strip() != ""].copy()
    else:
        scope = df.copy()

    out: dict = {"rows_in_scope": int(len(scope))}
    if "GAP_TYPE" not in scope.columns:
        return out

    amt = _to_money(scope["ASSET_TOTAL_PRICE"]) if "ASSET_TOTAL_PRICE" in scope else 0
    scope = scope.assign(_amt=amt)

    for label, key in [("Over Billing", "over_billing"), ("Under Billing", "under_billing")]:
        sub = scope[scope["GAP_TYPE"] == label]
        out[f"{key}_count"] = int(len(sub))
        out[f"{key}_dollars"] = round(float(sub["_amt"].sum()), 2)

    # Confirmed vs. justified over-billing, using the feature-enabled result.
    if PROCESSED_MARKER in scope.columns:
        over = scope[scope["GAP_TYPE"] == "Over Billing"]
        enabled = over[PROCESSED_MARKER].astype(str).str.strip().str.lower()
        confirmed = over[enabled != "yes"]
        justified = over[enabled == "yes"]
        out["over_billing_confirmed_count"] = int(len(confirmed))
        out["over_billing_confirmed_dollars"] = round(float(confirmed["_amt"].sum()), 2)
        out["over_billing_justified_count"] = int(len(justified))

    return out


def analysis_to_text(a: dict) -> str:
    """Readable lines for the prompt / for humans."""
    def money(x):
        return f"${x:,.2f}"
    lines = [f"Dealers in scope (verified): {a.get('rows_in_scope', 0)}"]
    if "over_billing_count" in a:
        lines += [
            f"Over-billing: {a['over_billing_count']} dealers, {money(a['over_billing_dollars'])}",
            f"Under-billing: {a['under_billing_count']} dealers, {money(a['under_billing_dollars'])}",
        ]
    if "over_billing_confirmed_count" in a:
        lines += [
            f"  - Over-billing CONFIRMED (feature not enabled): "
            f"{a['over_billing_confirmed_count']} dealers, {money(a['over_billing_confirmed_dollars'])}",
            f"  - Over-billing likely justified (feature enabled): "
            f"{a['over_billing_justified_count']} dealers",
        ]
    return "\n".join(lines)


def recommend_from_analysis(a: dict, api_key: str | None = None,
                            model: str = DEFAULT_MODEL) -> str:
    """Gemini writes the recommendation FROM the exact numbers above."""
    prompt = (
        "You just ran the VinSolutions audit. Here are the EXACT results "
        "(computed in Python — use these numbers verbatim, do not recalculate):\n\n"
        f"{analysis_to_text(a)}\n\n"
        "Reply in English, stakeholder-friendly, in three short parts:\n"
        "1. Summary — how many are over-billing and how much it adds up to.\n"
        "2. What's confirmed — distinguish confirmed from likely justified.\n"
        "3. Recommendation — 2-3 concrete actions for the 3WM team.\n"
        "Quote the numbers verbatim."
    )
    return ask_gemini(prompt, api_key=api_key, model=model)
