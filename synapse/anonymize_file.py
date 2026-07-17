"""
anonymize_file.py — physically remove names/addresses from the data file.

The website only anonymizes IN MEMORY before calling the AI; your audit.csv on
disk still has every dealer name and address. This makes a CLEAN copy on disk
with that info actually gone, so what you open and share has no customer data.

    python anonymize_file.py                      # audit.csv -> audit_clean.csv (+ secure lookup)
    python anonymize_file.py mydata.csv out.csv
    python anonymize_file.py --no-lookup          # don't even keep the name<->code map

What it does:
  * DELETES the name/address columns (dealer name, address, parent name, ...)
  * REPLACES account IDs with anonymous codes like D18331
  * keeps everything the script/dashboard needs (keys, product, gap, price)

Output:
  * audit_clean.csv           -> safe to open, share, or e-mail. No names.
  * audit_lookup_SECURE.csv   -> code -> real name/ID, so YOU can still trace back.
                                 Keep it private, or delete it for full anonymity
                                 (use --no-lookup to skip creating it).
"""
import sys
from pathlib import Path

import pandas as pd

from synapse_core import anonymize, PII_DROP, PII_HASH

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--no-lookup"]
    keep_lookup = "--no-lookup" not in sys.argv[1:]

    src = Path(args[0]) if len(args) > 0 else ROOT / "audit.csv"
    dst = Path(args[1]) if len(args) > 1 else ROOT / "audit_clean.csv"
    lookup_path = ROOT / "audit_lookup_SECURE.csv"

    if not src.exists():
        sys.exit(f"Not found: {src}")

    df = pd.read_csv(src, dtype=str, keep_default_na=False)
    safe_df, lookup = anonymize(df)          # drops names/addresses, hashes IDs
    safe_df.to_csv(dst, index=False)

    removed = [c for c in PII_DROP if c in df.columns]
    hashed = [c for c in PII_HASH if c in df.columns]

    print(f"Read {src.name}: {len(df)} rows, {len(df.columns)} columns")
    print(f"Wrote {dst.name}: names/addresses removed, IDs turned into codes")
    print(f"  Deleted columns : {removed}")
    print(f"  Anonymized IDs  : {hashed}")
    print(f"  Dealers hidden  : {len(lookup)}")

    if keep_lookup:
        lookup.to_csv(lookup_path, index=False)
        print(f"\nAlso wrote {lookup_path.name} (code -> real name). Keep it private, "
              f"or delete it for full anonymity.")
    else:
        print("\nNo lookup kept (--no-lookup): the real names are now unrecoverable "
              "from these files.")

    print(f"\nNext: use {dst.name} as your data. You can then securely delete/lock "
          f"the original {src.name}.")


if __name__ == "__main__":
    main()
