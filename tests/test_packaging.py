"""The transfer package: what goes into it, and what its manifests say.

The package is the only thing that reaches the corporate PC, so a file the README tells
the operator to use has to be in it. 3.0.0 moved the agent prompt out of the README and
into agents.md - which the package did not ship. Found while running the packaging
script, not by a test; these tests are the ones that would have found it.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("make_package", ROOT / "tools" / "make_package.py")
make_package = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_package)


def packaged() -> set[str]:
    return {p.relative_to(ROOT).as_posix() for p in make_package.collect()}


class TestWhatIsPackaged(unittest.TestCase):
    def test_the_agent_prompt_is_in_the_package(self):
        """The README names agents.md as the file to paste; it must travel with it."""
        self.assertIn("[agents.md](agents.md)", (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("agents.md", packaged())

    def test_the_licence_is_in_the_package(self):
        """Its own terms: the notice is included in all copies. The package is the copy
        that reaches the corporate PC, so a package without it breaks the licence."""
        text = (ROOT / "LICENSE.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("MIT License\n\nCopyright (c) "))
        self.assertIn("LICENSE.md", packaged())

    def test_every_file_the_readme_links_to_is_in_the_package(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        links = set(re.findall(r"\]\(((?:docs|tools|planning|tests)/[^)#]+|[\w.-]+\.(?:md|json))\)",
                               readme))
        self.assertIn("docs/phase3-anythingllm-agent-prompt.md", links)
        missing = sorted(link for link in links if link not in packaged())
        self.assertEqual(missing, [])

    def test_every_module_and_test_is_in_the_package(self):
        files = packaged()
        for folder in ("planning", "tests", "tools"):
            for path in (ROOT / folder).glob("*.py"):
                self.assertIn(f"{folder}/{path.name}", files)
        self.assertIn("server.py", files)

    def test_nothing_private_is_in_the_package(self):
        for path in packaged():
            parts = set(path.split("/"))
            self.assertFalse(parts & {"state", "dist", ".git", "__pycache__", "runtime"}, path)
        self.assertNotIn("CLAUDE.md", packaged())


class TestManifests(unittest.TestCase):
    """The archive's manifest and the repository's describe two different trees."""

    FILES = [ROOT / "server.py", ROOT / "agents.md"]
    RUNTIME = "0" * 64 + "  1234567  runtime/python-3.12.10-embed-amd64.zip"

    def paths(self, text: str) -> list[str]:
        return [line.split(None, 2)[2] for line in text.splitlines()
                if line and not line.startswith("#")]

    def test_the_repository_manifest_never_names_the_bundled_runtime(self):
        """It used to: --with-python wrote the archive's manifest over the repo's, and
        verify_install then failed here on a runtime/ zip the working tree does not have."""
        repo = make_package.manifest_text(self.FILES, "2026-10-03T00:00:00+09:00")
        self.assertEqual(self.paths(repo), ["server.py", "agents.md"])

    def test_the_archive_manifest_does(self):
        archive = make_package.manifest_text(self.FILES, "2026-10-03T00:00:00+09:00",
                                             self.RUNTIME)
        self.assertEqual(self.paths(archive),
                         ["server.py", "agents.md", "runtime/python-3.12.10-embed-amd64.zip"])

    def test_they_agree_on_every_source_file(self):
        repo = make_package.manifest_text(self.FILES, "t")
        archive = make_package.manifest_text(self.FILES, "t", self.RUNTIME)
        self.assertEqual(archive, repo + self.RUNTIME + "\n")

    def test_a_line_is_what_verify_install_reads(self):
        """sha256, size, path - three fields, the path last (it may contain spaces)."""
        line = make_package.manifest_text(self.FILES, "t").splitlines()[-1]
        digest, size, rel = line.split(None, 2)
        self.assertEqual((len(digest), rel), (64, "agents.md"))
        self.assertEqual(int(size), (ROOT / "agents.md").stat().st_size)
        self.assertEqual(digest, make_package.sha256(ROOT / "agents.md"))


if __name__ == "__main__":
    unittest.main()
