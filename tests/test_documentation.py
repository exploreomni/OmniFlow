import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
REQUIRED_DOCUMENTS = [
    "docs/INSTALLATION.md",
    "docs/CONFIGURATION.md",
    "docs/TESTING.md",
    "docs/TROUBLESHOOTING.md",
    "docs/SECURITY_MODEL.md",
    "docs/LIMITATIONS.md",
    "docs/ARCHITECTURE.md",
    "docs/AI_REPAIR.md",
    "docs/DBT_SYNC.md",
    "SUPPORT.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "LICENSE",
]


class DocumentationTests(unittest.TestCase):
    def test_required_documents_exist_and_are_linked_from_readme(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for relative in REQUIRED_DOCUMENTS:
            self.assertTrue((ROOT / relative).is_file(), msg=f"Missing required document: {relative}")
            self.assertIn(relative, readme, msg=f"README does not link to {relative}")

    def test_internal_markdown_links_resolve(self):
        for document in sorted(ROOT.rglob("*.md")):
            if any(
                part in {".git", ".venv", "build", "dist"} or part.endswith(".egg-info")
                for part in document.parts
            ):
                continue
            text = document.read_text(encoding="utf-8")
            for raw_target in MARKDOWN_LINK_RE.findall(text):
                target = raw_target.strip().strip("<>")
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                path_text = target.split("#", 1)[0]
                if not path_text:
                    continue
                resolved = (document.parent / path_text).resolve()
                self.assertTrue(resolved.exists(), msg=f"Broken link in {document.relative_to(ROOT)}: {target}")

    def test_installation_guide_covers_the_customer_lifecycle(self):
        text = (ROOT / "docs/INSTALLATION.md").read_text(encoding="utf-8")
        for step in range(1, 12):
            self.assertIn(f"## Step {step}:", text)
        for required_text in [
            ".omni/flow.json",
            ".github/workflows/omniflow.yml",
            "OMNI_API_KEY",
            "full 40-character OmniFlow commit SHA",
            "Create pull request",
            "Protect The Base Branch",
            "Verify The Installation",
            "OMNIFLOW_UPLOAD_SARIF",
            "Team or Enterprise",
        ]:
            self.assertIn(required_text, text)
        self.assertLess(text.index("Protect The Base Branch"), text.index("Create And Store A Dedicated Omni PAT"))
        self.assertIn("Do not use an Organization API Key", text)
        self.assertIn("omni-api-key", text)
        self.assertNotIn("version input", text)
        self.assertIn("Step 10: Optionally Add Post-Deployment dbt Sync", text)
        self.assertIn("Step 11: Do Not Install AI Repair Yet", text)

    def test_ai_repair_guide_documents_every_required_safety_gate(self):
        text = (ROOT / "docs/AI_REPAIR.md").read_text(encoding="utf-8")
        for required_text in (
            "OMNIFLOW_REPAIR_API_KEY",
            "allow_query_execution: true",
            "omniflow-ai-repair",
            "protected GitHub environment",
            "same-repository",
            "one repair attempt",
            "does not approve, merge, or deploy",
            "does not document a no-query",
            "rollback_failed",
            "require_branch_exists: true",
            "non-production model",
            "Release blocked",
        ):
            self.assertIn(required_text, text)

    def test_documentation_is_included_in_source_distribution(self):
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("include SUPPORT.md", manifest)
        self.assertIn("recursive-include docs *.md", manifest)

    def test_workflow_example_has_two_pinned_action_placeholders(self):
        workflow = (ROOT / ".github/workflow-examples/omniflow.yml").read_text(encoding="utf-8")
        self.assertEqual(workflow.count("exploreomni/OmniFlow@<pinned-commit-sha>"), 2)
        repair = (ROOT / ".github/workflow-examples/omniflow-ai-repair.yml").read_text(encoding="utf-8")
        self.assertEqual(repair.count("exploreomni/OmniFlow@<pinned-commit-sha>"), 1)
        sync = (ROOT / ".github/workflow-examples/omniflow-dbt-sync.yml").read_text(encoding="utf-8")
        self.assertEqual(sync.count("exploreomni/OmniFlow@<pinned-commit-sha>"), 1)


if __name__ == "__main__":
    unittest.main()
