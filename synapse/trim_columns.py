"""
trim_columns.py — keep only the columns the tools actually use.

Your audit.csv has ~55 columns; most are never read. This writes a slim copy
with only the columns that:
  * the VinSolutions script reads or writes, and
  * the Synapse dashboard/agent needs for its analysis.

It is NON-DESTRUCTIVE: it reads one CSV and writes a NEW one, so your original
is untouched.

    python trim_columns.py                       # audit.csv  -> audit_slim.csv
    python trim_columns.py audit_demo.csv out.csv

If you want the scraper to use the slim file, rename it to audit.csv afterwards.
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

# --- Columns actually used, grouped by why we keep them ---------------------
KEYS = ["MAPPING_PFA_ID", "ASSET_PFA_ID"]          # dealer lookup keys
PRODUCT = ["PRODUCT_NAME", "PRODUCTCODE"]          # product matching + reporting
ANALYSIS = ["GAP_TYPE", "ASSET_TOTAL_PRICE", "IMPLEMENTATION_STATUS",
            "BUSINESS_UNIT__C", "TWOWAYMATCH", "THREEWAYMATCH"]
IDENTITY = ["ROOFTOP_ACCOUNT_CAID", "ROOFTOP_ACCOUNT_NAME"]  # for local traceability
# Columns the script WRITES (kept if already present):
OUTPUTS = ["VIN Account Status", "Vin Name", "ILM Status", "Full Crm Status",
           "Max Number of Users", "Desking", "AIS Status",
           "Rates & Residuals as Enabled", "Vin Feature Enabled", "Inventory"]

KEEP = KEYS + PRODUCT + ANALYSIS + IDENTITY + OUTPUTS


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "audit.csv"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "audit_slim.csv"
    if not src.exists():
        sys.exit(f"Not found: {src}")

    df = pd.read_csv(src, dtype=str, keep_default_na=False)
    keep = [c for c in KEEP if c in df.columns]
    missing = [c for c in KEEP if c not in df.columns]
    dropped = [c for c in df.columns if c not in keep]

    df[keep].to_csv(dst, index=False)

    print(f"Read {src.name}: {len(df.columns)} columns, {len(df)} rows")
    print(f"Wrote {dst.name}: {len(keep)} columns kept")
    print(f"\nKept ({len(keep)}): {keep}")
    print(f"\nDropped ({len(dropped)}): {dropped}")
    if missing:
        print(f"\nNote — expected but not in this file: {missing}")


if __name__ == "__main__":
    main()
