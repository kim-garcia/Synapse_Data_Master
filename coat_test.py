"""
coat_test.py  --  quick single-account test for coat_audit.py

Logs in once, looks up ONE ROOFTOP_ACCOUNT_CAID, prints the COAT status, and
leaves the browser open so you can inspect the page while we tune selectors.
Does NOT read or write audit.csv.

RUN:   python coat_test.py CA11252814
       (defaults to CA11252814 if no CAID is given)
"""

import sys

import coat_audit as c


def main():
    caid = sys.argv[1].strip() if len(sys.argv) > 1 else "CA11252814"
    c.setup_logging()
    c.log.info("=== COAT single-account test: %s ===", caid)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            c.PROFILE_DIR,
            headless=c.HEADLESS,
            viewport=c.WINDOW,
            args=[f"--window-size={c.WINDOW['width']},{c.WINDOW['height']}"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        c.wait_for_login(page)

        result = c.lookup_coat(page, caid, {})
        status = result.get("COAT status")

        c.log.info("RESULT for %s: %r", caid, status)
        print("\n==================  RESULT  ==================")
        print(f"  {caid}  ->  {status!r}")
        print("==============================================")
        print(f"(full log saved to {c.LOG_FILE})")

        input("\nBrowser stays open for inspection. Press Enter to close...")
        ctx.close()


if __name__ == "__main__":
    main()
