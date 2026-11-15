"""Fetch the bundled browser assets listed in vendor/manifest.json - build machine only.

    python tools/fetch_vendor.py            download, check each file against its pinned SHA-256
    python tools/fetch_vendor.py --pin      first download of a new version: record the hashes
    python tools/fetch_vendor.py --check    no network: check the files already in vendor/

The server itself never downloads anything and needs none of this to start. The only
asset is mermaid.js, used by the approval page with ADVANCED_WORKFLOW_PLANNING=1 (the
one exception to "no third-party dependencies" - see wiki/README.md, ground rule 1a).
A file whose hash does not match is not written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "vendor" / "manifest.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def files_of(manifest: dict) -> list[dict]:
    """The file entries themselves (not copies), so a recorded hash lands in the manifest."""
    out = []
    for entry in manifest.values():
        if isinstance(entry, dict):
            out.extend(entry.get("files", []))
    return out


def check(manifest: dict) -> list[str]:
    problems = []
    for item in files_of(manifest):
        path = ROOT / item["path"]
        if not path.exists():
            problems.append(f"missing: {item['path']}")
        elif sha256(path.read_bytes()) != item.get("sha256"):
            problems.append(f"hash mismatch: {item['path']}")
    return problems


def fetch(manifest: dict, pin: bool) -> int:
    failed = 0
    for item in files_of(manifest):
        url = item["source"]
        print(f"  {url}")
        with urlopen(Request(url, headers={"User-Agent": "planning-mcp-build"}), timeout=60) as r:
            data = r.read()
        digest = sha256(data)
        want = item.get("sha256")
        if want and want != digest:
            print(f"    REFUSED: sha256 {digest} != pinned {want}", file=sys.stderr)
            failed += 1
            continue
        if not want and not pin:
            print("    REFUSED: no pinned sha256 - run with --pin to record it", file=sys.stderr)
            failed += 1
            continue
        item["sha256"] = digest
        path = ROOT / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"    {len(data):,} bytes  sha256 {digest}")
    if pin and not failed:
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        print(f"  pinned in {MANIFEST.relative_to(ROOT)}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pin", action="store_true", help="record the hashes of a new version")
    parser.add_argument("--check", action="store_true", help="check local files, no network")
    args = parser.parse_args(argv)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if args.check:
        problems = check(manifest)
        for p in problems:
            print(p, file=sys.stderr)
        print("OK" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    return fetch(manifest, args.pin)


if __name__ == "__main__":
    raise SystemExit(main())
