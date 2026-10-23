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
        from .notification_policy import select
        self.prepare_accounting()
        if not self.state.read("notifications").get("notifications"):
            return
        snapshot = self.state.snapshot()
        existing = snapshot.get("notification_outbox", {})
        covered = {pair for row in existing.values() for pair in row["versions"].items()}
        messages, suppressed, deferred = select(snapshot, covered=covered)
        fresh = []
        for message in messages:
            versions = message["versions"]
            chunks = text_chunks(message["text"])
            identity = digest([self.owner, snapshot["account"], snapshot["network"], versions, chunks, "aqla-delivery-v2"])
            fresh.append({"delivery_id": identity, "owner": self.owner, "account": snapshot["account"],
                          "network": snapshot["network"], "versions": versions, "chunks": chunks,
                          "severity": message["severity"], "status": "pending", "sent_parts": [],
                          "attempts": 0, "created_at": now_ms(), "next_attempt_at": 0, "uncertain": False,
                          **{key: value for key, value in message.items() if key in ("event_keys", "fill_keys", "incident", "incident_signature", "resolved", "trade_id", "closed_notice", "accounting_fingerprint")}})
        if fresh or suppressed or deferred:
            def create(data):
                policy = data.setdefault("notice_policy_state", {})
                policy.setdefault("incident_since", {}).update(deferred)
                for row in fresh:
                    data["notification_outbox"].setdefault(row["delivery_id"], row)
                    policy["seen_fills"] = sorted(set(policy.get("seen_fills", [])) | set(row.get("fill_keys", [])))
                for identity, (version, reason) in suppressed.items():
                    current = data["notifications"].get(identity)
                    if current is None or digest(current) != version:
                        continue
                    data["notice_history"][identity + ":" + version] = {"notice": current, "version": version,
                        "disposition": "dashboard_only", "reason": reason, "recorded_at": now_ms()}
                    del data["notifications"][identity]
            self.state.update_metadata_if_changed(("notification_outbox", "notifications", "notice_history", "notice_policy_state"), create)

    def prepare_accounting(self):
        outbox = self.state.read("notification_outbox").get("notification_outbox", {})
        originals = [row for row in outbox.values() if row.get("closed_notice") and row["status"] == "sent" and row.get("message_id")]
        if not originals:
            return
        from .monthly import report_row
        from .notification_policy import compact_event
        snapshot = self.state.read("trades", "source_names", "profiles")
        queued = []
        for original in originals:
            trade = snapshot["trades"].get(original["trade_id"])
            if trade is None or trade["status"] != "closed":
                continue
            report = report_row(snapshot, trade)
            version = digest(report)
            if not report["complete"] or version == original["accounting_fingerprint"]:
                continue
            identity = digest([original["delivery_id"], version, "closed-accounting-edit"])
            if identity in outbox:
                continue
            queued.append({"delivery_id": identity, "owner": self.owner, "account": original["account"], "network": original["network"],
                "versions": {}, "chunks": [compact_event(snapshot, {}, trade, "closed")], "severity": "normal", "status": "pending", "sent_parts": [],
                "attempts": 0, "created_at": now_ms(), "next_attempt_at": 0, "uncertain": False,
                "edit_message_id": original["message_id"], "trade_id": original["trade_id"], "accounting_fingerprint": version})
        if queued:
            self.state.update_metadata_if_changed(("notification_outbox",), lambda data: [data["notification_outbox"].setdefault(row["delivery_id"], row) for row in queued])

    @staticmethod
    def delivery_part(row, part):
        text = row["chunks"][part]
        if row["uncertain"] and not row.get("edit_message_id"):
            text = "⚠️ Wiederhergestellte Zustellung – diese Meldung könnte bereits angekommen sein.\n\n" + text
        return {"delivery_id": row["delivery_id"], "part": part, "text": text,
                **({"edit_message_id": row["edit_message_id"]} if row.get("edit_message_id") else {})}


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
                return self.delivery_part(row, part)
            return {"delivery_id": None}
        return self.state.update_metadata_if_changed(("notification_outbox",), select)

    @staticmethod
    def acknowledge_completed_delivery(data, row, identity):
        row["status"] = "sent"
        if row.get("incident"):
            policy = data.setdefault("notice_policy_state", {})
            policy.setdefault("announced_incidents", {})[row["incident"]] = not row.get("resolved")
            policy.setdefault("incident_signatures", {})[row["incident"]] = row.get("incident_signature")
            if row.get("resolved"):
                policy.setdefault("incident_since", {}).pop(row["incident"], None)
        for notice_id, version in row["versions"].items():
            notice = data["notifications"].get(notice_id)
            if notice and digest(notice) == version:
                data["notice_history"][notice_id + ":" + version] = {"notice": notice, "version": version,
                    "disposition": "sent", "delivery_id": identity, "recorded_at": now_ms()}
        # Persisted success and the exact-version facts ACK are ONE state transaction.
        acknowledge_notifications(data, list(row["versions"]), row["versions"])


    def complete(self, generation, identity, part, success, message_id=None):
        if generation != self.generation:
            raise AqlaError("Native delivery owner changed")
        def commit(data):
            row = data["notification_outbox"].get(identity)
            if row is not None and part in row["sent_parts"]:
                return {"status": row["status"], "already_confirmed": True}
            if row is None or row["status"] != "sending" or row.get("sending_part") != part:
                raise AqlaError("Notification send does not match its frozen part")
            if success:
                if message_id is not None:
                    row["message_id"] = message_id
                row["sent_parts"].append(part)
                row.update(status="pending", attempts=0, uncertain=False, next_attempt_at=0)
                if len(row["sent_parts"]) == len(row["chunks"]):
                    self.acknowledge_completed_delivery(data, row, identity)
            else:
                row.update(status="exhausted" if row["attempts"] >= 5 else "pending", uncertain=True,
                           next_attempt_at=now_ms() + min(300000, 30000 * 2 ** row["attempts"]))
            row.pop("sending_part", None)
            row.pop("send_lease_until", None)
            return {"status": row["status"]}
        return self.state.update_metadata(("notification_outbox", "notifications", "notice_history", "notice_policy_state"), commit)

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
