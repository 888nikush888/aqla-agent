"""Deterministic public journal rows and a durable, digest-bound export queue."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from aqla_agent.monthly import accounting_facts, accounting_issues, fill_identity
from aqla_agent.source_queue import is_source_row
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
HEADERS["Signale"] += ["Quellenname"]
TRADE_DETAILS = ["Quellenname", "Originalsymbol", "Signalhebel original", "Signalhebel abgerundet", "Hebel wirksam", "Backup-Felder", "Backup-Referenzentry", "Backup-SL (%)", "Backup-TPs (%)"]
HEADERS["Trades"] += TRADE_DETAILS
HEADERS["Handelsparameter"] += ["Profil", "Börse", "Währung", "Revision"]
HEADERS["Pipelines"] = ["ID", "Name", "Quellen", "Börse", "Profil", "Aktiv", "Börse aktiv", "Revision"]
PRE_RANK_HEADERS = {table: list(headers) for table, headers in HEADERS.items()}
RANK_DETAILS = ["Konto-Kontext", "Setup-ID", "Angeforderte Börse", "Routing-Ziel", "Ersatzgrund", "Rang beim Entry", "Score beim Entry", "Risikofaktor",
                "Profil-RPR (%)", "RPR-Budget (%)", "Anfängliches SL-Risiko", "TP-Quelle", "TP-Prozentbelege"]
HEADERS["Trades"] += RANK_DETAILS
HEADERS["Pipelines"] += ["Ersatzbörsen", "Netzwerkregel"]
HEADERS["Ränge"] = ["ID", "Quellenname", "Rang", "Startfreigabe", "Score", "Nachweise gesamt", "Setups im Fenster", "Durchschnitt Netto-R", "Profit-Faktor", "Drawdown R", "Gewinnquote (%)", "Risikofaktor", "Netzwerk", "Rangrevision", "Regelrevision", "Ausgeschlossene Setups"]
HEADERS["Ranghistorie"] = ["ID", "Quelle", "Quellenname", "Zeit (UTC)", "Von", "Nach", "Score", "Drawdown R", "Netzwerk", "Revision", "Grund", "Nachweise", "Risikofaktor"]
PRE_PRESENTATION_HEADERS = {table: list(headers) for table, headers in HEADERS.items()}
HEADERS["Trades"] += ["Entry-Ausführung", "TP-Ausführung"]
HEADERS["Fills"] += ["Ausführungsrolle"]
TABLES = tuple(HEADERS)
Table = Literal["Trades", "Signale", "Orders", "Fills", "Vorgänge", "Regeln", "Handelsparameter", "Ereignisse", "Pipelines", "Ränge", "Ranghistorie"]


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
    if table in PRE_PRESENTATION_HEADERS and len(cells) == len(PRE_PRESENTATION_HEADERS[table]):
        cells += [""] * (len(HEADERS[table]) - len(cells))
    if table in PRE_RANK_HEADERS and len(cells) == len(PRE_RANK_HEADERS[table]):
        cells += [""] * (len(HEADERS[table]) - len(cells))
    if table in LEGACY_HEADERS and len(cells) == len(LEGACY_HEADERS[table]):
        cells += [""] * (len(HEADERS[table]) - len(cells))
        if table in ("Trades", "Orders", "Fills", "Vorgänge"):
            start = HEADERS[table].index("Börse")
            cells[start:start + 7] = ["hyperliquid", "", "", "legacy", "standard", "", "USDC"]
    if table not in HEADERS or len(cells) != len(HEADERS[table]):
        raise AqlaError("Journal row does not match its fixed table schema")
    return {"table": table, "record_id": identity, "values": cells, "fingerprint": fingerprint(cells)}


def record_key(row):
    return row["table"] + ":" + row["record_id"]


def _signal_record(data, identity, event, aliases):
    source, interpretation = event["source"], event.get("interpretation") or {}
    return record("Signale", identity, aliases.get(source["chat_id"], source["chat_id"]), source["message_id"], timestamp(source["sent_at"]), timestamp(source.get("edited_at")), source.get("reply_to_id"), source.get("historical", False), event["status"], interpretation.get("kind", ""), interpretation.get("symbol", ""), interpretation.get("side", ""), interpretation.get("update_action", ""), event.get("last_error") or interpretation.get("reason", ""), source["text"], event["source_id"], data.get("source_names", {}).get(source["chat_id"], ""))


def _selected_record(data, key, fallback, history):
    table, separator, identity = key.partition(":")
    if separator and table in ("Signale", "Ereignisse"):
        row = history.get(key)
        if row is not None:
            return row
        if table == "Signale" and identity in data["events"] and is_source_row(data["events"][identity]):
            aliases = {value: name for name, value in data.get("source_aliases", {}).items()}
            return _signal_record(data, identity, data["events"][identity], aliases)
        return None
    if not fallback:
        fallback.append(project(data))
    return fallback[0].get(key)


def project(data):
    rows = {}
    def add(row):
        rows[record_key(row)] = row
    aliases = {value: name for name, value in data.get("source_aliases", {}).items()}
    actions = {order.get("c"): order for operation in data["operations"].values() for order in operation["wire_action"].get("orders", []) if order.get("c")}
    for identity, event in data["events"].items():
        if is_source_row(event):
            add(_signal_record(data, identity, event, aliases))
    for identity, trade in data["trades"].items():
        plan = trade["plan"]
        fills = trade.get("fill_events", {})
        entry = Decimal(trade["entry_vwap"]) if trade.get("entry_vwap") else None
        exits = [fill for fill in fills.values() if fill["kind"] != "entry"]
        gross = sum((Decimal(fill["sz"]) * (Decimal(fill["px"]) - entry) * (1 if plan["side"] == "long" else -1) for fill in exits), Decimal(0)) if entry is not None else None
        quote = plan["market"].get("quote_token", "USDC")
        fee_facts = {identity: trade.get("report_fee_evidence", {}).get(fill_identity(fill), fill) for identity, fill in fills.items()}
        fees_known = bool(fills) and all(fact.get("fee") is not None and fact.get("feeToken") == quote for fact in fee_facts.values())
        fees = sum((Decimal(fact["fee"]) for fact in fee_facts.values()), Decimal(0)) if fees_known else None
        net = gross - fees if gross is not None and fees is not None else None
        if accounting_issues(trade, {**trade, **accounting_facts(trade)}):
            net = None
        add(record("Trades", identity, aliases.get(trade["chat_id"], trade["chat_id"]), plan["market"]["symbol"], plan["side"], trade["status"], timestamp(plan["created_at"]), number(plan["entry_price"]), number(entry), number(plan["quantity"]), number(trade["filled_quantity"]), number(trade["remaining_quantity"]), number(trade["stop_price"]), text(trade.get("targets", plan["take_profits"])), number(plan["risk_usdc"]), number(trade["reserved_risk"]), number(gross), number(fees), number(net), text(plan["source_refs"])))
        entry_order = {"kind": "entry", "cloid": trade["entry_cloid"], "quantity": plan["quantity"], "price": plan["entry_limit"]}
        for order in [entry_order, *trade["orders"], *trade.get("exit_orders", [])]:
            action = actions.get(order["cloid"], {})
            observed = trade.get("order_observations", {}).get(order["cloid"], {})
            add(record("Orders", order["cloid"], identity, order["kind"], order.get("index"), number(order.get("quantity", action.get("s"))), number(order.get("price", action.get("p"))), order.get("retired", False), observed.get("status", ""), str(observed["oid"]) if observed.get("oid") is not None else "", timestamp(observed.get("changed_at"))))
        for fill_id, fill in fills.items():
            fee = fee_facts[fill_id]
            add(record("Fills", fill_id, identity, timestamp(fill.get("time")), fill.get("coin", plan["market"]["symbol"]), fill["kind"], fill.get("index"), str(fill["oid"]), number(fill["sz"]), number(fill["px"]), number(fee.get("fee")), fee.get("feeToken")))
    for identity, operation in data["operations"].items():
        add(record("Vorgänge", identity, timestamp(operation["created_at"]), operation["wire_action"].get("type", ""), operation["phase"], operation.get("outcome", ""), str(operation["nonce"]), timestamp(operation.get("expires_after")), text(operation["request"]), text(operation.get("result"))))
    add(record("Regeln", "aktuell", data["network"], data["account"], data["policy_revision"], data["paused"], text(data["sources"]), text(data["policy"])))
    for values in parameter_values(data):
        add(record("Handelsparameter", *values))
    for identity, pipeline in data.get("pipelines", {}).items():
        for target in pipeline["targets"]:
            add(record("Pipelines", identity + ":" + target["exchange"], pipeline["name"], text(pipeline["sources"]), target["exchange"],
                       target["profile_id"], pipeline["enabled"], data["venue_enabled"].get(target["exchange"], False), pipeline["revision"],
                       text(target.get("fallback_exchanges") if target.get("fallback_exchanges") is not None else ["aster", "lighter"] if target["exchange"] == "hyperliquid" else []), "Rang: C+ Mainnet, D/E/F Testnet"))
    if data.get("ranking_policy"):
        from aqla_agent.ranking import FACTORS, source_key
        for source, rank in data.get("source_ranks", {}).items():
            metrics = rank.get("metrics", {})
            resolved = source_key(data, source)
            tier = rank["tier"]
            add(record("Ränge", source, data.get("source_names", {}).get(resolved, resolved), tier, rank.get("bootstrap", False),
                       number(metrics.get("score")), len(rank.get("evidence", [])), metrics.get("count", 0), number(metrics.get("mean_r")),
                       metrics.get("profit_factor", ""), number(metrics.get("drawdown_r")), number(metrics.get("win_rate")),
                       data["ranking_policy"]["factors"].get(tier, FACTORS[tier]), "mainnet" if tier in ("C", "B", "A", "S") else "testnet",
                       rank["revision"], data["ranking_revision"], len(rank.get("exclusions", []))))
        for identity, event in data.get("ranking_history", {}).items():
            add(record("Ranghistorie", identity, event["source_id"], data.get("source_names", {}).get(event["source_id"], event["source_id"]), timestamp(event["time"]),
                       event["old"], event["new"], number(event["metrics"].get("score")), number(event["metrics"].get("drawdown_r")), event["network"], event["revision"],
                       event.get("reason"), event.get("proof_total"), event.get("factor")))
    for row in rows.values():
        table, identity = row["table"], row["record_id"]
        if table not in ("Trades", "Orders", "Fills", "Vorgänge"):
            continue
        trade = data["trades"].get(identity if table == "Trades" else row["values"][1])
        plan = trade["plan"] if trade else {}
        operation = data["operations"].get(identity, {}) if table == "Vorgänge" else {}
        venue = plan.get("exchange", operation.get("exchange", "hyperliquid"))
        binding = data.get("venue_accounts", {}).get(venue, {})
        start = HEADERS[table].index("Börse")
        row["values"][start:start + 7] = [venue, plan.get("network", binding.get("network", data["network"])), plan.get("account", binding.get("account", data["account"])),
                              plan.get("pipeline_id") or operation.get("pipeline_id") or "legacy", plan.get("profile_id") or operation.get("profile_id") or "standard",
                              plan.get("profile_revision") or operation.get("profile_revision") or "", plan.get("market", {}).get("quote_token", "USDT" if venue == "aster" else "USDC")]
        if table == "Trades":
            backup = plan.get("backup_values") or {}
            final = trade.get("backup_finalization") or {}
            details_start = HEADERS[table].index("Quellenname")
            row["values"][details_start:details_start + len(TRADE_DETAILS)] = [data.get("source_names", {}).get(trade["chat_id"], ""), plan.get("source_symbol") or plan["market"]["symbol"],
                number(plan.get("signal_leverage_original", plan.get("signal_leverage"))), number(plan.get("signal_leverage")), number(plan.get("effective_leverage")),
                text(plan.get("backup_fields", [])), number(final.get("reference_entry")), number(backup.get("fallback_stop_loss_percent")), text(backup.get("fallback_take_profit_percentages", []))]
            rank = plan.get("rank_snapshot") or {}
            base = plan.get("base_parameters") or {}
            rank_start = HEADERS[table].index(RANK_DETAILS[0])
            row["values"][rank_start:rank_start + len(RANK_DETAILS)] = [plan.get("account_key", venue + ":" + plan["network"]), plan.get("setup_id", ""), plan.get("requested_exchange", venue), plan.get("routing_leg_id", ""),
                       plan.get("fallback_reason", ""), rank.get("tier", ""), number(rank.get("score")), rank.get("factor", ""),
                       number(base.get("risk_percent")), number(plan["policy"]["risk_percent"]), number(trade.get("initial_sl_risk", {}).get("amount")),
                       trade.get("source_tp_override", plan.get("source_tp", {})).get("unit", "price"),
                       text({"original": plan.get("source_tp"), "final": trade.get("percent_finalization"), "updates": trade.get("percent_updates", {}), "current": trade.get("source_tp_override")} if plan.get("source_tp") or trade.get("percent_updates") else {})]
            row["values"][-2:] = [plan.get("entry_execution", "standard"), plan.get("take_profit_execution", "standard")]
        elif table == "Fills":
            role = trade.get("fill_execution_roles", {}).get(identity, "unknown")
            row["values"][-1] = role if role in ("maker", "taker", "unknown") else "unknown"
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
    pending = data.get("journal_pending", {})
    history = {record_key(row): row for row in data.get("journal_history", {}).values() if record_key(row) in pending}
    selected, fallback = [], []
    size = 0
    for key in pending:
        current = _selected_record(data, key, fallback, history)
        if current is None:
            continue
        row = {**current, "revision": data["sequence"]}
        extra = len(text(row).encode("utf-8"))
        if len(selected) >= limit or size + extra > 600_000:
            break
        selected.append(row)
        size += extra
    return {"records": selected, "pending": len(pending)}


def acknowledge(data, accepted):
    fallback = []
    history = {record_key(row): row for row in data.get("journal_history", {}).values() if record_key(row) in accepted} if accepted else {}
    pending = data.get("journal_pending", {})
    count = 0
    for key, version in accepted.items():
        current = _selected_record(data, key, fallback, history)
        if current and current["fingerprint"] == version and pending.get(key) == version:
            pending.pop(key)
            if current["table"] == "Ereignisse":
                data["journal_history"].pop(current["record_id"], None)
            count += 1
    if count:
        data["journal_synced_at"] = now_ms()
    return {"acknowledged": count, "pending": len(pending)}
