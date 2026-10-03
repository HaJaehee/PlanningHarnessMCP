"""Post-transfer acceptance check. Run this FIRST on the corporate PC.

    python tools/verify_install.py

Answers one question: is this copy intact and does it actually work on this machine,
before you spend any time wiring it into AnythingLLM. Prints a GO / NO-GO verdict and
exits non-zero on failure.

Checks, in order:
  1. Python version
  2. No third-party imports needed (stdlib only)
  3. File integrity against MANIFEST.txt (if present)
  4. Every module imports
  5. Unit suite passes
  6. stdio end-to-end smoke test passes
  7. State directory is writable
"""

from __future__ import annotations

import hashlib
import importlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MIN_PYTHON = (3, 9)
_results: list[tuple[str, bool, str]] = []


def record(label: str, ok: bool, detail: str = "") -> bool:
    _results.append((label, ok, detail))
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  -> {detail}" if detail else ""))
    return ok


def check_python() -> bool:
    v = sys.version_info
    ok = v[:2] >= MIN_PYTHON
    return record(
        f"Python {v.major}.{v.minor}.{v.micro} (need >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})",
        ok,
        "" if ok else "Install a newer Python - see the deployment manual, section 3.",
    )


def check_manifest() -> bool:
    manifest = ROOT / "MANIFEST.txt"
    if not manifest.exists():
        return record("MANIFEST.txt integrity check", True, "no manifest present, skipped")
    bad: list[str] = []
    missing: list[str] = []
    checked = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        expected, _size, rel = parts
        path = ROOT / rel
        if not path.exists():
            missing.append(rel)
            continue
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        checked += 1
        if h.hexdigest() != expected:
            bad.append(rel)
    if bad or missing:
        detail = ""
        if bad:
            detail += f"modified: {', '.join(bad[:3])}"
        if missing:
            detail += f" missing: {', '.join(missing[:3])}"
        return record("MANIFEST.txt integrity check", False, detail.strip())
    return record("MANIFEST.txt integrity check", True, f"{checked} files match")


def check_no_third_party() -> bool:
    """Prove the zero-dependency claim on this machine, not just in the README."""
    # sys.stdlib_module_names only exists on 3.10+; on 3.9 we skip the foreign-import
    # comparison rather than fail a check the interpreter cannot answer.
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    modules = [
        "planning.config",
        "planning.models",
        "planning.schemas",
        "planning.store",
        "planning.leniency",
        "planning.state_machine",
        "planning.responses",
        "planning.handlers",
        "planning.protocol",
        "planning.transport",
    ]
    before = set(sys.modules)
    try:
        for name in modules:
            importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        return record("All modules import", False, f"{type(exc).__name__}: {exc}")
    record("All modules import", True, f"{len(modules)} modules")

    if not stdlib:
        return record(
            "No third-party packages required", True, "skipped (needs Python 3.10+ to check)"
        )

    foreign = {
        m.split(".")[0]
        for m in set(sys.modules) - before
        if not m.startswith("planning")
        and m.split(".")[0] not in stdlib
        and not m.startswith("_")
    }
    return record(
        "No third-party packages required",
        not foreign,
        "" if not foreign else f"unexpected: {sorted(foreign)}",
    )


def run_child(label: str, argv: list[str]) -> bool:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, *argv],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    ok = proc.returncode == 0
    if not ok:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-6:]
        return record(label, False, " | ".join(tail))
    summary = ""
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.startswith("Ran ") or line.startswith("All smoke"):
            summary = line.strip()
    return record(label, True, summary)


def check_state_writable() -> bool:
    state_dir = Path(os.environ.get("PLANNING_MCP_STATE_DIR") or (ROOT / "state"))
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        probe = state_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return record(
            f"State directory writable ({state_dir})",
            False,
            f"{type(exc).__name__} - set PLANNING_MCP_STATE_DIR to a writable path",
        )
    return record(f"State directory writable ({state_dir})", True)


def check_bundled_runtime() -> bool:
    """If this package shipped an interpreter, make sure it is the one being used."""
    runtime = ROOT / "runtime"
    exe = runtime / "python.exe"
    pending = sorted(runtime.glob("python-*embed*.zip")) if runtime.is_dir() else []

    if not exe.exists() and not pending:
        return record("Bundled runtime", True, "none shipped, using system Python")

    if not exe.exists():
        return record(
            "Bundled runtime is set up",
            False,
            f"found {pending[0].name} but it is not extracted - run: python tools/setup_runtime.py",
        )

    # Re-extracting the embed zip (an innocent, documented action) silently reverts the
    # ._pth patch. The server still works (server.py bootstraps its own sys.path), which
    # is exactly why this must be checked explicitly - nothing else will notice.
    pth_files = list(runtime.glob("python*._pth"))
    if pth_files:
        has_bom = pth_files[0].read_bytes()[:3] == b"\xef\xbb\xbf"
        lines = pth_files[0].read_text(encoding="utf-8-sig").splitlines()
        patched = any(line.strip() == ".." for line in lines) and not has_bom
        detail = ""
        if has_bom:
            detail = "file has a BOM (breaks the interpreter) - run: python tools/setup_runtime.py"
        elif not patched:
            detail = "the embed zip was re-extracted - run: python tools/setup_runtime.py"
        if not record(
            f"Runtime {pth_files[0].name} is patched and BOM-free", patched, detail
        ):
            return False

    using_it = Path(sys.executable).resolve() == exe.resolve()
    return record(
        "Bundled runtime in use",
        using_it,
        "" if using_it else f"you are running {sys.executable}\n           use instead: {exe}",
    )


def check_utf8() -> bool:
    """Korean plan text hits cp949 errors when UTF-8 mode is off."""
    enc = (sys.stdout.encoding or "").lower()
    ok = "utf-8" in enc or "utf8" in enc
    return record(
        f"Console encoding is UTF-8 (found {enc or 'unknown'})",
        ok,
        "" if ok else "Set PYTHONUTF8=1 in the AnythingLLM MCP config env block.",
    )


def main() -> int:
    print(f"\nplanning-mcp installation check\n  location: {ROOT}\n")

    print("Environment")
    check_python()
    check_bundled_runtime()
    check_utf8()
    check_state_writable()

    print("\nIntegrity")
    check_manifest()

    print("\nImports")
    check_no_third_party()

    print("\nTests")
    run_child("Unit suite", ["-m", "unittest", "discover", "-s", "tests"])
    run_child("stdio end-to-end smoke test", ["tests/smoke_stdio.py"])
    run_child("blocking-approval smoke test", ["tests/smoke_blocking_approval.py"])
    run_child("shared-approval smoke test", ["tests/smoke_shared_approval.py"])
    run_child("multi-plan smoke test", ["tests/smoke_multi_plan.py"])
    run_child("SSE transport smoke test", ["tests/smoke_sse.py"])
    run_child("gate-and-run smoke test", ["tests/smoke_gate_and_run.py"])

    failures = [label for label, ok, _ in _results if not ok]
    print("\n" + "=" * 68)
    if failures:
        print(f"NO-GO - {len(failures)} check(s) failed:")
        for label in failures:
            print(f"  - {label}")
        print("\nSee docs/deployment-airgap-manual.md section 8, and")
        print("docs/phase4-testing-matrix.md Part E, before registering with AnythingLLM.")
        return 1
    print("GO - this copy is intact and works on this machine.")
    print("\nNext: register the server in AnythingLLM (deployment manual section 6),")
    print("paste the agent prompt from docs/phase3-anythingllm-agent-prompt.md,")
    print("set temperature <= 0.3, then run acceptance test A1.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
