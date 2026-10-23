"""Important events from execution receipts; routine facts remain in the local activity log."""
from __future__ import annotations

from decimal import Decimal

from aqla_hyperliquid_mcp.models import digest, now_ms

from .messages import literal, number

TRANSIENT = {"reconciliation_unavailable", "recovery_timeout"}


def completed_waves(trade):
    """Only paired native terminal orders and real fills prove a completed wave."""
    observations = trade.get("order_observations", {})
    for entry in trade.get("entry_orders", []):
        if entry.get("kind") != "waverider" or entry.get("not_sent"):
            continue
        exit_order = next((row for row in trade.get("exit_orders", []) if row.get("kind") == "waverider"
                           and row.get("cycle") == entry.get("cycle") and not row.get("not_sent")), None)
        if not exit_order:
            continue
        pair = [observations.get(row["cloid"], {}) for row in (exit_order, entry)]
        if any(row.get("status") not in ("filled", "canceled") or row.get("oid") is None for row in pair):
            continue
        fills = [[fill for fill in trade.get("fill_events", {}).values() if fill["oid"] == row["oid"]] for row in pair]
        sold, bought = [sum((Decimal(fill["sz"]) for fill in group), Decimal(0)) for group in fills]
        if sold > 0 and bought >= Decimal(entry["quantity"]) and bought <= sold:
            yield "wave:" + trade["trade_id"] + ":" + str(entry["cycle"]), fills[1]


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


def select(data, *, covered, stamp=None):
    """Return messages and explicit suppression receipts, never fake Telegram ACKs."""
    stamp = now_ms() if stamp is None else stamp
    policy = data.get("notice_policy_state", {})
    announced = policy.get("announced_incidents", {})
    seen_fills = set(policy.get("seen_fills", []))
    used = {key for row in data.get("notification_outbox", {}).values() if row["status"] != "suppressed"
            for key in row.get("event_keys", [])}
    messages, suppressed, deferred = [], {}, {}
    notifications = data.get("notifications", {})
    for identity, notice in notifications.items():
        version = digest(notice)
        if (identity, version) in covered:
            continue
        trade = data.get("trades", {}).get(notice.get("trade_id"))
        severity, keys, text = notice.get("severity", "normal"), [], None
        incident, incident_signature = None, None
        observed = notice.get("observed_at", stamp)
        if identity.startswith("protection:") or severity in ("critical", "urgent", "error") and not ("recovery_reports" in notice or "reports" in notice):
            account = notice.get("account_key") or (trade or {}).get("plan", {}).get("account_key")
            incident = digest([account]) if account else None
            incident_signature = digest([identity, notice.get("error"), notice.get("code"), severity])
            if incident and announced.get(incident) and policy.get("incident_signatures", {}).get(incident) == incident_signature:
                suppressed[identity] = (version, "same_active_alarm")
                continue
            keys = ["alarm:" + digest([identity, incident_signature, notice.get("generation_id", observed)])]
        elif "recovery_reports" in notice or "reports" in notice:
            from .recovery_notices import safe_report
            reports = notice.get("recovery_reports", [fact for row in notice.get("reports", []) if (fact := safe_report(row))])
            incident = digest(sorted({row.get("account_key", row.get("exchange", "unknown")) for row in reports}))
            incident_signature = digest(reports)
            # An announced incident is cleared only by a positively reconciled resolution.
            if notice.get("resolved"):
                if not announced.get(incident):
                    suppressed[identity] = (version, "unannounced_recovery")
                    continue
                keys = ["recovery:" + incident + ":" + str(observed)]
            elif reports and all(row.get("code") in TRANSIENT for row in reports):
                newer_resolution = any(other.get("resolved") and other.get("observed_at", 0) >= observed
                    and {row.get("account_key", row.get("exchange")) for row in other.get("recovery_reports", [])}
                    >= {row.get("account_key", row.get("exchange")) for row in reports} for other in notifications.values())
                if newer_resolution:
                    suppressed[identity] = (version, "transient_resolved")
                    continue
                first = policy.get("incident_since", {}).get(incident, observed)
                if stamp - first < 120000:
                    deferred[incident] = first
                    continue
                if announced.get(incident):
                    suppressed[identity] = (version, "incident_already_announced")
                    continue
                keys = ["incident:" + incident + ":" + str(first)]
            else:
                if announced.get(incident) and policy.get("incident_signatures", {}).get(incident) == incident_signature:
                    suppressed[identity] = (version, "same_active_alarm")
                    continue
                keys = ["incident:" + incident + ":" + incident_signature + ":" + str(observed)]
                severity = "urgent"
        elif notice.get("source_notice"):
            code = notice["source_notice"].get("code", "")
            category = notice["source_notice"].get("category", "reader")
            incident = "source:" + category
            incident_signature = digest(notice["source_notice"])
            if code.endswith(("recovered", "resolved")):
                if not announced.get(incident):
                    suppressed[identity] = (version, "unannounced_source_recovery")
                    continue
                keys = ["source-recovery:" + identity]
            else:
                newer_resolution = any(other.get("source_notice", {}).get("category") == category
                    and other["source_notice"].get("code", "").endswith(("recovered", "resolved"))
                    and other.get("observed_at", 0) >= observed for other in notifications.values())
                if newer_resolution:
                    suppressed[identity] = (version, "transient_source_resolved")
                    continue
                if announced.get(incident) and policy.get("incident_signatures", {}).get(incident) == incident_signature:
                    suppressed[identity] = (version, "same_active_source_alarm")
                    continue
                if code in ("reader_unavailable", "reader_rate_limited", "sources_unavailable"):
                    first = policy.get("incident_since", {}).get(incident, observed)
                    if stamp - first < 120000:
                        deferred[incident] = first
                        continue
                else:
                    severity = "urgent"
                keys = ["source:" + identity]
        elif notice.get("monthly_report"):
            keys = ["month:" + digest(notice["monthly_report"])]
        elif trade:
            lifecycle = notice.get("lifecycle", {}).get("kind")
            current_fills = trade.get("fill_events", {})
            fresh = [(key, row) for key, row in current_fills.items() if trade["trade_id"] + ":" + key not in seen_fills]
            kind = None
            if lifecycle == "entry_fill":
                kind, keys = "entry", ["entry:" + trade["trade_id"]]
            elif lifecycle == "closed" or notice.get("change", {}).get("status") == "closed":
                kind, keys = "closed", ["closed:" + trade["trade_id"]]
            elif notice.get("change", {}).get("status") in ("unknown", "conflicted"):
                keys = ["trade-alarm:" + trade["trade_id"] + ":" + trade["status"]]
                severity = "urgent"
            else:
                exits = [(key, row) for key, row in fresh if row["kind"] not in ("entry", "waverider", "waverider_retirement")]
                if exits:
                    kind, keys = "exit", ["fill:" + trade["trade_id"] + ":" + key for key, _ in exits]
                    fresh = exits
                else:
                    wave = next(((key, fills) for key, fills in completed_waves(trade) if key not in used), None)
                    if wave:
                        kind, keys = "wave", [wave[0]]
                        fresh = [(digest(fill), fill) for fill in wave[1]]
            if kind:
                if stamp - observed < 15000:
                    continue
                text = compact_event(data, notice, trade, kind, [row for _, row in fresh])
            if not keys:
                suppressed[identity] = (version, "routine_trade_observation")
                continue
        elif notice.get("error") and not notice.get("event_id"):
            keys = ["system:" + digest(notice)]
        else:
            suppressed[identity] = (version, "dashboard_only")
            continue
        resolved = bool(notice.get("resolved") or notice.get("source_notice", {}).get("code", "").endswith(("recovered", "resolved")))
        if incident and not resolved and any(row.get("incident") == incident and row.get("incident_signature") == incident_signature
                and not row.get("resolved") and row["status"] in ("pending", "sending", "exhausted") for row in data.get("notification_outbox", {}).values()):
            suppressed[identity] = (version, "same_pending_alarm")
            continue
        if keys and all(key in used for key in keys):
            suppressed[identity] = (version, "same_execution_event")
            continue
        if text is None:
            from .messages import telegram_view
            subset = {**data, "notifications": {identity: notice}}
            text = "\n".join(part for message in telegram_view(subset, {}, include_status=False)["messages"] for part in message["texts"])
        item = {"versions": {identity: version}, "text": text, "severity": severity, "event_keys": keys}
        if trade:
            item["fill_keys"] = [trade["trade_id"] + ":" + key for key in trade.get("fill_events", {})]
            item["trade_id"] = trade["trade_id"]
            if "closed:" + trade["trade_id"] in keys:
                from .monthly import report_row
                item["closed_notice"] = True
                item["accounting_fingerprint"] = digest(report_row(data, trade))
        if incident:
            item["incident"] = incident
            item["incident_signature"] = incident_signature
            item["resolved"] = resolved
        # Sibling notices for the same receipt are included in one immutable delivery.
        sibling = next((row for row in messages if set(row["event_keys"]) == set(keys)), None)
        if sibling:
            sibling["versions"][identity] = version
        else:
            messages.append(item)
        used.update(keys)
    return messages, suppressed, deferred
