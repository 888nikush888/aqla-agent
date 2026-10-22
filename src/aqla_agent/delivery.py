"""Frozen versioned notification outbox. The native worker alone owns retries."""
from __future__ import annotations

from aqla_hyperliquid_mcp.models import AqlaError, digest, now_ms

from .notifications import acknowledge_notifications


def text_chunks(text, maximum=3000):
    chunks, current, size = [], [], 0
    for character in text:
        width = 2 if ord(character) > 0xffff else 1
        if size + width > maximum:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(character)
        size += width
    if current:
        chunks.append("".join(current))
    return chunks


class NotificationOutbox:
    def __init__(self, state, owner):
        self.state, self.owner = state, owner
        self.generation = None

    def bind(self, generation):
        if generation != self.generation:
            # A sending item from an earlier native process may already have reached Telegram.
            def recover(data):
                for row in data["notification_outbox"].values():
                    if row["status"] == "sending":
                        row.update(status="exhausted" if row["attempts"] >= 5 else "pending", uncertain=True, next_attempt_at=0)
            self.state.update_metadata_if_changed(("notification_outbox",), recover)
            self.generation = generation

    def prepare(self):
        from .messages import telegram_view
        if not self.state.read("notifications").get("notifications"):
            return
        snapshot = self.state.snapshot()
        existing = snapshot.get("notification_outbox", {})
        covered = {pair for row in existing.values() for pair in row["versions"].items()}
        snapshot["notifications"] = {identity: notice for identity, notice in snapshot["notifications"].items()
                                     if (identity, digest(notice)) not in covered}
        # Use the existing full renderer internally, not a possibly overflowed model-facing page.
        view = telegram_view(snapshot, {}, include_status=False)
        fresh = []
        for message in view["messages"]:
            versions = message["notification_versions"]
            chunks = [chunk for text in message["texts"] for chunk in text_chunks(text)]
            identity = digest([self.owner, snapshot["account"], snapshot["network"], versions, chunks, "aqla-delivery-v1"])
            fresh.append({"delivery_id": identity, "owner": self.owner, "account": snapshot["account"],
                          "network": snapshot["network"], "versions": versions, "chunks": chunks,
                          "severity": message["severity"], "status": "pending", "sent_parts": [],
                          "attempts": 0, "created_at": now_ms(), "next_attempt_at": 0, "uncertain": False})
        if fresh:
            def create(data):
                for row in fresh:
                    data["notification_outbox"].setdefault(row["delivery_id"], row)
            self.state.update_metadata(("notification_outbox",), create)

    def next_part(self, generation):
        if generation != self.generation:
            raise AqlaError("Native delivery owner changed")
        self.prepare()
        def select(data):
            rows = sorted(data["notification_outbox"].values(),
                key=lambda row: (row["severity"] not in ("urgent", "critical", "error"), row["created_at"], row["delivery_id"]))
            for row in rows:
                if row["status"] == "sending" and row.get("send_lease_until", 0) <= now_ms():
                    row.update(status="exhausted" if row["attempts"] >= 5 else "pending", uncertain=True, next_attempt_at=0)
                if row["status"] != "pending" or row["next_attempt_at"] > now_ms() or row["attempts"] >= 5:
                    continue
                if row["owner"] != self.owner:
                    raise AqlaError("Notification recipient differs from configured owner")
                part = next((i for i in range(len(row["chunks"])) if i not in row["sent_parts"]), None)
                if part is None:
                    continue
                row.update(status="sending", sending_part=part, attempts=row["attempts"] + 1, send_lease_until=now_ms() + 30000)
                text = row["chunks"][part]
                if row["uncertain"]:
                    text = "⚠️ Wiederhergestellte Zustellung – diese Meldung könnte bereits angekommen sein.\n\n" + text
                return {"delivery_id": row["delivery_id"], "part": part, "text": text}
            return {"delivery_id": None}
        return self.state.update_metadata_if_changed(("notification_outbox",), select)

    def complete(self, generation, identity, part, success):
        if generation != self.generation:
            raise AqlaError("Native delivery owner changed")
        def commit(data):
            row = data["notification_outbox"].get(identity)
            if row is not None and part in row["sent_parts"]:
                return {"status": row["status"], "already_confirmed": True}
            if row is None or row["status"] != "sending" or row.get("sending_part") != part:
                raise AqlaError("Notification send does not match its frozen part")
            if success:
                row["sent_parts"].append(part)
                row.update(status="pending", attempts=0, uncertain=False, next_attempt_at=0)
                if len(row["sent_parts"]) == len(row["chunks"]):
                    row["status"] = "sent"
                    # Persisted success and the exact-version facts ACK are ONE state transaction.
                    acknowledge_notifications(data, list(row["versions"]), row["versions"])
            else:
                row.update(status="exhausted" if row["attempts"] >= 5 else "pending", uncertain=True,
                           next_attempt_at=now_ms() + min(300000, 30000 * 2 ** row["attempts"]))
            row.pop("sending_part", None)
            row.pop("send_lease_until", None)
            return {"status": row["status"]}
        return self.state.update_metadata(("notification_outbox", "notifications"), commit)

    def retry(self, generation):
        if generation != self.generation:
            raise AqlaError("Native delivery owner changed")
        def requeue(data):
            count = 0
            for row in data["notification_outbox"].values():
                if row["status"] == "exhausted":
                    row.update(status="pending", attempts=0, next_attempt_at=0)
                    count += 1
            return {"requeued": count}
        return self.state.update_metadata(("notification_outbox",), requeue)

    def overview(self):
        rows = self.state.read("notification_outbox").get("notification_outbox", {}).values()
        return {status: sum(row["status"] == status for row in rows)
                for status in ("pending", "sending", "sent", "exhausted")}
