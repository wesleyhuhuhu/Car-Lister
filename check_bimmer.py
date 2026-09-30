"""
Check what bimmer.work shows you right now, without using up a lookup.

Loads the bimmer.work home page in the automation Chrome, saves a screenshot,
and tells you whether the VIN box and Submit button are showing or whether the
site is answering with an error such as 429 (Too Many Requests).

    python check_bimmer.py                # normal Chrome window
    python check_bimmer.py --headless     # no visible window

Screenshots and page HTML go in the bmw_debug folder. Setting the BMW_HEADLESS=1
environment variable makes the lookup scripts run headless too.
"""
import argparse
import time

from bmw_lookup import BMW_URL, BmwSession


def main():
    parser = argparse.ArgumentParser(description="Check what bimmer.work shows right now.")
    parser.add_argument("--headless", action="store_true", help="Run Chrome without a visible window")
    parser.add_argument("--wait", type=float, default=5.0, help="Seconds to let the page settle before checking")
    args = parser.parse_args()

    with BmwSession(headless=args.headless or None) as session:
        page = session.page
        response = page.goto(BMW_URL, wait_until="domcontentloaded")
        session.last_status = response.status if response is not None else None
        time.sleep(args.wait)
        form_ok = False
        try:
            box = page.get_by_role("textbox").first
            submit = page.get_by_role("button", name="Submit", exact=True)
            form_ok = box.is_visible() and submit.is_visible()
        except Exception:
            pass
        info = session.snapshot("check")

    print()
    print(f"HTTP status : {info['status']}" + ("  <- Too Many Requests: rate limited" if info["status"] == 429 else ""))
    print(f"Page title  : {info['title']!r}")
    print(f"Buttons     : {info['buttons']}")
    print(f"Page text   : {info['text']!r}")
    print(f"Screenshot  : {info['png']}")
    print()
    if info["status"] is not None and info["status"] >= 400:
        print(f"Result: the site answered with an error (HTTP {info['status']}). Wait before looking up more VINs.")
    elif form_ok:
        print("Result: looks normal. The VIN box and the Submit button are showing.")
    else:
        print("Result: the page loaded, but the VIN box / Submit button are NOT showing. "
              "Open the screenshot: it usually explains why (a limit message, a human check, ...).")


if __name__ == "__main__":
    main()
