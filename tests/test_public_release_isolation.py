"""Git-provenance release guard regression tests (no Home Assistant dependencies)."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.check_public_release_isolation import hosted_commits_in_candidate


class PublicReleaseIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "CI Test")
        self.git("config", "user.email", "ci@example.invalid")
        self.commit("initial.txt", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("branch", "integration/hosted-v1")
        self.git("update-ref", "refs/remotes/origin/main", "main")
        self.git("update-ref", "refs/remotes/origin/integration/hosted-v1", "integration/hosted-v1")

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.strip()

    def commit(self, name: str, content: str) -> str:
        (self.repo / name).write_text(content)
        self.git("add", name)
        self.git("commit", "-q", "-m", f"update {name}")
        return self.git("rev-parse", "HEAD")

    def hosted_commit(self) -> str:
        self.git("checkout", "-q", "integration/hosted-v1")
        result = self.commit("ordinary_module.py", "token_handling = True\n")
        self.git("update-ref", "refs/remotes/origin/integration/hosted-v1", result)
        return result

    def check(self, *, candidate: str, base: str | None = None) -> set[str]:
        return hosted_commits_in_candidate(
            self.repo,
            candidate=candidate,
            base=base or self.base,
            hosted="refs/remotes/origin/integration/hosted-v1",
        )

    def test_public_only_change_passes_regardless_of_filename(self) -> None:
        self.hosted_commit()
        self.git("checkout", "-q", "main")
        candidate = self.commit("hosted_sounding_filename.py", "local calendar helper\n")
        self.assertEqual(self.check(candidate=candidate), set())

    def test_hosted_commit_detected_regardless_of_filename(self) -> None:
        hosted = self.hosted_commit()
        self.assertEqual(self.check(candidate=hosted), {hosted})

    def test_merging_hosted_branch_into_public_fails(self) -> None:
        hosted = self.hosted_commit()
        self.git("checkout", "-q", "main")
        self.commit("public.txt", "public")
        self.git("merge", "--no-ff", "-q", "-m", "merge hosted", "integration/hosted-v1")
        self.assertEqual(self.check(candidate="HEAD"), {hosted})

    def test_public_commit_synced_into_hosted_is_not_flagged(self) -> None:
        self.git("checkout", "-q", "main")
        public_sha = self.commit("public_feature.py", "calendar UI\n")
        self.git("checkout", "-q", "integration/hosted-v1")
        self.git("merge", "--ff-only", "main")
        self.git("update-ref", "refs/remotes/origin/integration/hosted-v1", "HEAD")
        self.assertEqual(self.check(candidate=public_sha, base=public_sha), set())

    def test_push_uses_previous_public_main_commit_as_base(self) -> None:
        hosted = self.hosted_commit()
        self.git("checkout", "-q", "main")
        self.git("merge", "--ff-only", "integration/hosted-v1")
        self.assertEqual(self.check(candidate="HEAD", base=self.base), {hosted})

    def test_missing_ref_fails_closed(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            hosted_commits_in_candidate(
                self.repo,
                candidate="HEAD",
                base=self.base,
                hosted="refs/remotes/origin/nonexistent",
            )


if __name__ == "__main__":
    unittest.main()
