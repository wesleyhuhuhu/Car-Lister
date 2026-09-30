"""
Show exactly what NHTSA returns for VINs, using both the batch endpoint (what
main.py uses) and the single-VIN endpoint, so a VIN that stays undecoded can be
understood.

    python debug_nhtsa.py                 # every VIN in listings.json missing year/make/model
    python debug_nhtsa.py VIN1 VIN2       # specific VINs
"""
import json
import sys

import requests

try:                                   # company networks that inspect HTTPS
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from listing_store import load_rows
from vin_lookup import NHTSA_BATCH_URL, NHTSA_DECODE_URL

SHOW_ALWAYS = ("ErrorCode", "ErrorText", "AdditionalErrorText", "SuggestedVIN", "PossibleValues")


def vins_to_check(argv):
    if argv:
        return argv
    rows = load_rows("listings.json")
    return [r["vin"] for r in rows
            if r.get("vin") and not (r.get("year") and r.get("make") and r.get("model"))]


def main():
    vins = vins_to_check(sys.argv[1:])
    if not vins:
        print("No VINs to check.")
        return
    for vin in vins:
        print("=" * 70)
        print(f"VIN {vin!r}  (length {len(vin)})")
        try:
            resp = requests.post(NHTSA_BATCH_URL, data={"format": "json", "data": vin}, timeout=60)
            print(f"-- batch endpoint: HTTP {resp.status_code}")
            for rec in resp.json().get("Results", []):
                shown = {k: v for k, v in rec.items() if v not in (None, "") or k in SHOW_ALWAYS}
                print(json.dumps(shown, indent=2))
        except Exception as exc:
            print(f"-- batch endpoint failed: {exc.__class__.__name__}: {exc}")
        try:
            resp = requests.get(NHTSA_DECODE_URL.format(vin=vin), timeout=30)
            print(f"-- single endpoint: HTTP {resp.status_code}")
            items = resp.json().get("Results", [])
            shown = {i["Variable"]: i["Value"] for i in items
                     if i.get("Value") not in (None, "", "Not Applicable")
                     and (i["Variable"] in ("Model Year", "Make", "Model", "Trim") or "Error" in i["Variable"]
                          or i["Variable"] in ("Suggested VIN", "Possible Values"))}
            print(json.dumps(shown, indent=2))
        except Exception as exc:
            print(f"-- single endpoint failed: {exc.__class__.__name__}: {exc}")


if __name__ == "__main__":
    main()
