"""
Synapse Data Master — Phase 1: AI diagnosis from the command line (no website).

Run it to prove the AI value on your existing audit.csv:

    python diagnose.py                 # uses ../audit.csv
    python diagnose.py path\to\file.csv

Before running:
    pip install -r requirements.txt
    set GEMINI_API_KEY=your_key_here        (Windows CMD)
    $env:GEMINI_API_KEY="your_key_here"     (PowerShell)
"""

import sys
from pathlib import Path

import pandas as pd

from synapse_core import anonymize, build_gap_summary, summary_to_text, diagnose

# Default to the audit.csv that sits one level up (in the audit/ folder).
DEFAULT_CSV = Path(__file__).resolve().parent.parent / "audit.csv"


def main() -> None:
    csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path}")

    print(f"Reading {csv_path} ...")
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)

    # Step 2 of the infographic: strip names/addresses before anything leaves.
    safe_df, lookup = anonymize(df)
    print(f"Anonymized {len(df)} rows "
          f"({len(lookup)} unique dealers hidden behind surrogates).")

    # Steps 3-4 are already done in the CSV; here we summarize them.
    summary = build_gap_summary(safe_df)
    print("\n----- FACTS SENT TO GEMINI (no customer names) -----")
    print(summary_to_text(summary))

    # Steps 5-6: AI diagnosis + suggestions.
    print("----- GEMINI DIAGNOSIS -----\n")
    print(diagnose(summary))


if __name__ == "__main__":
    main()
