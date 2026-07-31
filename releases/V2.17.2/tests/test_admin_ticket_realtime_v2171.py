import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AdminTicketRealtimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (ROOT / "admin_ui" / "app.js").read_text(encoding="utf-8")
        cls.shell_js = (ROOT / "admin_ui" / "console-shell.js").read_text(encoding="utf-8")

    def test_ticket_loader_supports_non_disruptive_refresh(self):
        self.assertIn("async function loadTickets(filter,options={})", self.app_js)
        self.assertIn("const silent=options.silent===true", self.app_js)
        self.assertIn("{cache:'no-store'}", self.app_js)

    def test_only_visible_ticket_view_is_polled(self):
        self.assertIn("document.hidden||currentView()!=='tickets'", self.shell_js)
        self.assertIn("loadTickets(undefined,{silent:true})", self.shell_js)
        self.assertIn("},10000)", self.shell_js)


if __name__ == "__main__":
    unittest.main()
