"""Thread-safe, process-local state for a single bot instance."""

import threading
import time


class ConversationStateStore:
    """Own short-lived user conversation state outside the message handler."""

    def __init__(self, feedback_ttl=1800, attachment_ttl=3600, engine_ttl=1800):
        self.feedback_ttl = int(feedback_ttl)
        self.attachment_ttl = int(attachment_ttl)
        self.engine_ttl = int(engine_ttl)
        self._feedback = {}
        self._attachments = {}
        self._engines = {}
        self._knowledge_events = {}
        self._spreadsheets = {}
        self._lock = threading.RLock()

    @staticmethod
    def user_key(info):
        conv_id = str(info.get("conv_id") or "")
        sender_id = str(info.get("sender_id") or info.get("staff_id") or "")
        return f"{conv_id}\x1f{sender_id}" if sender_id else conv_id

    def set_feedback(self, key, value):
        with self._lock:
            self._feedback[key] = {**value, "timestamp": time.time()}

    def get_feedback(self, key):
        with self._lock:
            value = self._feedback.get(key)
            if not value:
                return None
            if time.time() - value["timestamp"] > self.feedback_ttl:
                self._feedback.pop(key, None)
                return None
            return dict(value)

    def pop_feedback(self, key):
        with self._lock:
            return self._feedback.pop(key, None)

    def replace_attachment(self, key, value):
        with self._lock:
            previous = self._attachments.pop(key, None)
            self._attachments[key] = {**value, "timestamp": time.time()}
            return previous

    def pop_attachment(self, key):
        with self._lock:
            return self._attachments.pop(key, None)

    def pop_expired_attachments(self):
        now = time.time()
        with self._lock:
            keys = [
                key for key, value in self._attachments.items()
                if now - value.get("timestamp", 0) > self.attachment_ttl
            ]
            return [(key, self._attachments.pop(key)) for key in keys]

    def set_engine(self, key, engine):
        with self._lock:
            self._engines[key] = {"engine": engine, "timestamp": time.time()}

    def get_engine(self, key):
        with self._lock:
            value = self._engines.get(key)
            if not value:
                return None
            if time.time() - value["timestamp"] > self.engine_ttl:
                self._engines.pop(key, None)
                return None
            value["timestamp"] = time.time()
            return value["engine"]

    def pop_engine(self, key):
        with self._lock:
            return self._engines.pop(key, None)

    def set_knowledge_event(self, key, event_id):
        with self._lock:
            self._knowledge_events[key] = event_id

    def pop_knowledge_event(self, key):
        with self._lock:
            return self._knowledge_events.pop(key, None)

    def replace_spreadsheet(self, key, value):
        with self._lock:
            previous = self._spreadsheets.pop(key, None)
            self._spreadsheets[key] = {**value, "timestamp": time.time()}
            return previous

    def append_spreadsheet(self, key, value, max_files=2):
        """Keep up to two uploaded files so a user can request a comparison."""
        with self._lock:
            current = self._spreadsheets.get(key) or {"files": [], "timestamp": time.time()}
            expired = time.time() - current.get("timestamp", 0) > self.attachment_ttl
            files = [] if expired else list(current.get("files") or [])
            removed = list(current.get("files") or []) if expired else []
            files.append(value)
            removed.extend(files[:-max_files])
            self._spreadsheets[key] = {"files": files[-max_files:], "timestamp": time.time()}
            return removed

    def get_spreadsheet(self, key):
        with self._lock:
            value = self._spreadsheets.get(key)
            if not value:
                return None
            if time.time() - value["timestamp"] > self.attachment_ttl:
                # Keep the paths until the cleanup owner can delete the files.
                return None
            return dict(value)

    def pop_spreadsheet(self, key):
        with self._lock:
            return self._spreadsheets.pop(key, None)

    def pop_expired_spreadsheets(self):
        now = time.time()
        with self._lock:
            keys = [
                key for key, value in self._spreadsheets.items()
                if now - value.get("timestamp", 0) > self.attachment_ttl
            ]
            return [(key, self._spreadsheets.pop(key)) for key in keys]
