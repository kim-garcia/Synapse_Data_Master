"""
mask_file.py — readable masking for REAL scraped data (for sharing).

Use this after you scrape the real dealers, when you want to share results
without exposing real data. It:
  * removes names/addresses,
  * masks the last digits of each CAID   (CA11212546 -> CA11212XXX),
  * turns product names into category codes (Customer Texting... -> TXT-042),
and writes a small legend so YOU can still read the codes.

NON-DESTRUCTIVE — reads one file, writes new ones. Your real audit.csv (which
the scraper needs) is untouched.

    python mask_file.py                    # audit.csv -> audit_masked.csv (+ legend)
    python mask_file.py mydata.csv out.csv
"""
import sys
from pathlib import Path

import pandas as pd

from synapse_core import mask_dataframe

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "audit.csv"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "audit_masked.csv"
    legend_path = ROOT / "product_legend_SECURE.csv"

    if not src.exists():
        sys.exit(f"Not found: {src}")

    df = pd.read_csv(src, dtype=str, keep_default_na=False)
    masked, legend = mask_dataframe(df)
    masked.to_csv(dst, index=False)
    legend.to_csv(legend_path, index=False)

    print(f"Read {src.name}: {len(df)} rows")
    print(f"Wrote {dst.name}: names removed, CAIDs masked, products as category codes")
    print(f"Wrote {legend_path.name}: {len(legend)} product codes -> real names "
          f"(keep private — this is the key to read the codes)")
    print("\nSample of the masking:")
    cols = [c for c in ["ROOFTOP_ACCOUNT_CAID", "PRODUCT_NAME", "GAP_TYPE",
                        "ASSET_TOTAL_PRICE"] if c in masked.columns]
    print(masked[cols].head(5).to_string(index=False))


if __name__ == "__main__":
    main()
