"""Keep landing-tool merge authority exclusive (DOCKET-2026-10-16-15).

Run with: python3 -B -m unittest discover -s tests -v
"""

from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
# Match shell commands and quoted argument sequences, including multiline lists
# and shell continuations. Inspect source without executing workflows or scripts.
SEPARATOR = r"(?:\s|,|\\\r?\n)+"
GH = r"(?<![\w-])['\"]?gh['\"]?"
CURL = r"(?<![\w-])['\"]?curl['\"]?"
# gh's repo selector may sit between `gh` and `pr merge`: -R X, --repo X, --repo=X.
REPO_FLAG = (
    r"(?:" + SEPARATOR + r"['\"]?(?:-R|--repo)['\"]?(?:" + SEPARATOR + r"|=)"
    + r"['\"]?[^\s'\",]+['\"]?)?"
)
PUT = r"(?:(?:-X|--request|--method)(?:" + SEPARATOR + r"|=)|-X)['\"]?PUT['\"]?"
# /pulls/<number or interpolated name>/merge, e.g. /pulls/5/merge, /pulls/${PR}/merge.
PULL_MERGE_URL = r"/pulls/[^\s'\",/]+/merge(?![\w-])"
MERGE_COMMAND = re.compile(
    GH
    + REPO_FLAG
    + SEPARATOR
    + r"['\"]?pr['\"]?"
    + SEPARATOR
    + r"['\"]?merge(?![\w-])"
    + "|(?:" + GH + SEPARATOR + r"['\"]?api['\"]?" + "|" + CURL + ")"
    + r"[^\n]*?"
    + PUT
    + r"[^\n]*?"
    + PULL_MERGE_URL
)


def merge_command_paths(root: Path) -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    ).stdout.decode("utf-8").split("\0")
    guard = Path(__file__).resolve().relative_to(root)
    return [
        Path(name) for name in sorted(filter(None, tracked))
        if Path(name) != guard
        and (root / name).is_file()
        and MERGE_COMMAND.search((root / name).read_text(encoding="utf-8", errors="replace"))
    ]


class MergePathRetirementTests(unittest.TestCase):
    def test_label_merge_workflow_is_retired(self):
        self.assertFalse(
            (ROOT / ".github/workflows/director-approved-merge.yml").exists(),
            "The director-approved merge workflow must stay retired",
        )

    def test_sources_have_no_github_cli_merge_command(self):
        self.assertEqual(merge_command_paths(ROOT), [])

    def test_command_detection_controls(self):
        for source in (
            'gh pr merge "$PR_NUMBER" --squash',
            'gh \\\n  pr \\\n  merge 123',
            'subprocess.run(["gh", "pr", "merge", pr], check=True)',
            "subprocess.run(['gh', 'pr', 'merge', pr])",
            '["gh",\n "pr",\n "merge"]',
            'gh --repo OWNER/REPO pr merge 42',
            'gh api -X PUT repos/OWNER/REPO/pulls/42/merge',
            'gh pr merge "${PR_NUMBER}" --repo "${REPO}" --squash',
            'gh -R OWNER/REPO pr merge 42',
            'curl -X PUT https://api.github.com/repos/O/R/pulls/5/merge',
            'gh api --method=PUT repos/O/R/pulls/5/merge',
            'gh api --method PUT "repos/${REPO}/pulls/${PR_NUMBER}/merge"',
            'curl -X PUT "$API/repos/O/R/pulls/$PR/merge"',
        ):
            with self.subTest(source=source):
                self.assertIsNotNone(MERGE_COMMAND.search(source))
        for source in (
            'gh pr view 123',
            '["gh", "pr", "comment", pr]',
            'gh pr merge-base',
            'other-gh pr merge 123',
        ):
            with self.subTest(source=source):
                self.assertIsNone(MERGE_COMMAND.search(source))


if __name__ == "__main__":
    unittest.main()
