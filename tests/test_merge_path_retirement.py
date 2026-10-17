"""Keep landing-tool merge authority exclusive (DOCKET-2026-10-16-15).

Run with: python3 -B -m unittest discover -s tests -v
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
# Match shell commands and quoted argument sequences, including multiline lists
# and shell continuations. Inspect source without executing workflows or scripts.
SEPARATOR = r"(?:\s|,|\\\r?\n)+"
MERGE_COMMAND = re.compile(
    r"(?<![\w-])['\"]?gh['\"]?"
    + SEPARATOR
    + r"['\"]?pr['\"]?"
    + SEPARATOR
    + r"['\"]?merge(?![\w-])"
)


def merge_command_paths(root: Path) -> list[Path]:
    workflows = root / ".github" / "workflows"
    sources = sorted(
        path for path in workflows.rglob("*")
        if path.is_file() and path.suffix in {".yml", ".yaml"}
    )
    sources.append(root / ".github" / "scripts" / "llm_pr_review.py")
    return [
        path.relative_to(root) for path in sources
        if MERGE_COMMAND.search(path.read_text(encoding="utf-8"))
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
