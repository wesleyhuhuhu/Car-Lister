"""
Build the ZIP that people download to install the Car Lister Helper extension.

    python tools/pack_extension.py          ->  dist/car-lister-helper-<version>.zip

Only the files the browser needs go in (no tests, node_modules or package files). Attach the ZIP to a
GitHub Release; the listings page's install dialog links to the latest release. Bump "version" in
extension/manifest.json for every release so people can tell which one they have.
"""
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
INCLUDE = ["manifest.json", "*.js", "*.html", "adapters/*.js", "vendor/*", "icons/*.png"]


def main():
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    files = sorted({f for pattern in INCLUDE for f in EXT.glob(pattern) if f.is_file()})
    out = ROOT / "dist" / f"car-lister-helper-{manifest['version']}.zip"
    out.parent.mkdir(exist_ok=True)
    folder = f"car-lister-helper-{manifest['version']}"      # unzips into one folder, ready for "Load unpacked"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f"{folder}/{f.relative_to(EXT).as_posix()}")
    print(f"Wrote {out.relative_to(ROOT)} ({len(files)} files, {out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
