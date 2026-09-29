from __future__ import annotations

import json
import re
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError


# ---------------------------------------------------------------------------
# Vehicle information
# ---------------------------------------------------------------------------

# How long to wait for bimmer.work to show the vehicle information.
VEHICLE_WAIT_SECONDS = 35

VEHICLE_FIELDS = (
    "Market",
    "Transmission",
    "Color",
    "Upholstery",
    "Start of Production",
)


def extract_vehicle_field(page: Page, field_name: str) -> str | None:
    """
    Extract a value from the bimmer.work vehicle table, whose rows look like:

        <tr><th>Market</th><td><i class="bi-pin-map"></i>&nbsp;USA (left Steering)</td></tr>

    Reading the <th>/<td> cells is reliable. The page's plain text renders
    the same row as "Market<TAB> USA (left Steering)", which the old
    "Label | Value" / "next line" parsing could not match. A text-based
    fallback is kept in case the markup changes.
    """
    cell = page.locator(
        f"xpath=//th[normalize-space(.)='{field_name}']/following-sibling::td[1]"
    )
    if cell.count():
        value = " ".join(cell.first.inner_text().split())
        if value:
            return value

    body = page.locator("body").inner_text()
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        for separator in ("\t", "|"):
            if separator in line:
                label, _, value = line.partition(separator)
                if label.strip() == field_name and value.strip():
                    return value.strip()
        if line == field_name and index + 1 < len(lines):
            return lines[index + 1]

    return None


def scrape_vehicle_information(page: Page) -> dict[str, str | None]:
    """
    Extract the requested vehicle information from the bimmer.work
    vehicle page.
    """

    # bimmer.work takes roughly 15-18 seconds to process a VIN.
    # Rather than sleeping a fixed amount of time, wait for actual
    # vehicle content to appear.
    try:
        page.locator(
            "xpath=//th[normalize-space(.)='Market']"
        ).wait_for(
            state="visible",
            timeout=VEHICLE_WAIT_SECONDS * 1000,
        )
    except PlaywrightTimeoutError:
        raise RuntimeError(
            f"Vehicle information did not appear within {VEHICLE_WAIT_SECONDS} seconds."
        )

    vehicle_info = {}

    for field in VEHICLE_FIELDS:
        vehicle_info[field] = extract_vehicle_field(
            page,
            field,
        )

    return vehicle_info


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

def options_url_for(vehicle_page_url: str) -> str:
    """https://bimmer.work/vin/<id>/  ->  https://bimmer.work/vin/<id>/options/"""
    match = re.match(r"(https?://[^/]+/vin/[^/?#]+)", vehicle_page_url)
    if not match:
        raise RuntimeError(f"Not a bimmer.work vehicle page: {vehicle_page_url}")
    return match.group(1) + "/options/"


def parse_options_table(page: Page) -> dict[str, str]:
    """
    Read every option row on an already-loaded bimmer.work Options page.

    Each option is a table row whose second cell looks like:

        <td><b>248</b><br>Steering Wheel Heating<br>Lenkradheizung</td>

    i.e. code, English description, German description separated by <br>.
    Some rows have no English text (<b>1U0</b><br><br>German...), so the
    German text is used for those instead of dropping the description.
    Ad rows and link rows contain no <b> and are skipped automatically.
    """
    rows = page.evaluate(
        """() => [...document.querySelectorAll("tr")].flatMap(tr => {
            const b = tr.querySelector("td > b");
            if (!b) return [];
            const segments = [];
            let current = "";
            for (const node of b.parentElement.childNodes) {
                if (node === b) continue;
                if (node.nodeName === "BR") { segments.push(current.trim()); current = ""; }
                else current += node.textContent;
            }
            segments.push(current.trim());
            return [{code: b.textContent.trim(),
                     english: segments[1] || "",
                     german: segments[2] || ""}];
        })"""
    )

    options: dict[str, str] = {}
    for row in rows:
        code = row["code"].upper()
        if code:
            options[code] = row["english"] or row["german"]
    return options


def scrape_bmw_options(page: Page) -> dict[str, str]:
    """
    Navigate to the Options page and extract every BMW option code
    and its English description.

    Returns:

        {
            "1CA": "Selection Of Cop Relevant Vehicles",
            "205": "Automatic Transmission",
            ...
        }
    """

    # Go straight to the Options page instead of clicking the tab. On
    # bimmer.work the tabs are <a role="tab" href=".../options/">, and an
    # explicit role replaces the implicit "link" role, so
    # get_by_role("link", name="Options") can never match them. The page also
    # renders two copies of the nav and a cookie overlay that can block clicks.
    print("Opening BMW Options page...")
    page.goto(options_url_for(page.url), wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        pass  # ads/analytics can keep the network busy; the content is already there

    try:
        page.wait_for_selector("tr td > b", state="attached", timeout=15_000)
    except PlaywrightTimeoutError:
        raise RuntimeError(
            f"No option rows found on the Options page. Current URL: {page.url}"
        )

    options = parse_options_table(page)

    if not options:
        raise RuntimeError(
            "No BMW options could be extracted from the Options page."
        )

    print(f"Extracted {len(options)} BMW options.")

    return options


# ---------------------------------------------------------------------------
# Complete build sheet
# ---------------------------------------------------------------------------

def scrape_bmw_build_sheet(page: Page) -> dict[str, Any]:
    """
    Extract the requested vehicle information and all factory options.

    The page must already be on the bimmer.work vehicle result page.
    """

    vehicle_info = scrape_vehicle_information(page)

    print("Vehicle information:")
    for key, value in vehicle_info.items():
        print(f"  {key}: {value}")

    options = scrape_bmw_options(page)

    return {
        **vehicle_info,
        "Options": options,
    }


def build_sheet_to_json(build_sheet: dict[str, Any]) -> str:
    """
    Serialize the build sheet into a single JSON string suitable for
    storing inside one CSV cell.
    """

    return json.dumps(
        build_sheet,
        ensure_ascii=False,
        separators=(",", ":"),
    )