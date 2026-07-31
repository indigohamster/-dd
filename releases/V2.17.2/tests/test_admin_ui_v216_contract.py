import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "admin_ui" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "admin_ui" / "app.js").read_text(encoding="utf-8")
SHELL_JS = (ROOT / "admin_ui" / "console-shell.js").read_text(encoding="utf-8")


class AdminUiV216ContractTests(unittest.TestCase):
    def test_five_primary_entries_and_all_legacy_views_remain_present(self):
        nav_views = re.findall(r'class="nav[^\"]*"[^>]*data-view="([^"]+)"', HTML)
        self.assertEqual(nav_views, ["overview", "tickets", "knowledge", "files", "settings"])
        view_ids = set(re.findall(r'<section class="view(?: active)?" id="([^"]+)"', HTML))
        self.assertEqual(len(view_ids), 17)

    def test_module_switches_drive_shell_visibility_but_not_settings(self):
        self.assertIn('data-view="tickets" data-icon="▤" data-module-any="tickets"', HTML)
        self.assertIn('data-view="knowledge" data-icon="◇" data-module-any="knowledge"', HTML)
        self.assertIn('data-view="files" data-icon="▣" data-module-any="spreadsheet"', HTML)
        self.assertNotRegex(HTML, r'data-view="settings"[^>]+data-module-any')
        self.assertIn("function applyModuleVisibility()", SHELL_JS)
        self.assertIn("if(!viewEnabled(view))", SHELL_JS)

    def test_ai_provider_change_drops_key_and_none_drops_vision(self):
        self.assertIn("previous&&previous!==provider", SHELL_JS)
        self.assertIn("delete setupDraft.ai.api_key", SHELL_JS)
        self.assertIn("setupDraft.modules.vision=false", SHELL_JS)
        self.assertIn("renderSetupModules();updateSetupAiFields()", SHELL_JS)
        self.assertIn("$('llm-key').dataset.provider", APP_JS)

    def test_first_step_can_import_a_safe_handoff_package(self):
        import_position = HTML.index('id="setup-config-package-file"')
        second_step_position = HTML.index('data-setup-page="2"')
        self.assertLess(import_position, second_step_position)
        self.assertIn("/api/config-package/import/preview", SHELL_JS)
        self.assertIn("/api/config-package/import/apply", SHELL_JS)

    def test_setup_step_is_saved_and_no_script_has_guidance(self):
        self.assertIn("async function saveSetupDraft(step=setupStep)", SHELL_JS)
        self.assertIn("draft.step=", SHELL_JS)
        self.assertIn("await saveSetupDraft(next)", SHELL_JS)
        self.assertIn("<noscript>", HTML)


if __name__ == "__main__":
    unittest.main()
