"""Unified DingTalk notifications for user-visible ticket status changes."""

from modules.ticket import TicketFormatter


class TicketStatusNotifier:
    def __init__(self, dingtalk_api=None, logger=None, store=None):
        self.api = dingtalk_api
        self.logger = logger
        self.store = store

    def notify(self, ticket, action, operator_nick="", detail="") -> bool:
        if not ticket:
            return False
        recipient = ticket.get("creator_staff_id") or ticket.get("creator_id")
        if not recipient:
            return False
        delivery_id = self.store.create_delivery(ticket, recipient, action, operator_nick, detail) if self.store else None
        return self._send(ticket, action, operator_nick, detail, recipient, delivery_id)

    def _send(self, ticket, action, operator_nick, detail, recipient, delivery_id=None):
        if not self.api:
            if self.store and delivery_id:
                self.store.update_delivery(delivery_id, "failed", "钉钉 OpenAPI 未配置")
            return False
        title, body = TicketFormatter.format_user_status_notification(
            ticket, action, operator_nick, detail,
        )
        try:
            self.api.send_markdown_to_user(recipient, title, body)
            if self.store and delivery_id:
                self.store.update_delivery(delivery_id, "sent")
            if self.logger:
                self.logger.info(f"工单状态通知已发送: {ticket['ticket_id']} action={action}")
            return True
        except Exception as exc:
            if self.store and delivery_id:
                self.store.update_delivery(delivery_id, "failed", str(exc))
            if self.logger:
                self.logger.warning(f"工单状态通知发送失败: {ticket['ticket_id']}: {exc}")
            return False

    def retry(self, delivery_id):
        if not self.store:
            return False
        delivery = self.store.get_delivery(delivery_id)
        if not delivery:
            return False
        payload = self.store.decode_payload(delivery)
        return self._send(payload["ticket"], payload["action"], payload.get("operator_nick", ""),
                          payload.get("detail", ""), delivery["recipient"], delivery_id)
