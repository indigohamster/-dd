"""Unified DingTalk notifications for user-visible ticket status changes."""

from modules.ticket import TicketFormatter


class TicketStatusNotifier:
    def __init__(self, dingtalk_api=None, logger=None):
        self.api = dingtalk_api
        self.logger = logger

    def notify(self, ticket, action, operator_nick="", detail="") -> bool:
        if not ticket or not self.api:
            return False
        recipient = ticket.get("creator_staff_id") or ticket.get("creator_id")
        if not recipient:
            return False
        title, body = TicketFormatter.format_user_status_notification(
            ticket, action, operator_nick, detail,
        )
        try:
            self.api.send_markdown_to_user(recipient, title, body)
            if self.logger:
                self.logger.info(f"工单状态通知已发送: {ticket['ticket_id']} action={action}")
            return True
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"工单状态通知发送失败: {ticket['ticket_id']}: {exc}")
            return False
