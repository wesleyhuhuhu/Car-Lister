"""
Debug helper: loads an AutoTempest search page, waits for the async
per-source result sections to populate, then saves the fully-rendered
HTML and a screenshot so we can read off the real CSS selectors.

Run this, then open autotempest_debug.html in a browser (or just a text
editor) and search for a chunk of text you recognize from a listing (a
price like "$21,995" or a dealer name) to see what tags/classes wrap it.
Share that snippet back and I'll fix the selectors in autotempest_scraper.py.
"""
from playwright.sync_api import sync_playwright
from autotempest_scraper import build_search_url

def main():
    url = build_search_url(make="toyota", model="camry", zip="90001", rad=50)
    print(f"Loading: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        page.goto(url, timeout=45000)

        # Give the per-source XHR calls time to fire and populate the DOM
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            print("networkidle wait timed out, continuing anyway")
        page.wait_for_timeout(5000)  # extra buffer for late-loading sources

        # Nudge lazy-loaded sections into view
        for _ in range(4):
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1000)

        html = page.content()
        with open("autotempest_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
        page.screenshot(path="autotempest_debug.png", full_page=True)

        # Print any elements whose class name hints at being a listing/result
        candidates = page.eval_on_selector_all(
            "[class*='result' i], [class*='listing' i], [class*='card' i]",
            "els => [...new Set(els.map(e => e.className))].slice(0, 40)",
        )
        print("\nCandidate class names found on the page:")
        for c in candidates:
            print(" -", c)

        browser.close()

    print("\nSaved autotempest_debug.html and autotempest_debug.png")
    print("Open the HTML file and search for a price or dealer name from")
    print("a real listing to find the actual wrapping element/class.")

if __name__ == "__main__":
    main()
