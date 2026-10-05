"""Deterministic public journal rows and a durable, digest-bound export queue."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from aqla_hyperliquid_mcp.models import AqlaError, Model, digest, now_ms

from .parameters import parameter_values

HEADERS = {
    "Trades": ["ID", "Quelle", "Symbol", "Richtung", "Status", "Erstellt (UTC)", "Entry geplant", "Entry realisiert", "Menge geplant", "Gefüllt", "Offen", "SL", "TPs", "Risiko geplant USD", "Risiko reserviert USD", "PnL realisiert vor Gebühren/Funding", "Gebühren USD", "Netto vor Funding USD", "Quellen-IDs"],
    "Signale": ["ID", "Quelle", "Nachrichten-ID", "Gesendet (UTC)", "Editiert (UTC)", "Reply-ID", "Kontext", "Status", "Klassifikation", "Symbol", "Richtung", "Update", "Grund", "Text", "Quellen-ID"],
    "Orders": ["ID", "Trade-ID", "Art", "TP-Index", "Menge", "Preis/Ziel", "Ersetzt", "Status Börse", "Order-ID", "Status geändert (UTC)"],
    "Fills": ["ID", "Trade-ID", "Zeit (UTC)", "Symbol", "Art", "TP-Index", "Order-ID", "Menge", "Preis", "Gebühr", "Gebühren-Token"],
    "Vorgänge": ["ID", "Erstellt (UTC)", "Art", "Phase", "Ergebnis", "Nonce", "Ablauf (UTC)", "Request", "Response"],
    "Regeln": ["ID", "Netzwerk", "Konto", "Revision", "Pause", "Quellen", "Regeln"],
    "Handelsparameter": ["ID", "Bereich", "Parameter", "Aktueller Wert", "Einheit", "Zulässige Werte", "Standard bei Regelerstellung", "Bedeutung"],
    "Ereignisse": ["ID", "Zeit (UTC)", "Sequenz", "Tabelle", "Datensatz-ID", "Vorher", "Nachher"],
}
LEGACY_HEADERS = {table: list(headers) for table, headers in HEADERS.items()}
TARGET_COLUMNS = ["Börse", "Netzwerk", "Konto", "Pipeline", "Profil", "Profilrevision", "Währung"]
for _table in ("Trades", "Orders", "Fills", "Vorgänge"):
    HEADERS[_table] += TARGET_COLUMNS
HEADERS["Handelsparameter"] += ["Profil", "Börse", "Währung", "Revision"]
HEADERS["Pipelines"] = ["ID", "Name", "Quellen", "Börse", "Profil", "Aktiv", "Börse aktiv", "Revision"]
TABLES = tuple(HEADERS)
Table = Literal["Trades", "Signale", "Orders", "Fills", "Vorgänge", "Regeln", "Handelsparameter", "Ereignisse", "Pipelines"]


class JournalRecord(Model):
    table: Table
    record_id: str
    values: list[str | int | float | bool]
    fingerprint: str
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_row(self):
        if len(self.values) != len(HEADERS[self.table]) or self.values[0] != self.record_id or fingerprint(self.values) != self.fingerprint:
            raise ValueError("Journal row identity, schema or digest is invalid")
        return self


def timestamp(value):
    return datetime.fromtimestamp(int(value) / 1000, UTC).isoformat() if value else ""


def text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def number(value):
    return float(Decimal(str(value))) if value is not None else ""


def fit_cell(value):
    if not isinstance(value, str):
        return value
    encoded = value.encode("utf-16-le")
    return encoded[:88000].decode("utf-16-le", errors="ignore") + " … [gekürzt]" if len(encoded) > 88000 else value


def fingerprint(values):
    canonical = [str(Decimal(str(value)).normalize()) if isinstance(value, (int, float)) and not isinstance(value, bool) else value for value in values]
    return digest(canonical)


def record(table, identity, *values):
    cells = [fit_cell(value if value is not None else "") for value in [identity, *values]]
    if table in LEGACY_HEADERS and len(cells) == len(LEGACY_HEADERS[table]):
        cells += [""] * (len(HEADERS[table]) - len(cells))
        if table in ("Trades", "Orders", "Fills", "Vorgänge"):
            cells[-7:] = ["hyperliquid", "", "", "legacy", "standard", "", "USDC"]
    if table not in HEADERS or len(cells) != len(HEADERS[table]):
        raise AqlaError("Journal row does not match its fixed table schema")
    return {"table": table, "record_id": identity, "values": cells, "fingerprint": fingerprint(cells)}


def record_key(row):
    return row["table"] + ":" + row["record_id"]


def project(data):
    rows = {}
    def add(row):
        rows[record_key(row)] = row
    aliases = {value: name for name, value in data.get("source_aliases", {}).items()}
    actions = {order.get("c"): order for operation in data["operations"].values() for order in operation["wire_action"].get("orders", []) if order.get("c")}
    for identity, event in data["events"].items():
        source, interpretation = event["source"], event.get("interpretation") or {}
        add(record("Signale", identity, aliases.get(source["chat_id"], source["chat_id"]), source["message_id"], timestamp(source["sent_at"]), timestamp(source.get("edited_at")), source.get("reply_to_id"), source.get("historical", False), event["status"], interpretation.get("kind", ""), interpretation.get("symbol", ""), interpretation.get("side", ""), interpretation.get("update_action", ""), event.get("last_error") or interpretation.get("reason", ""), source["text"], event["source_id"]))
    for identity, trade in data["trades"].items():
        plan = trade["plan"]
        fills = trade.get("fill_events", {})
        entry = Decimal(trade["entry_vwap"]) if trade.get("entry_vwap") else None
        exits = [fill for fill in fills.values() if fill["kind"] != "entry"]
        gross = sum((Decimal(fill["sz"]) * (Decimal(fill["px"]) - entry) * (1 if plan["side"] == "long" else -1) for fill in exits), Decimal(0)) if entry is not None else None
        quote = plan["market"].get("quote_token", "USDC")
        fees_known = all(fill.get("fee") is not None and fill.get("feeToken") in (None, quote) for fill in fills.values())
        fees = sum((Decimal(fill["fee"]) for fill in fills.values()), Decimal(0)) if fees_known else None
        net = gross - fees if gross is not None and fees is not None else None
        add(record("Trades", identity, aliases.get(trade["chat_id"], trade["chat_id"]), plan["market"]["symbol"], plan["side"], trade["status"], timestamp(plan["created_at"]), number(plan["entry_price"]), number(entry), number(plan["quantity"]), number(trade["filled_quantity"]), number(trade["remaining_quantity"]), number(trade["stop_price"]), text(trade.get("targets", plan["take_profits"])), number(plan["risk_usdc"]), number(trade["reserved_risk"]), number(gross), number(fees), number(net), text(plan["source_refs"])))
        entry_order = {"kind": "entry", "cloid": trade["entry_cloid"], "quantity": plan["quantity"], "price": plan["entry_limit"]}
        for order in [entry_order, *trade["orders"], *trade.get("exit_orders", [])]:
            action = actions.get(order["cloid"], {})
            observed = trade.get("order_observations", {}).get(order["cloid"], {})
            add(record("Orders", order["cloid"], identity, order["kind"], order.get("index"), number(order.get("quantity", action.get("s"))), number(order.get("price", action.get("p"))), order.get("retired", False), observed.get("status", ""), str(observed["oid"]) if observed.get("oid") is not None else "", timestamp(observed.get("changed_at"))))
        for fill_id, fill in fills.items():
            add(record("Fills", fill_id, identity, timestamp(fill.get("time")), fill.get("coin", plan["market"]["symbol"]), fill["kind"], fill.get("index"), str(fill["oid"]), number(fill["sz"]), number(fill["px"]), number(fill.get("fee")), fill.get("feeToken") or quote))
    for identity, operation in data["operations"].items():
        add(record("Vorgänge", identity, timestamp(operation["created_at"]), operation["wire_action"].get("type", ""), operation["phase"], operation.get("outcome", ""), str(operation["nonce"]), timestamp(operation.get("expires_after")), text(operation["request"]), text(operation.get("result"))))
    add(record("Regeln", "aktuell", data["network"], data["account"], data["policy_revision"], data["paused"], text(data["sources"]), text(data["policy"])))
    for values in parameter_values(data):
        add(record("Handelsparameter", *values))
    for identity, pipeline in data.get("pipelines", {}).items():
        for target in pipeline["targets"]:
            add(record("Pipelines", identity + ":" + target["exchange"], pipeline["name"], text(pipeline["sources"]), target["exchange"],
                       target["profile_id"], pipeline["enabled"], data["venue_enabled"].get(target["exchange"], False), pipeline["revision"]))
    for row in rows.values():
        table, identity = row["table"], row["record_id"]
        if table not in ("Trades", "Orders", "Fills", "Vorgänge"):
            continue
        trade = data["trades"].get(identity if table == "Trades" else row["values"][1])
        plan = trade["plan"] if trade else {}
        operation = data["operations"].get(identity, {}) if table == "Vorgänge" else {}
        venue = plan.get("exchange", operation.get("exchange", "hyperliquid"))
        binding = data.get("venue_accounts", {}).get(venue, {})
        row["values"][-7:] = [venue, plan.get("network", binding.get("network", data["network"])), plan.get("account", binding.get("account", data["account"])),
                              plan.get("pipeline_id") or operation.get("pipeline_id") or "legacy", plan.get("profile_id") or operation.get("profile_id") or "standard",
                              plan.get("profile_revision") or operation.get("profile_revision") or "", plan.get("market", {}).get("quote_token", "USDT" if venue == "aster" else "USDC")]
        row["fingerprint"] = fingerprint(row["values"])
    for row in data.get("journal_history", {}).values():
        add(row)
    return rows


def enqueue_changes(previous, proposed):
    before, after = project(previous), project(proposed)
    pending = proposed.setdefault("journal_pending", {})
    history = proposed.setdefault("journal_history", {})
    for key, row in after.items():
        old = before.get(key)
        if old and old["fingerprint"] == row["fingerprint"]:
            continue
        pending[key] = row["fingerprint"]
        if row["table"] != "Ereignisse":
            identity = digest([proposed["sequence"], key, row["fingerprint"]])[:32]
            # Short transition facts; large original texts are already in Signale.
            def summary(item):
                if not item:
                    return ""
                return text({name: value for name, value in zip(HEADERS[item["table"]], item["values"], strict=True) if name not in ("Text", "Request", "Response")})
            audit = record("Ereignisse", identity, timestamp(now_ms()), proposed["sequence"], row["table"], row["record_id"], summary(old), summary(row))
            history[identity] = audit
            pending[record_key(audit)] = audit["fingerprint"]


def seed(data):
    pending = data.setdefault("journal_pending", {})
    for key, row in project(data).items():
        pending[key] = row["fingerprint"]


def batch(data, limit=100):
    records = project(data)
    pending = data.get("journal_pending", {})
    selected = []
    size = 0
    for key in pending:
        if key not in records:
            continue
        row = {**records[key], "revision": data["sequence"]}
        extra = len(text(row).encode("utf-8"))
        if len(selected) >= limit or size + extra > 600_000:
            break
        selected.append(row)
        size += extra
    return {"records": selected, "pending": len(pending)}


def acknowledge(data, accepted):
    records = project(data)
    pending = data.get("journal_pending", {})
    count = 0
    for key, version in accepted.items():
        current = records.get(key)
        if current and current["fingerprint"] == version and pending.get(key) == version:
            pending.pop(key)
            if current["table"] == "Ereignisse":
                data["journal_history"].pop(current["record_id"], None)
            count += 1
    if count:
        data["journal_synced_at"] = now_ms()
    return {"acknowledged": count, "pending": len(pending)}
