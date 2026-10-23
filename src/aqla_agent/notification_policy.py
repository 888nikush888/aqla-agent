"""Important events from execution receipts; routine facts remain in the local activity log."""
from __future__ import annotations

from decimal import Decimal

from aqla_hyperliquid_mcp.models import digest, now_ms

from .messages import literal, number

TRANSIENT = {"reconciliation_unavailable", "recovery_timeout"}


def _completed_wave_fills(trade, exit_order, entry):
    observations = trade.get("order_observations", {})
    pair = [observations.get(row["cloid"], {}) for row in (exit_order, entry)]
    if any(row.get("status") not in ("filled", "canceled") or row.get("oid") is None for row in pair):
        return None
    fills = [[fill for fill in trade.get("fill_events", {}).values() if fill["oid"] == row["oid"]] for row in pair]
    sold, bought = [sum((Decimal(fill["sz"]) for fill in group), Decimal(0)) for group in fills]
    if sold > 0 and Decimal(entry["quantity"]) <= bought <= sold:
        return fills[1]

    return None


def completed_waves(trade):
    """Only paired native terminal orders and real fills prove a completed wave."""
    for entry in trade.get("entry_orders", []):
        if entry.get("kind") != "waverider" or entry.get("not_sent"):
            continue
        exit_order = next((row for row in trade.get("exit_orders", []) if row.get("kind") == "waverider"
                           and row.get("cycle") == entry.get("cycle") and not row.get("not_sent")), None)
        if not exit_order:
            continue
        fills = _completed_wave_fills(trade, exit_order, entry)
        if fills is not None:
            yield "wave:" + trade["trade_id"] + ":" + str(entry["cycle"]), fills



def trade_label(trade):
    plan = trade["plan"]
    return (literal(plan["market"]["symbol"]) + " " + plan["side"].upper()
            + " · " + plan["network"].capitalize())


def compact_event(data, _notice, trade, kind, fills=()):
    plan = trade["plan"]
    unit = literal(plan["market"].get("quote_token", "USDC"))
    title = {"entry": "✅ Einstieg", "exit": "💰 Teilabschluss", "closed": "🏁 Geschlossen",
             "wave": "🌊 Waverider-Welle abgeschlossen"}[kind]
    lines = [title + " · " + trade_label(trade)]
    if kind == "entry":
        lines += ["Menge: " + number(trade.get("filled_quantity")) + " · Entry: " + number(trade.get("entry_vwap")) + " " + unit,
                  "SL: " + number(trade.get("stop_price")) + " · Schutzstatus im Menü"]
    elif kind == "closed":
        from .monthly import report_row
        report = report_row(data, trade)
        lines.append("Netto: " + number(report["net_pnl"]) + " " + unit if report["complete"]
                     else "Position geschlossen · Abrechnung noch unvollständig")
        if report["complete"]:
            lines.append("Gebühren: " + number(report["fees"]) + " · Funding: " + number(report["funding"]))
    elif fills:
        quantity = sum((Decimal(row["sz"]) for row in fills), Decimal(0))
        average = sum((Decimal(row["sz"]) * Decimal(row["px"]) for row in fills), Decimal(0)) / quantity
        labels = sorted({"TP" + str(row["index"] + 1) if row["kind"] == "tp" and row.get("index") is not None else row["kind"] for row in fills})
        lines.append(", ".join(labels) + " · " + number(quantity) + " @ " + number(average) + " " + unit)
        lines.append("Eigene Restmenge: " + number(trade.get("remaining_quantity")))
    lines += ["Quelle: " + literal(data.get("source_names", {}).get(trade.get("chat_id"), trade.get("chat_id", "—"))),
              "Details: /aqla · " + trade["trade_id"][:8]]
    return "\n".join(lines)


class _NoticePolicy:
    def __init__(self, data, covered, stamp):
        self.data, self.covered, self.stamp = data, covered, stamp
        self.policy = self.data.get("notice_policy_state", {})
        self.announced = self.policy.get("announced_incidents", {})
        self.seen_fills = set(self.policy.get("seen_fills", []))
        self.used = {key for row in self.data.get("notification_outbox", {}).values() if row["status"] != "suppressed"
                for key in row.get("event_keys", [])}
        self.messages, self.suppressed, self.deferred = [], {}, {}
        self.notifications = self.data.get("notifications", {})
        self.identity, self.version = "", ""
        self.notice: dict = {}
        self.trade: dict | None = None
        self.severity = "normal"
        self.keys: list[str] = []
        self.text: str | None = None
        self.incident: str | None = None
        self.incident_signature: str | None = None
        self.observed = stamp

    def select_notice(self, identity, notice):
        self.identity, self.notice = identity, notice
        self.version = digest(self.notice)
        if (self.identity, self.version) in self.covered:
            return
        self.trade = self.data.get("trades", {}).get(self.notice.get("trade_id"))
        self.severity, self.keys, self.text = self.notice.get("severity", "normal"), [], None
        self.incident, self.incident_signature = None, None
        self.observed = self.notice.get("observed_at", self.stamp)
        if self.is_alarm():
            if not self.alarm_notice():
                return
        elif "recovery_reports" in self.notice or "reports" in self.notice:
            if not self.recovery_notice():
                return
        elif self.notice.get("source_notice"):
            if not self.source_notice():
                return
        elif self.notice.get("monthly_report"):
            self.keys = ["month:" + digest(self.notice["monthly_report"])]
        elif self.trade:
            if not self.trade_notice():
                return
        elif self.notice.get("error") and not self.notice.get("event_id"):
            self.keys = ["system:" + digest(self.notice)]
        else:
            self.suppressed[self.identity] = (self.version, "dashboard_only")
            return
        self.append_notice()

    def is_alarm(self):
        return self.identity.startswith("protection:") or self.severity in ("critical", "urgent", "error") and not ("recovery_reports" in self.notice or "reports" in self.notice)

    def alarm_notice(self):
        account = self.notice.get("account_key") or (self.trade or {}).get("plan", {}).get("account_key")
        self.incident = digest([account]) if account else None
        self.incident_signature = digest([self.identity, self.notice.get("error"), self.notice.get("code"), self.severity])
        if self.incident and self.announced.get(self.incident) and self.policy.get("incident_signatures", {}).get(self.incident) == self.incident_signature:
            self.suppressed[self.identity] = (self.version, "same_active_alarm")
            return False
        self.keys = ["alarm:" + digest([self.identity, self.incident_signature, self.notice.get("generation_id", self.observed)])]
        return True

    def recovery_notice(self):
        from .recovery_notices import safe_report
        reports = self.notice.get("recovery_reports", [fact for row in self.notice.get("reports", []) if (fact := safe_report(row))])
        self.incident = digest(sorted({row.get("account_key", row.get("exchange", "unknown")) for row in reports}))
        self.incident_signature = digest(reports)
        # An announced incident is cleared only by a positively reconciled resolution.
        if self.notice.get("resolved"):
            if not self.announced.get(self.incident):
                self.suppressed[self.identity] = (self.version, "unannounced_recovery")
                return False
            self.keys = ["recovery:" + self.incident + ":" + str(self.observed)]
        elif reports and all(row.get("code") in TRANSIENT for row in reports):
            if not self.transient_recovery(reports):
                return False
        else:
            if self.announced.get(self.incident) and self.policy.get("incident_signatures", {}).get(self.incident) == self.incident_signature:
                self.suppressed[self.identity] = (self.version, "same_active_alarm")
                return False
            self.keys = ["incident:" + self.incident + ":" + self.incident_signature + ":" + str(self.observed)]
            self.severity = "urgent"
        return True

    def transient_recovery(self, reports):
        newer_resolution = any(other.get("resolved") and other.get("observed_at", 0) >= self.observed
            and {row.get("account_key", row.get("exchange")) for row in other.get("recovery_reports", [])}
            >= {row.get("account_key", row.get("exchange")) for row in reports} for other in self.notifications.values())
        if newer_resolution:
            self.suppressed[self.identity] = (self.version, "transient_resolved")
            return False
        first = self.policy.get("incident_since", {}).get(self.incident, self.observed)
        if self.stamp - first < 120000:
            self.deferred[self.incident] = first
            return False
        if self.announced.get(self.incident):
            self.suppressed[self.identity] = (self.version, "incident_already_announced")
            return False
        self.keys = ["incident:" + self.incident + ":" + str(first)]
        return True

    def newer_source_resolution(self, category):
        return any(other.get("source_notice", {}).get("category") == category
            and other["source_notice"].get("code", "").endswith(("recovered", "resolved"))
            and other.get("observed_at", 0) >= self.observed for other in self.notifications.values())

    def source_notice(self):
        code = self.notice["source_notice"].get("code", "")
        category = self.notice["source_notice"].get("category", "reader")
        self.incident = "source:" + category
        self.incident_signature = digest(self.notice["source_notice"])
        if code.endswith(("recovered", "resolved")):
            if not self.announced.get(self.incident):
                self.suppressed[self.identity] = (self.version, "unannounced_source_recovery")
                return False
            self.keys = ["source-recovery:" + self.identity]
        else:
            newer_resolution = self.newer_source_resolution(category)
            if newer_resolution:
                self.suppressed[self.identity] = (self.version, "transient_source_resolved")
                return False
            if self.announced.get(self.incident) and self.policy.get("incident_signatures", {}).get(self.incident) == self.incident_signature:
                self.suppressed[self.identity] = (self.version, "same_active_source_alarm")
                return False
            if code in ("reader_unavailable", "reader_rate_limited", "sources_unavailable"):
                first = self.policy.get("incident_since", {}).get(self.incident, self.observed)
                if self.stamp - first < 120000:
                    self.deferred[self.incident] = first
                    return False
            else:
                self.severity = "urgent"
            self.keys = ["source:" + self.identity]
        return True

    def trade_receipt(self):
        lifecycle = self.notice.get("lifecycle", {}).get("kind")
        current_fills = self.trade.get("fill_events", {})
        fresh = [(key, row) for key, row in current_fills.items() if self.trade["trade_id"] + ":" + key not in self.seen_fills]
        kind = None
        if lifecycle == "entry_fill":
            kind, self.keys = "entry", ["entry:" + self.trade["trade_id"]]
        elif lifecycle == "closed" or self.notice.get("change", {}).get("status") == "closed":
            kind, self.keys = "closed", ["closed:" + self.trade["trade_id"]]
        elif self.notice.get("change", {}).get("status") in ("unknown", "conflicted"):
            self.keys = ["trade-alarm:" + self.trade["trade_id"] + ":" + self.trade["status"]]
            self.severity = "urgent"
        else:
            exits = [(key, row) for key, row in fresh if row["kind"] not in ("entry", "waverider", "waverider_retirement")]
            if exits:
                kind, self.keys = "exit", ["fill:" + self.trade["trade_id"] + ":" + key for key, _ in exits]
                fresh = exits
            else:
                wave = next(((key, fills) for key, fills in completed_waves(self.trade) if key not in self.used), None)
                if wave:
                    kind, self.keys = "wave", [wave[0]]
                    fresh = [(digest(fill), fill) for fill in wave[1]]
        return kind, fresh

    def trade_notice(self):
        kind, fresh = self.trade_receipt()
        if kind:
            if self.stamp - self.observed < 15000:
                return False
            self.text = compact_event(self.data, self.notice, self.trade, kind, [row for _, row in fresh])
        if not self.keys:
            self.suppressed[self.identity] = (self.version, "routine_trade_observation")
            return False
        return True

    def notice_item(self, resolved):
        item = {"versions": {self.identity: self.version}, "text": self.text, "severity": self.severity, "event_keys": self.keys}
        if self.trade:
            item["fill_keys"] = [self.trade["trade_id"] + ":" + key for key in self.trade.get("fill_events", {})]
            item["trade_id"] = self.trade["trade_id"]
            if "closed:" + self.trade["trade_id"] in self.keys:
                from .monthly import report_row
                item["closed_notice"] = True
                item["accounting_fingerprint"] = digest(report_row(self.data, self.trade))
        if self.incident:
            item["incident"] = self.incident
            item["incident_signature"] = self.incident_signature
            item["resolved"] = resolved
        return item

    def pending_alarm(self, resolved):
        return self.incident and not resolved and any(row.get("incident") == self.incident and row.get("incident_signature") == self.incident_signature
                and not row.get("resolved") and row["status"] in ("pending", "sending", "exhausted") for row in self.data.get("notification_outbox", {}).values())

    def append_notice(self):
        resolved = bool(self.notice.get("resolved") or self.notice.get("source_notice", {}).get("code", "").endswith(("recovered", "resolved")))
        if self.pending_alarm(resolved):
            self.suppressed[self.identity] = (self.version, "same_pending_alarm")
            return
        if self.keys and all(key in self.used for key in self.keys):
            self.suppressed[self.identity] = (self.version, "same_execution_event")
            return
        if self.text is None:
            from .messages import telegram_view
            subset = {**self.data, "notifications": {self.identity: self.notice}}
            self.text = "\n".join(part for message in telegram_view(subset, {}, include_status=False)["messages"] for part in message["texts"])
        item = self.notice_item(resolved)
        # Sibling notices for the same receipt are included in one immutable delivery.
        sibling = next((row for row in self.messages if set(row["event_keys"]) == set(self.keys)), None)
        if sibling:
            sibling["versions"][self.identity] = self.version
        else:
            self.messages.append(item)
        self.used.update(self.keys)

    def select(self):
        for identity, notice in self.notifications.items():
            self.select_notice(identity, notice)
        return self.messages, self.suppressed, self.deferred


def select(data, *, covered, stamp=None):
    """Return messages and explicit suppression receipts, never fake Telegram ACKs."""
    return _NoticePolicy(data, covered, now_ms() if stamp is None else stamp).select()
