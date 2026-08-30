"""
check_mapping.py  --  preflight check for the VinSolutions audit (no browser)

Run this BEFORE audit_automation.py to prove the mapping CSV is actually being
read and that every product in audit.csv will get a real verdict. It opens no
browser and needs no login, so it takes a second.

    python scrapers/check_mapping.py

Exit code 0 = safe to run the audit. Non-zero = fix what it reports first.

It checks, in order:
  1. audit.csv and SFXvsFulfillmentAuditMapping.csv are both present in the
     CURRENT folder (both scripts use paths relative to where you run them).
  2. The mapping CSV parses and has the required columns.
  3. No product name is listed twice with contradictory expressions.
  4. Every distinct PRODUCT_NAME in audit.csv resolves to a rule it can
     evaluate - nothing falls through to "Check manually".
It also prints which source decides each product: the CSV, or a documented
business exception in PRODUCT_RULES.
"""

import csv
import sys
import logging
import collections
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_automation as A          # noqa: E402

OK, BAD, WARN = "OK   ", "FALLA", "AVISO"
_problems = []
_warnings = []


def _say(status, msg):
    print(f"[{status}] {msg}")
    if status == BAD:
        _problems.append(msg)
    elif status == WARN:
        _warnings.append(msg)


def check_files():
    print("\n=== 1. Archivos en la carpeta actual ===")
    print(f"    carpeta: {Path.cwd()}")
    for name in (A.CSV_PATH, A.MAPPING_FILE):
        p = Path(name)
        if p.exists():
            _say(OK, f"{name} encontrado ({p.stat().st_size:,} bytes)")
        else:
            _say(BAD, f"{name} NO esta en {Path.cwd()} - copialo aca")
    return all(Path(n).exists() for n in (A.CSV_PATH, A.MAPPING_FILE))


def check_mapping_loads():
    print("\n=== 2. Carga del archivo de mapeo ===")
    A.load_mapping()
    if not A._MAPPING:
        _say(BAD, f"{A.MAPPING_FILE} cargo 0 filas - revisa las columnas "
                  f"{A.NAME_COL!r} y {A.FEATURE_IDS_COL!r}")
        return False
    _say(OK, f"cargadas {len(A._MAPPING)} filas de {A.MAPPING_FILE}")
    return True


def check_conflicts():
    print("\n=== 3. Filas duplicadas contradictorias ===")
    by_name = collections.defaultdict(set)
    for name, expr in A._MAPPING:
        by_name[name.strip().lower()].add(expr.strip())
    bad = {n: e for n, e in by_name.items() if len(e) > 1}
    if not bad:
        _say(OK, "sin conflictos")
        return
    for n, exprs in bad.items():
        _say(WARN, f"{n!r} aparece con {len(exprs)} expresiones distintas: "
                   f"{sorted(exprs)} - se usa la primera del archivo. "
                   f"No bloquea la auditoria, pero conviene borrar una fila.")


def check_coverage():
    print("\n=== 4. Cobertura de productos en audit.csv ===")
    with open(A.CSV_PATH, newline="", encoding=A._read_encoding(Path(A.CSV_PATH))) as f:
        rows = list(csv.DictReader(f, delimiter=A._detect_delimiter(Path(A.CSV_PATH))))
    names = collections.Counter((r.get("PRODUCT_NAME") or "").strip() for r in rows)
    if not names:
        _say(BAD, "audit.csv no tiene columna PRODUCT_NAME con valores")
        return

    src = collections.Counter()
    print(f"\n    {'PRODUCTO':<58}{'n':>4}  {'FUENTE':<12}REGLA APLICADA")
    print("    " + "-" * 124)
    for name, count in sorted(names.items()):
        rule = A._match_rule(name)
        expr = A._lookup_expression(name)
        exception = bool(rule and rule.get("override_mapping"))
        use_mapping = bool(expr) and (A.MAPPING_FILE_WINS or not rule) and not exception

        if exception:
            source, shown = "EXCEPCION", " OR ".join(rule["codes"])
        elif use_mapping:
            source, shown = "ARCHIVO", expr
        elif rule:
            source, shown = "regla", rule.get("codes") or rule.get("columns")
        else:
            source, shown = "SIN REGLA", "-> Check manually"

        if source != "SIN REGLA":
            mode, tokens = A._parse_expression(
                expr if use_mapping else " OR ".join(rule.get("codes", ["x"])))
            if mode is None or not tokens:
                source, shown = "NO PARSEA", f"{expr!r} (AND y OR mezclados)"

        src[source] += count
        print(f"    {name[:56]:<58}{count:>4}  {source:<12}{shown}")

    print(f"\n    filas por fuente: {dict(src)}")
    broken = src["SIN REGLA"] + src["NO PARSEA"]
    if broken:
        _say(BAD, f"{broken} filas terminarian en '{A.FEATURE_CHECK_VALUE}'")
    else:
        _say(OK, f"las {sum(src.values())} filas se deciden con una regla real")


def main():
    logging.disable(logging.CRITICAL)      # the checks print their own output
    print("=" * 70)
    print(" PREFLIGHT - auditoria VinSolutions")
    print("=" * 70)

    if check_files() and check_mapping_loads():
        check_conflicts()
        check_coverage()

    print("\n" + "=" * 70)
    if _warnings and not _problems:
        print(f" {len(_warnings)} aviso(s) - revisalos, pero no bloquean:")
        for w in _warnings:
            print(f"   - {w}")
        print("-" * 70)
    if _problems:
        print(f" RESULTADO: {len(_problems)} problema(s) - NO corras la auditoria todavia")
        for p in _problems:
            print(f"   - {p}")
        print("=" * 70)
        sys.exit(1)
    print(" RESULTADO: todo OK - podes correr audit_automation.py")
    print("=" * 70)


if __name__ == "__main__":
    main()
