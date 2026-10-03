"""Build a reviewable transfer package for the corporate PC.

    python tools/make_package.py
    python tools/make_package.py --with-python C:\\dl\\python-3.12.10-embed-amd64.zip

Produces dist/planning-mcp-<version>-<date>.zip containing only plain-text source, plus
a MANIFEST.txt of per-file SHA-256 hashes so the copy can be proven intact on arrival.

With --with-python, the official python.org embeddable distribution is bundled so the
destination machine needs no Python at all. The zip is embedded VERBATIM - never
extracted, never modified - so a security reviewer can hash it against the checksum
published on python.org and confirm the interpreter is genuine.

Deliberately excluded: state/ (contains real plan contents from your own testing),
__pycache__/ (compiled blobs look opaque to a security reviewer), .git/ (history can
carry local paths and identities), dist/ itself.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from planning.config import SERVER_VERSION  # noqa: E402

# Everything shipped, listed explicitly. An allow-list, not a deny-list: a deny-list
# eventually leaks something you did not intend to hand to a security reviewer.
INCLUDE_FILES = [
    "server.py",
    "README.md",
    # The agent system prompt. Since 3.0.0 the README no longer carries a copy of it -
    # it names this file as the thing to paste - so a package without it would point
    # the operator at a file that is not there.
    "agents.md",
    ".gitignore",
    "anythingllm_mcp_servers.example.json",
]
INCLUDE_DIRS = [
    ("planning", "*.py"),
    ("tests", "*.py"),
    ("tools", "*.py"),
    ("docs", "*.md"),
]

EXCLUDE_NAMES = {"__pycache__", "state", "dist", ".git", ".venv", "venv"}


def collect() -> list[Path]:
    files: list[Path] = []
    for name in INCLUDE_FILES:
        path = ROOT / name
        if path.is_file():
            files.append(path)
        else:
            print(f"  ! missing, skipped: {name}")
    for dirname, pattern in INCLUDE_DIRS:
        base = ROOT / dirname
        if not base.is_dir():
            print(f"  ! missing, skipped: {dirname}/")
            continue
        for path in sorted(base.rglob(pattern)):
            if EXCLUDE_NAMES & set(path.relative_to(ROOT).parts):
                continue
            files.append(path)
    return files


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_embed_zip(path: Path) -> tuple[bool, str]:
    """Confirm this really is an embeddable CPython distribution before shipping it."""
    if not path.is_file():
        return False, "file not found"
    if path.suffix.lower() != ".zip":
        return False, "not a .zip"
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        return False, "not a readable zip archive"
    if "python.exe" not in names:
        return False, "no python.exe at the archive root"
    stdlib = [n for n in names if n.startswith("python") and n.endswith(".zip")]
    pth = [n for n in names if n.endswith("._pth")]
    if not stdlib:
        return False, "no bundled stdlib zip - is this the embeddable distribution?"
    if not pth:
        return False, "no python3xx._pth - is this the embeddable distribution?"
    return True, f"{len(names)} files, stdlib {stdlib[0]}, {pth[0]}"


RUNTIME_NOTE = """\
# Bundled Python runtime - provenance

This directory contains the OFFICIAL Python embeddable distribution from python.org,
embedded verbatim. It has not been extracted, repacked, or modified in any way.

  file    : {name}
  sha256  : {digest}
  size    : {size:,} bytes

## How to verify it is genuine

Compare the SHA-256 above against the checksum published on python.org for the same
filename (Downloads -> Windows -> "Windows embeddable package (64-bit)"). If the values
match, the interpreter is bit-identical to the one Python distributes.

## What it contains

python.exe, the CPython DLL, the standard library as a zip, and a small set of C
extension modules. It has no pip, no installer, and writes nothing outside this folder.

## How it is set up

Run `python tools/setup_runtime.py` (with any Python), or see deployment manual
section 3-3. Setup extracts this zip in place and appends one line ("..") to
python3xx._pth so the interpreter can import the project. That single text line is the
only local modification, and it is visible in the file afterwards.

## Removing it

Delete this folder. Nothing outside it is touched - no registry keys, no services, no
PATH changes.
"""


def manifest_text(files: list[Path], built: str, runtime_line: str | None = None) -> str:
    """Per-file SHA-256 lines for `files`, plus the bundled runtime's line if given."""
    lines = [
        "# planning-mcp transfer manifest",
        f"# version      : {SERVER_VERSION}",
        f"# built        : {built}",
        f"# files        : {len(files)}",
        f"# total bytes  : {sum(f.stat().st_size for f in files)}",
        "# python       : 3.9 or newer (developed and tested on 3.12)",
        "# dependencies : none - Python standard library only",
        "#",
        "# Verify on arrival with:  python tools/verify_install.py",
        "#",
        "# sha256                                                            size  path",
    ]
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        lines.append(f"{sha256(path)}  {path.stat().st_size:>7}  {rel}")
    if runtime_line:
        lines.append(runtime_line)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-python",
        metavar="EMBED_ZIP",
        help="Path to the official python-3.x.y-embed-amd64.zip from python.org",
    )
    args = parser.parse_args()

    stamp = datetime.date.today().strftime("%Y%m%d")
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    suffix = "-with-python" if args.with_python else ""
    archive = dist / f"planning-mcp-{SERVER_VERSION}-{stamp}{suffix}.zip"

    print(f"Packaging planning-mcp {SERVER_VERSION}\n")

    embed: Path | None = None
    if args.with_python:
        embed = Path(args.with_python).expanduser().resolve()
        ok, detail = inspect_embed_zip(embed)
        if not ok:
            print(f"  ! --with-python rejected: {detail}\n    {embed}")
            return 1
        print(f"  runtime : {embed.name}  ({detail})")

    files = collect()
    if not files:
        print("Nothing to package.")
        return 1

    runtime_note = ""
    runtime_line = None
    if embed is not None:
        embed_digest = sha256(embed)
        runtime_line = f"{embed_digest}  {embed.stat().st_size:>7}  runtime/{embed.name}"
        runtime_note = RUNTIME_NOTE.format(
            name=embed.name, digest=embed_digest, size=embed.stat().st_size
        )

    built = datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()
    # Two manifests, because they describe two different trees. The archive's names the
    # bundled runtime zip; the repository's must not - the working tree has no runtime/
    # zip, and a manifest that lists one makes verify_install fail here with "missing".
    # Writing the archive's manifest over the repo's is what used to force "build
    # --with-python first, source-only last".
    manifest = manifest_text(files, built)
    archive_manifest = manifest_text(files, built, runtime_line)
    (ROOT / "MANIFEST.txt").write_text(manifest, encoding="utf-8")

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            rel = path.relative_to(ROOT).as_posix()
            zf.write(path, f"planning-mcp/{rel}")
            print(f"  + {rel}")
        zf.writestr("planning-mcp/MANIFEST.txt", archive_manifest)
        print("  + MANIFEST.txt")
        if embed is not None:
            # Stored, not deflated: it is already compressed, and leaving the bytes
            # untouched keeps the python.org checksum meaningful.
            zf.write(embed, f"planning-mcp/runtime/{embed.name}", zipfile.ZIP_STORED)
            zf.writestr("planning-mcp/runtime/RUNTIME.md", runtime_note)
            print(f"  + runtime/{embed.name}  (verbatim, stored uncompressed)")
            print("  + runtime/RUNTIME.md")

    count = len(files) + 1 + (2 if embed is not None else 0)
    kind = "source plain text + 1 official Python zip" if embed else "all plain text"
    print(f"\nArchive : {archive}")
    print(f"Size    : {archive.stat().st_size:,} bytes ({count} entries, {kind})")
    print(f"SHA-256 : {sha256(archive)}")
    if embed is not None:
        print(
            f"\nBundled interpreter: {embed.name}\n"
            "  Submit its python.org checksum with your security request - the reviewer can\n"
            "  verify the interpreter independently. See runtime/RUNTIME.md in the archive."
        )
    print(
        "\nRecord the SHA-256 above somewhere OUTSIDE the archive (a note, a chat message,\n"
        "a photo of a written line). On the corporate PC, compare it before unpacking:\n"
        f"    Get-FileHash .\\{archive.name} -Algorithm SHA256"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
