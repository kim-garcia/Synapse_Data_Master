"""
sf_test.py  --  quick single-account test for sf_audit.py

Logs in once, looks up ONE ASSET_BILLTO_ROOFTOP_CAID + PRODUCT_NAME, prints
both output values, and leaves the browser open for inspection.
Does NOT read or write audit.csv.

RUN:   python sf_test.py CA12345678 "CRM/ILM"
       (defaults shown above if no args given)
"""

import sys

import sf_audit as s


def main():
    caid    = sys.argv[1].strip() if len(sys.argv) > 1 else "CA12345678"
    product = sys.argv[2].strip() if len(sys.argv) > 2 else ""
    price   = sys.argv[3].strip() if len(sys.argv) > 3 else ""

    s.setup_logging()
    s.log.info("=== SF single-account test: %s | product: %r ===", caid, product)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            s.PROFILE_DIR,
            headless=False,
            viewport=s.WINDOW,
            args=[f"--window-size={s.WINDOW['width']},{s.WINDOW['height']}"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page = s.wait_for_login(page)

        sf_name, asset_status = s.lookup_sf(page, caid, product, price)

        s.log.info("RESULT — name=%r  status=%r", sf_name, asset_status)
        print("\n==================  RESULT  ==================")
        print(f"  CAID     : {caid}")
        print(f"  Product  : {product!r}")
        print(f"  SF Name  : {sf_name!r}")
        print(f"  Status   : {asset_status!r}")
        print("==============================================")
        print(f"(full log saved to {s.LOG_FILE})")

        input("\nBrowser stays open for inspection. Press Enter to close...")
        ctx.close()


if __name__ == "__main__":
    main()
