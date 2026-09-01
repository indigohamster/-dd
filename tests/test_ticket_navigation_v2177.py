import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "admin_ui" / "app.js").read_text(encoding="utf-8")
STYLES = (ROOT / "admin_ui" / "styles-v21.css").read_text(encoding="utf-8")


class TicketNavigationV2177Tests(unittest.TestCase):
    def test_shared_ticket_link_opens_existing_detail(self):
        self.assertIn("function openTicketFromAnywhere(ticketId)", APP_JS)
        self.assertIn("function ticketLinkMarkup(ticketId", APP_JS)
        self.assertIn("showTicket(id)", APP_JS)

    def test_all_ticket_surfaces_use_shared_link(self):
        expected = (
            "ticketLinkMarkup(x.ticket_id)",
            "ticketLinkMarkup(issue.ticket_id)",
            "ticketLinkMarkup(k.source_ticket_id)",
            "ticketLinkMarkup(x.detail)",
        )
        for marker in expected:
            with self.subTest(marker=marker):
                self.assertIn(marker, APP_JS)
        self.assertGreaterEqual(APP_JS.count("ticketLinkMarkup(x.ticket_id)"), 4)

    def test_ticket_links_have_visible_keyboard_focus(self):
        self.assertIn(".ticket-link{", STYLES)
        self.assertIn(".ticket-link:focus-visible", STYLES)


if __name__ == "__main__":
    unittest.main()
