from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from aqla_agent.execution_outcome import (
    EntryOperations,
    diagnostic_view,
    event_outcomes,
    execution_counts,
    operation_diagnostic_view,
    retry_view,
    trade_outcome,
)
from aqla_agent.journal import Table
from aqla_agent.messages import telegram_view
from aqla_agent.native_setup import load_verifier
from aqla_agent.notifications import (
    NotificationAckResult,
    NotificationDetailsResult,
    NotificationReadResult,
    acknowledge_notifications,
    notification_page,
)
from aqla_agent.operator_management import ManagementRequest
from aqla_agent.pipelines import (
    AccountLimits,
    Pipeline,
    TradingProfile,
    Venue,
    route_control,
    sync_legacy_policy,
)
from aqla_agent.ranking import RankingPolicy, Tier, set_manual_rank, status_view
from aqla_agent.reporting import Reporting
from aqla_agent.router import Router, recovery_diagnostics
from aqla_agent.setup import SetupBinding, SetupGate
from aqla_agent.source_queue import is_source_row, queue_overview
from aqla_agent.tool_queries import compact_view, source_attribution
from aqla_agent.tool_queries import source_lookup as lookup_sources

from . import __version__
from .actions import NATIVE_MODELS, NativeRequest
from .api import Hyperliquid
from .auth import Authority
from .catalog import INFO_MODELS, SDK_MODELS, InfoRequest, SdkRequest, validate_info
from .coverage import audit
from .models import (
    Address,
    AqlaError,
    Interpretation,
    Policy,
    Settings,
    SourceClassification,
    SourceEvent,
    now_ms,
)
from .multisig import Signature
from .projections import ReaderOverview as ReaderOverview
from .projections import reader_overview as reader_overview
from .state import State
from .streams import SUBSCRIPTION_MODELS, Streams, Subscription
from .trades import OPEN


def create_server(settings: Settings, *, profile="agent", exchange=None, state=None):
    state = state or State(settings)
    exchange = exchange or Hyperliquid(settings, state)
    trading = Router(settings, state, exchange)
    reporting = Reporting(state, trading)
    streams = Streams(settings.base_url, timeout=settings.timeout_seconds)
    authority = Authority.from_file(settings.integration_key_file) if settings.integration_key_file else None
    trading.source.disposition_key = authority.key if authority else None
    from aqla_agent.delivery import NotificationOutbox
    from aqla_agent.source_processing import SourceProcessor
    processor = SourceProcessor(state, trading)
    from aqla_agent.express_worker import ExpressWorker
    express = ExpressWorker(processor, settings.express)
    outbox = NotificationOutbox(state, settings.telegram_owner_id)
    setup_gate = SetupGate(authority is not None and bool(settings.telegram_owner_id) and load_verifier(settings.setup_password_hash_file) is not None)

    def setup_context(claims):
        value = claims.get("setup", {}) if claims else {}
        try:
            binding = SetupBinding(**value["binding"])
            return value["identity"], binding
        except (KeyError, TypeError):
            return None, None

    def require_setup(claims):
        setup_gate.require(*setup_context(claims))

    def authorize(token, operator=False):
        if authority is None:
            raise AqlaError("Trusted Hermes integration key is not configured")
        claims = authority.verify(token, operator=operator)
        if processor.mode == "hybrid" and claims["kind"] == "source":
            raise AqlaError("Automatic sources are owned by the native Python processor")
        return claims

    async def maintenance():
        while True:
            started = asyncio.get_running_loop().time()
            try:
                account_passes = {}
                async with trading.lock:
                    reports = await trading.recover(diagnostics_sink=account_passes)
                from aqla_agent.recovery_notices import record_recovery_notices
                state.update_metadata(("notifications", "source_status"), lambda data, rows=reports, passes=account_passes:
                    record_recovery_notices(data, rows, passes))
            except Exception:
                # Native logging only; no second Telegram sender and no resubmission.
                import logging
                logging.getLogger(__name__).warning("Trade reconciliation unavailable; new entries remain gated")
            await asyncio.sleep(max(0.1, 10 - (asyncio.get_running_loop().time() - started)))

    @asynccontextmanager
    async def lifespan(_):
        # Serve MCP while the first maintenance pass reconciles; entry guards still require successful recovery.
        task = asyncio.create_task(maintenance(), name="aqla-protection")
        async def report_evidence():
            while True:
                try:
                    await reporting.tick()
                    from aqla_agent.monthly import scheduled_reports
                    reports = scheduled_reports(state.snapshot())
                    if reports:
                        def queue_monthly(data, reports=reports):
                            for report in reports:
                                month = report["month"]
                                if data["monthly_notified"].get(month):
                                    continue
                                data["notifications"].setdefault("monthly-local:" + month, {"monthly_report": report, "observed_at": now_ms()})
                                data["monthly_notified"][month] = {"queued_at": now_ms(), "fingerprint": report["fingerprint"]}
                        state.update_metadata(("notifications", "monthly_notified"), queue_monthly)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning("Monthly accounting evidence unavailable; completeness remains explicit")
                await asyncio.sleep(60)
        report_task = asyncio.create_task(report_evidence(), name="aqla-report-evidence")
        express_task = asyncio.create_task(express.run(), name="aqla-express")
        from aqla_agent.entry_watch import EntryWatch
        entry_task = asyncio.create_task(EntryWatch(trading).run(), name="aqla-entry-watch")
        try:
            yield None
        finally:
            task.cancel()
            report_task.cancel()
            express_task.cancel()
            entry_task.cancel()
            await asyncio.gather(task, report_task, express_task, entry_task, return_exceptions=True)
            await trading.close()
            await streams.close()
            state.close()

    server = MCPServer("aqla-hyperliquid", version=__version__, lifespan=lifespan,
                       instructions="Hermes interprets received sources. Python enforces user rules. Unknown exchange results are never automatically retried. Keep operation IDs stable.")
    server._aqla_setup_gate = setup_gate
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

    @server.tool(annotations=read)
    async def capabilities() -> dict:
        """Report actual endpoint coverage, configured account/network and signing scope."""
        return {"name": "aqla Agent", "version": __version__, "profile": profile, "network": settings.network,
                "account": exchange.account, "writes_configured": exchange.wallet is not None,
                "state": "transactional_sqlite_single_writer", "source_images": False,
                "automatic_source_products": ["perp"],
                "automatic_source_exchanges": trading.ready(), "pipeline_execution": "independent_targets",
                "info_types": sorted(INFO_MODELS), "sdk_exchange_operations": sorted(SDK_MODELS),
                "native_action_types": sorted(NATIVE_MODELS),
                "subscriptions": sorted(SUBSCRIPTION_MODELS),
                "coverage_complete": audit()["complete"], "pending_coverage": audit()["missing"], "coverage_scope": audit()["scope"],
                "exchange_atomicity_guaranteed": False, "live_exchange_verified": False}

    @server.tool(annotations=read)
    async def status(context_auth: str = "", view: Literal["full", "compact", "summary", "configuration"] = "full", offset: int = 0, limit: int = 20,
                     view_version: str | None = None) -> dict:
        """Read effective user rules, sources, pending work, owned trades and unresolved operations."""
        if view == "configuration":
            authorize(context_auth, operator=True)
            data = state.read("configuration_revision", "profiles", "pipelines", "ranking_policy", "ranking_revision",
                              "sources", "source_names", "source_names_revision", "source_ranks", "source_aliases", "trades", "paused",
                              "execution_accounts", "account_enabled", "account_limits", "venue_enabled", "venue_limits", "venue_accounts", "network")
            data["execution_accounts"] = trading.accounts_ready(data)
            data["source_ranks"] = status_view(data)
            data["trades"] = [row for row in data["trades"].values() if row["status"] in OPEN]
            from types import SimpleNamespace

            from aqla_agent.pipelines import Parameters
            from aqla_agent.waverider import live_authority
            permissions = {}
            for identity, profile_row in data["profiles"].items():
                if not profile_row.get("parameters"):
                    continue
                profile_parameters = Parameters.model_validate(profile_row["parameters"])
                permissions[identity] = {}
                for key, account in data["execution_accounts"].items():
                    engine = trading.engine_for(*key.split(":"))
                    permission = None
                    if engine and account.get("limits"):
                        plan = SimpleNamespace(policy=profile_parameters.policy(AccountLimits.model_validate(account["limits"])),
                            base_parameters=profile_row["parameters"], created_at=now_ms(), exchange="hyperliquid",
                            network=engine.exchange.settings.network, account=engine.exchange.account,
                            market=SimpleNamespace(quote_token="USDC"))
                        permission = live_authority(engine, plan)
                    permissions[identity][key] = permission
            data["waverider_permissions"] = permissions
            return data
        if view == "summary":
            snapshot = state.status_summary()
            from aqla_agent.waverider import recorder_health
            claims = authorize(context_auth, operator=True) if context_auth else None
            lease_id, binding = setup_context(claims)
            counts = snapshot["execution_counts"]
            return {"view": "summary", "paused": snapshot["paused"], "network": settings.network,
                    "setup": setup_gate.status(binding, lease_id, owner_id=settings.telegram_owner_id),
                    "source_queue": snapshot["source_queue"], "notification_pending": snapshot["notification_pending"],
                    "open_trade_count": counts["known_own_open"],
                    "open_trade_count_meaning": "confirmed_own_positions_from_retained_receipts",
                    "active_trade_count": snapshot["active_trade_count"],
                    "conflicted_trade_count": snapshot["conflicted_trade_count"],
                    "execution_counts": counts,
                    "execution_accounts": {key: {name: value for name, value in row.items() if name != "limits"}
                                           for key, row in trading.accounts_ready(snapshot).items()},
                    "account_recovery": recovery_diagnostics(snapshot),
                    "journal": {"enabled": True, "storage": "local SQLite"},
                    "configuration_revision": snapshot["configuration_revision"], "policy_revision": snapshot["policy_revision"],
                    "inventory_refreshed": False, "details": {"tool": "status", "view": "full"},
                    "source_processing": {**processor.overview(), "delivery": outbox.overview()},
                    "replay_recorder": {key: recorder_health(engine) for key, engine in trading.engines.items()},
                    "account_work": {key: {"waiting": sum(not future.done() for _, _, future, _ in engine.lock.waiters),
                        "wait_metrics": dict(engine.lock.metrics), "packet_timeout_seconds": getattr(engine, "recovery_packet_timeout", 30)}
                        for key, engine in trading.engines.items()}}
        snapshot = state.snapshot()
        queue = queue_overview(snapshot)
        ready = trading.ready()
        inventory = await trading.inventory_status()
        entry_operations = EntryOperations(snapshot["operations"])
        page = notification_page(snapshot, authority.key if authority else None, ready, require_key=False)
        claims = authorize(context_auth, operator=True) if context_auth else None
        lease_id, binding = setup_context(claims)
        result = {"network": settings.network, "account": exchange.account, "paused": snapshot["paused"], "setup": setup_gate.status(binding, lease_id, owner_id=settings.telegram_owner_id),
                "policy": snapshot["policy"], "policy_revision": snapshot["policy_revision"], "sources": snapshot["sources"],
                "source_names": snapshot.get("source_names", {}), "source_names_revision": snapshot.get("source_names_revision", 0),
                "pending": queue["ready"], "source_queue": queue, "source_processing": processor.overview(),
                "trades": [{**row, "execution_outcome": trade_outcome(row, entry_operations), "entry_diagnostic": diagnostic_view(row, entry_operations)} for row in snapshot["trades"].values() if row["status"] in OPEN],
                "execution_counts": execution_counts(snapshot),
                "unresolved_operations": [operation_diagnostic_view(trading.public_operation(row)) for row in snapshot["operations"].values() if row["phase"] in ("prepared", "sending", "unknown")],
                "notifications": page["notifications"], "notification_versions": page["notification_versions"],
                "notifications_page": {"pending": page["pending"], "next_cursor": page["next_cursor"], "pagination_available": authority is not None},
                "configuration_revision": snapshot["configuration_revision"], "profiles": snapshot["profiles"],
                "pipelines": snapshot["pipelines"], "exchanges": ready,
                "account_inventory": inventory,
                "account_recovery": recovery_diagnostics(snapshot),
                "execution_accounts": trading.accounts_ready(), "ranking_policy": snapshot["ranking_policy"],
                "ranking_revision": snapshot["ranking_revision"], "source_ranks": status_view(snapshot),
                "telegram_view": {**page["telegram_view"], "status": telegram_view({**snapshot, "notifications": {}}, trading.accounts_ready() if snapshot["ranking_policy"]["enabled"] else ready,
                                                                                 inventory=inventory)["status"]},
                "journal": {"enabled": True, "storage": "local SQLite"}}
        return compact_view(result, "status", {}, offset, limit, view_version) if view == "compact" else result

    @server.tool(annotations=read)
    async def journal_status() -> dict:
        """Local journal availability and columns; no external credentials or publication queue."""
        from aqla_agent.journal import HEADERS
        return {"enabled": True, "storage": "local SQLite", "tables": HEADERS, "manual_trade_fact_writes": False}

    @server.tool(annotations=read)
    async def journal_statistics(group_by: Literal["gesamt", "Quelle", "Symbol", "Richtung", "Börse", "Pipeline", "Profil", "Währung"] = "gesamt",
                                 account_key: str | None = None) -> dict:
        """Evidence-qualified closed-trade results, separated by currency/network and including actual funding."""
        from decimal import Decimal

        from aqla_agent.monthly import report_row
        if account_key is not None and account_key not in ("hyperliquid:mainnet", "hyperliquid:testnet"):
            raise AqlaError("Select a configured execution account")
        data, groups = state.snapshot(), {}
        labels = {"Quelle": "chat_id", "Symbol": "symbol", "Richtung": "side", "Börse": "exchange", "Pipeline": "pipeline_id", "Profil": "profile_id", "Währung": "currency"}
        for trade in data["trades"].values():
            if trade["status"] != "closed":
                continue
            plan = trade["plan"]
            context = plan.get("exchange", "hyperliquid") + ":" + plan["network"]
            if account_key and context != account_key:
                continue
            row = report_row(data, trade)
            fields = {**plan, **plan["market"], **row, "chat_id": trade["chat_id"]}
            group = "gesamt" if group_by == "gesamt" else str(fields.get(labels[group_by], "—"))
            identity = context + ":" + row["currency"] + ":" + group
            selected = groups.setdefault(identity, {"network": plan["network"], "currency": row["currency"], "group": group,
                "trades": 0, "complete": 0, "excluded": 0, "net_pnl": Decimal(0), "fees": Decimal(0), "funding": Decimal(0), "wins": 0})
            selected["trades"] += 1
            if not row["complete"]:
                selected["excluded"] += 1
                continue
            selected["complete"] += 1
            selected["wins"] += Decimal(row["net_pnl"]) > 0
            for key in ("net_pnl", "fees", "funding"):
                selected[key] += Decimal(row[key])
        return {"groups": {identity: {key: (str(value) if row["complete"] else None) if isinstance(value, Decimal) else value for key, value in row.items()}
                           for identity, row in groups.items()}, "funding_included": True, "storage": "local SQLite"}

    @server.tool(annotations=read)
    async def journal_read(table: Table, offset: Annotated[int, Field(ge=0)] = 0,
                           limit: Annotated[int, Field(ge=1, le=100)] = 20,
                           filters: dict[str, str] | None = None, account_key: str | None = None,
                           columns: list[str] | None = None, trade_id: str | None = None) -> dict:
        """Read local journal evidence, including historical accounts. Never writes trade facts."""
        from aqla_agent.journal import read_page
        return await asyncio.to_thread(read_page, state.snapshot(), table, offset=offset, limit=limit, filters=filters,
                                       account_key=account_key, columns=columns, trade_id=trade_id)

    @server.tool(annotations=read)
    async def dashboard(network: Literal["mainnet", "testnet"] = "mainnet", period: str = "current",
                        currency: str = "USDC", offset: Annotated[int, Field(ge=0)] = 0,
                        limit: Annotated[int, Field(ge=1, le=20)] = 8) -> dict:
        """Local dashboard with evidence-qualified PnL, fees and funding, separated by network and currency."""
        from aqla_agent.presentation import build_presentation
        data = state.snapshot()
        snapshot = await asyncio.to_thread(build_presentation, data, {"network": network, "period": period, "currency": currency}, transport_budget=False)
        value = snapshot.model_dump(mode="json")
        rows = value.pop("journal")
        for key in ("channels", "parameters", "months", "daily_pnl", "monthly_pnl", "cumulative_realized_net"):
            value.pop(key)
        rows.sort(key=lambda row: (row.get("closed_at") or row.get("entry_at") or 0, row["trade_id"]), reverse=True)
        return {**value, "journal": rows[offset:offset + limit], "total": len(rows),
                "next_offset": offset + limit if offset + limit < len(rows) else None,
                "paused": data["paused"], "account_recovery": recovery_diagnostics(data),
                "source_queue": queue_overview(data), "storage": "local SQLite",
                "observations": "Last reconciled execution evidence; no new exchange orders or live price request"}

    @server.tool(annotations=read)
    async def source_lookup(start: str, end: str, source: str | None = None, symbol: str | None = None,
                            status: str | None = None, account: str | None = None, network: Literal["mainnet", "testnet"] | None = None,
                            offset: int = 0, limit: int = 20, cursor: str | None = None,
                            time_field: Literal["sent_at", "received_at"] = "sent_at") -> dict:
        """LOCAL retained-source search over an aware interval [start,end), stable pages and explicit Berlin offsets. Status matches event status OR a bound trade's status. Undated saved bindings have unknown interval membership. Never reads an exchange or changes state; absence never proves never received."""
        return await asyncio.to_thread(lambda: lookup_sources(state.snapshot(), start, end, source=source, symbol=symbol, status=status,
                              account=account, network=network, offset=offset, limit=limit, cursor=cursor, time_field=time_field))

    @server.tool(annotations=read)
    async def setup_status(context_auth: str = "") -> dict:
        """Read Setup availability and remaining fixed lease time; no private credential data."""
        claims = authorize(context_auth, operator=True) if context_auth else None
        lease_id, binding = setup_context(claims)
        return setup_gate.status(binding, lease_id, owner_id=settings.telegram_owner_id)

    @server.tool(annotations=read)
    async def ranking_status(source: str | None = None, history_limit: int = 30) -> dict:
        """Read source tiers, net-R performance, proof/exclusions, policy and rank history. Never consumes new proof or changes ranks."""
        from aqla_agent.ranking import source_key
        from aqla_agent.sources import resolve_source
        data = state.read("ranking_policy", "ranking_revision", "source_ranks", "ranking_history", "source_names",
                          "sources", "source_aliases", "network", "venue_accounts", "venue_enabled", "account_enabled",
                          "venue_limits", "account_limits")
        if not 1 <= history_limit <= 100:
            raise AqlaError("Ranking history limit must be 1–100")
        selected = source_key(data, resolve_source(data, source)) if source else None
        return {"policy": data["ranking_policy"], "revision": data["ranking_revision"],
                "sources": {identity: {**row, "source_id": identity, "name": data.get("source_names", {}).get(identity, identity)}
                            for identity, row in status_view(data).items() if selected is None or identity == selected},
                "history": sorted([row for row in data["ranking_history"].values() if selected is None or row["source_id"] == selected], key=lambda row: row["time"], reverse=True)[:history_limit],
                "execution_accounts": trading.accounts_ready(data), "basis": "net_R_per_original_signal_setup"}

    @server.tool(annotations=read)
    async def monthly_report(month: str, offset: int = 0, limit: int = 100, diagnostics_only: bool = False) -> dict:
        """Read Aqla Mainnet trades closed in YYYY-MM, fees/funding, initial-margin ROE and completeness. No exchange writes."""
        from aqla_agent.monthly import month_bounds
        from aqla_agent.monthly import monthly_report as calculate
        if isinstance(offset, bool) or isinstance(limit, bool) or offset < 0 or not 1 <= limit <= 500:
            raise AqlaError("Monthly page requires offset >= 0 and limit between 1 and 500")
        month_bounds(month)
        snapshot = state.snapshot()
        diagnostic = None
        if diagnostics_only:
            return {"month": month, "publication_job": diagnostic}
        report = calculate(snapshot, month)
        totals = {key: len(report[key]) for key in ("rows", "open_trades", "undated_closed_trades")}
        return {**report, **{key: report[key][offset:offset + limit] for key in totals},
                "page": {"offset": offset, "limit": limit, "totals": totals}, "publication_job": diagnostic}

    @server.tool(annotations=read)
    async def trade_diagnostics(trade_id: str | None = None, symbol: str | None = None, offset: int = 0, limit: int = 20,
                                view: Literal["full", "compact"] = "full", view_version: str | None = None,
                                sections: list[Literal["plan", "sources", "orders", "fills", "accounting", "operations", "lifecycle", "prices"]] | None = None) -> dict:
        """Read source, exact sent order type, own fills, price checks and proved closure reason, including closed trades."""
        from aqla_agent.lifecycle import close_reason
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise AqlaError("Diagnostic page requires offset >= 0 and limit 1–100")
        snapshot = state.snapshot()
        entry_operations = EntryOperations(snapshot["operations"])
        if trade_id is None:
            if not symbol:
                raise AqlaError("Supply an Aqla trade ID or a market symbol")
            candidates = [row for row in snapshot["trades"].values() if row["plan"]["market"]["symbol"].casefold() == symbol.casefold()]
            candidates.sort(key=lambda row: (row.get("closed_at", row["plan"]["created_at"]), row["trade_id"]), reverse=True)
            if len(candidates) != 1:
                return {"candidates": [{"trade_id": row["trade_id"], "market": row["plan"]["market"]["symbol"], "status": row["status"], "execution_outcome": trade_outcome(row, entry_operations), "created_at": row["plan"]["created_at"], "closed_at": row.get("closed_at")} for row in candidates[offset:offset + limit]],
                        "selection_required": True, "total": len(candidates), "offset": offset,
                        "next_offset": offset + limit if offset + limit < len(candidates) else None}
            trade_id = candidates[0]["trade_id"]
        trade = snapshot["trades"].get(trade_id)
        if trade is None:
            raise AqlaError("Unknown Aqla trade ID")
        from aqla_agent.journal import trade_operation_ids
        operation_ids = trade_operation_ids(snapshot, trade_id)
        operations = [operation_diagnostic_view(trading.public_operation(row)) for key, row in snapshot["operations"].items()
                      if key in operation_ids]
        result = {"trade_id": trade_id, "status": trade["status"], "plan": trade["plan"],
                "execution_outcome": trade_outcome(trade, entry_operations), "observation_retry": retry_view(trade),
                "entry_diagnostic": diagnostic_view(trade, entry_operations),
                "sources": [row["source"] for row in snapshot["events"].values() if is_source_row(row) and row["source_id"] in trade["plan"]["source_refs"]],
                "source_names": snapshot.get("source_names", {}), "orders": trade["orders"], "exit_orders": trade.get("exit_orders", []),
                "order_observations": trade.get("order_observations", {}), "fill_events": trade.get("fill_events", {}),
                "entry_vwap": trade.get("entry_vwap"), "entry_accounting": trade.get("entry_accounting"),
                "initial_sl_risk": trade.get("initial_sl_risk"), "source_tp_finalization": trade.get("percent_finalization"),
                "source_tp_updates": trade.get("percent_updates", {}), "execution_incident": trade.get("execution_incident"),
                "price_checks": trade.get("price_checks", []), "waiting_snapshot": trade.get("waiting_snapshot"),
                "waiting_error": trade.get("waiting_error"), "unsent_reason": trade.get("unsent_reason"),
                "close_reason": trade.get("close_reason", close_reason(trade)), "lifecycle_events": trade.get("lifecycle_events", {}),
                "operations": operations, "historical_missing_quotes_are_not_inferred": True,
                **source_attribution(snapshot, trade["plan"])}
        if sections is not None:
            from aqla_agent.efficiency import diagnostic_sections
            result = diagnostic_sections(result, snapshot, sections)
        return compact_view(result, "trade_diagnostics", {"trade_id": trade_id, "sections": sections}, offset, limit, view_version) if view == "compact" else result

    @server.tool(annotations=read)
    async def source_context(event_id: str, view: Literal["full", "compact", "decision"] = "full", offset: int = 0, limit: int = 20,
                             view_version: str | None = None) -> dict:
        """Read source message, bounded same-channel/reply context and open trade bindings."""
        result = trading.context(event_id, decision=view == "decision")
        snapshot = state.snapshot()
        entry_operations = EntryOperations(snapshot["operations"])
        result = {**result, "execution_outcome": event_outcomes(snapshot, result["event"], entry_operations),
                  "trades": [{**row, "execution_outcome": trade_outcome(row, entry_operations), "entry_diagnostic": diagnostic_view(row, entry_operations)} for row in result["trades"]]}
        if view == "decision":
            from aqla_agent.efficiency import decision_context
            return decision_context(result)
        receipt = result.get("source_read_receipt")
        result = {"source_read_receipt": receipt, "percentage_rule": "source-roe-v1: new TP percentages use roe_percent on own initial margin before fees/funding, effective margin leverage and final own entry VWAP", **result}
        return compact_view(result, "source_context", {"event_id": event_id}, offset, limit, view_version) if view == "compact" else result

    @server.tool(annotations=read)
    async def source_check_interpretation(event_id: str, interpretation: Interpretation) -> dict:
        """Check a source draft against current originals without saving, planning or trading. Returns precise errors and safely shortened original numeric quotes. Submit the returned interpretation to existing tools only after valid=true; their authorization and validation still apply."""
        from aqla_agent.source_assistance import check_interpretation
        return check_interpretation(trading.context(event_id, decision=True), interpretation)

    @server.tool(annotations=write)
    async def interpret_source(event_id: str, interpretation: Interpretation, context_auth: str = "", source_read_receipt: str = "",
                               result_view: Literal["full", "ack"] = "full") -> dict:
        """Save contextual classification/evidence for an actual received source; does not trade."""
        authorize(context_auth)
        if interpretation.kind not in ("new_signal", "trade_update"):
            result = trading.source.record_interpretation(event_id, interpretation, read_receipt=source_read_receipt, result_view=result_view)
        else:
            result = trading.source.record_interpretation(event_id, interpretation, result_view=result_view)
        snapshot = state.snapshot()
        return {**result, "execution_outcome": event_outcomes(snapshot, snapshot["events"][event_id])}

    @server.tool(annotations=read)
    async def source_context_batch(event_ids: Annotated[list[str], Field(min_length=1, max_length=8)]) -> dict:
        """Read up to eight distinct source anchors losslessly with shared context and a separate receipt per item."""
        from aqla_agent.efficiency import decision_batch
        if len(set(event_ids)) != len(event_ids):
            raise AqlaError("Source context batch requires distinct event IDs")
        snapshot = state.snapshot()
        index = trading.source.context_index(snapshot)
        contexts = [trading.context(identity, decision=True, snapshot=snapshot, index=index) for identity in event_ids]
        entry_operations = EntryOperations(snapshot["operations"])
        return decision_batch([{**context, "execution_outcome": event_outcomes(snapshot, context["event"], entry_operations),
                                "trades": [{**row, "execution_outcome": trade_outcome(row, entry_operations), "entry_diagnostic": diagnostic_view(row, entry_operations)} for row in context["trades"]]}
                               for context in contexts])

    @server.tool(annotations=write)
    async def interpret_source_batch(items: Annotated[list[SourceClassification], Field(min_length=1, max_length=8)],
                                     context_auth: str = "") -> dict:
        """Independently classify non-actionable sources with original quotes and own current receipts. Never creates plans/orders. Partial success is explicit."""
        authorize(context_auth)
        if len({item.event_id for item in items}) != len(items):
            raise AqlaError("Classification batch requires distinct event IDs")
        results = []
        for item in items:
            authorize(context_auth)
            try:
                interpretation = Interpretation.model_validate(item.model_dump(exclude={"event_id", "source_read_receipt"}))
                row = trading.source.record_interpretation(item.event_id, interpretation,
                      read_receipt=item.source_read_receipt, result_view="ack")
                snapshot = state.snapshot()
                results.append({"ok": True, **row, "execution_outcome": event_outcomes(snapshot, snapshot["events"][item.event_id])})
            except AqlaError as error:
                results.append({"ok": False, "event_id": item.event_id, "error": str(error), "reread_required": True})
        return {"items": results, "succeeded": sum(row["ok"] for row in results), "partial_success": any(not row["ok"] for row in results)}

    @server.tool(annotations=write)
    async def trade_preview(event_id: str, interpretation: Interpretation, context_auth: str = "", source_read_receipt: str = "") -> dict:
        """Calculate and durably save an immutable source-bound plan under current user rules."""
        authorize(context_auth)
        if interpretation.kind not in ("new_signal", "trade_update"):
            return trading.source.record_interpretation(event_id, interpretation, read_receipt=source_read_receipt)
        try:
            result = await trading.preview(event_id, interpretation)
            if state.snapshot()["events"][event_id].get("last_error"):
                state.update(lambda data: data["events"][event_id].pop("last_error", None))
            return result
        except AqlaError as error:
            if event_id in state.snapshot()["events"]:
                state.update(lambda data, message=str(error): data["events"][event_id].update(last_error=message))
            raise

    @server.tool(annotations=write)
    async def trade_execute(plan_id: str | None = None, context_auth: str = "", plan_ids: list[str] | None = None) -> dict:
        """Execute exactly the saved plan once; repeated IDs never dispatch a second entry."""
        authorize(context_auth)
        if plan_ids is not None:
            if plan_id is not None:
                raise AqlaError("Supply either plan_id or plan_ids")
            return await trading.execute_many(plan_ids)
        if plan_id is None:
            raise AqlaError("Supply a saved plan ID")
        return await trading.execute(plan_id)

    @server.tool(annotations=write)
    async def trade_update(event_id: str, interpretation: Interpretation, context_auth: str = "") -> dict:
        """Apply one uniquely bound source update under the original trade's policy."""
        authorize(context_auth)
        return await trading.update_trade(event_id, interpretation)

    @server.tool(annotations=read)
    async def operation_status(operation_id: str) -> dict:
        """Read one durable operation without sending or altering exchange orders."""
        row = state.snapshot()["operations"].get(operation_id)
        if row is None:
            raise AqlaError("Unknown operation")
        return operation_diagnostic_view(trading.public_operation(row))

    @server.tool(annotations=write)
    async def operation_reconcile(operation_id: str, context_auth: str = "") -> dict:
        """Read exact order identities and update evidence; never resubmit an exchange action."""
        authorize(context_auth)
        return await trading.reconcile(operation_id)

    @server.tool(annotations=read)
    async def notifications_read(cursor: str | None = None, limit: int = 100,
                                 view: Literal["full", "facts", "rendered"] = "full") -> NotificationReadResult:
        """Read a bounded versioned page; after ACK/stale begin again with no cursor."""
        # Notification messages use saved account/plan facts; no inventory/status
        # overview is rendered here, so another complete readiness snapshot is unnecessary.
        return state.notification_page(authority.key if authority else None, cursor=cursor, limit=limit, view=view)

    @server.tool(annotations=read)
    async def notifications_details(notification_id: str, version: str, cursor: str | None = None, limit: int = 16384) -> NotificationDetailsResult:
        """Read lossless canonical JSON fragments of the exact saved notification version."""
        return state.notification_details(authority.key if authority else None, notification_id, version, cursor=cursor, limit=limit)

    @server.tool(annotations=write)
    async def notifications_ack(notification_ids: list[str], context_auth: str = "", versions: dict[str, str] | None = None) -> NotificationAckResult:
        """Standalone clients only before native outbox binding. In every bound gateway mode Python owns delivery and exact-version ACK; source agents must not acknowledge these notices."""
        authorize(context_auth)
        if outbox.generation is not None:
            raise AqlaError("Notification acknowledgements are owned by the native outbox")
        def acknowledge(data):
            return acknowledge_notifications(data, notification_ids, versions)
        return state.update_metadata(("notifications",), acknowledge)

    @server.tool(annotations=write)
    async def operator_manage(request: ManagementRequest, context_auth: str = "") -> dict:
        """Explicit owner management of one existing trade; never opens a new setup."""
        from aqla_agent.operator_management import submit
        claims = authorize(context_auth, operator=True)
        def commit_check():
            authorize(context_auth, operator=True)
            if request.action not in ("close", "reduce", "cancel_entry", "pause_waverider"):
                require_setup(claims)
        commit_check()
        return await submit(trading, request, claims["request_id"], commit_check)

    @server.tool(annotations=write)
    async def control(action: Literal["set_policy", "pause", "resume", "set_sources", "add_sources", "remove_sources", "set_source_name", "set_source_rank", "close_trade", "resolve_conflict", "clear_notification", "set_profile", "set_pipeline", "delete_profile", "delete_pipeline", "set_exchange_limits", "set_exchange_enabled", "set_ranking_policy", "set_account_limits", "set_account_enabled"],
                      expected_revision: Annotated[int | None, Field(strict=True, ge=0)] = None, policy: Policy | None = None, sources: list[str] | None = None,
                      trade_id: str | None = None, notification_id: str | None = None, context_auth: str = "",
                      profile: TradingProfile | None = None, pipeline: Pipeline | None = None, exchange_id: Venue | None = None,
                      limits: AccountLimits | None = None, enabled: bool | None = None, identity: str | None = None,
                      source_id: str | None = None, source_name: str | None = None,
                      ranking_policy: RankingPolicy | None = None, account_key: str | None = None, tier: Tier | None = None,
                      review_fingerprint: str | None = None) -> dict:
        """Authenticated operator controls. add_sources/remove_sources preserve other sources; set_sources replaces the COMPLETE list. Source selection requires current configuration_revision."""
        claims = authorize(context_auth, operator=True)
        protected = action not in ("pause", "set_source_name", "close_trade", "clear_notification") and not (
            action in ("set_account_enabled", "set_exchange_enabled") and enabled is False)
        def commit_check():
            authorize(context_auth, operator=True)
            if protected:
                require_setup(claims)
        commit_check()
        if action == "set_source_rank":
            def set_rank(data):
                commit_check()
                return set_manual_rank(data, source_id, tier, expected_revision, setup_context(claims)[1].owner_id)
            result = state.update_if_changed(set_rank)
            if result["changed"]:
                # Existing cancellation/reconciliation handles old unfilled entries;
                # owned partial fills retain their protection and are never replayed.
                async with trading.lock:
                    await trading.recover()
            return result
        if action == "set_ranking_policy":
            if ranking_policy is None or expected_revision != state.snapshot()["ranking_revision"]:
                raise AqlaError("Supply the complete ranking policy and its current revision")
            def set_ranking(data):
                commit_check()
                if expected_revision != data["ranking_revision"]:
                    raise AqlaError("Read the current ranking policy revision")
                data["ranking_policy"] = ranking_policy.model_dump(mode="json")
                data["ranking_revision"] += 1
                for row in data["source_ranks"].values():
                    row.update(up=None, down=0)
                return {"policy": data["ranking_policy"], "revision": data["ranking_revision"]}
            return state.update(set_ranking)
        if action in ("set_account_limits", "set_account_enabled"):
            data = state.snapshot()
            if account_key not in data["execution_accounts"] or expected_revision != data["configuration_revision"]:
                raise AqlaError("Select a configured execution account and current configuration revision")
            if action == "set_account_enabled" and enabled:
                engine = trading.engine_for(*account_key.split(":"))
                if engine is None or not data["account_limits"].get(account_key):
                    raise AqlaError("Account credentials and explicit limits must be ready before activation")
                await engine.exchange.verify_signer()
                engine.state.assert_binding(engine.exchange.settings)
            def set_account(values):
                commit_check()
                if expected_revision != values["configuration_revision"]:
                    raise AqlaError("Read the current configuration revision")
                if action == "set_account_limits":
                    if limits is None:
                        raise AqlaError("Complete account limits are required")
                    values["account_limits"][account_key] = limits.model_dump(mode="json")
                else:
                    if enabled is None:
                        raise AqlaError("Specify the account activation value")
                    values["account_enabled"][account_key] = enabled
                engine = trading.engine_for(*account_key.split(":"))
                if engine and engine.state.legacy_context(values):
                    venue = account_key.split(":")[0]
                    values["venue_limits"][venue] = values["account_limits"][account_key]
                    values["venue_enabled"][venue] = values["account_enabled"][account_key]
                    if values.get("policy") is None and values["profiles"]["standard"].get("parameters") and values["account_limits"].get(account_key):
                        from aqla_agent.pipelines import Parameters
                        values["policy"] = Parameters.model_validate(values["profiles"]["standard"]["parameters"]).policy(
                            AccountLimits.model_validate(values["account_limits"][account_key])).model_dump(mode="json")
                values["configuration_revision"] += 1
                return {"configuration_revision": values["configuration_revision"], "account": account_key,
                        "limits": values["account_limits"][account_key], "enabled": values["account_enabled"][account_key]}
            return state.update(set_account)
        if action == "set_source_name":
            from aqla_agent.sources import set_source_name
            def rename(data):
                commit_check()
                return set_source_name(data, source_id, source_name, expected_revision)
            return state.update(rename)
        if action == "close_trade":
            if trade_id is None:
                raise AqlaError("trade_id is required")
            return await trading.close_trade(trade_id, claims["request_id"])
        if action == "resolve_conflict":
            return await trading.resolve_conflict(trade_id, review_fingerprint, commit_check)
        if action == "set_exchange_enabled" and enabled is True:
            if expected_revision != state.snapshot()["configuration_revision"]:
                raise AqlaError("Read the current configuration revision before activation")
            await trading.activate(exchange_id)
        if action in ("set_profile", "set_pipeline", "delete_profile", "delete_pipeline", "set_exchange_limits", "set_exchange_enabled"):
            def configure(data):
                commit_check()
                return route_control(data, action, expected_revision=expected_revision, profile=profile, pipeline=pipeline,
                                     exchange=exchange_id, limits=limits, enabled=enabled, identity=identity)
            return state.update(configure)

        def apply(data):
            commit_check()
            previous_paused = data["paused"]
            if action == "set_policy":
                if policy is None or expected_revision != data["policy_revision"]:
                    raise AqlaError("Read current rule revision and supply a complete validated policy")
                data["policy"] = policy.model_dump(mode="json")
                data["policy_revision"] += 1
                sync_legacy_policy(data)
            elif action in ("pause", "resume"):
                data["paused"] = action == "pause"
            elif action in ("set_sources", "add_sources", "remove_sources"):
                from aqla_agent.sources import change_sources
                return change_sources(data, action, sources, expected_revision)
            elif action == "clear_notification":
                data["notifications"].pop(notification_id, None)
            return {"policy": data["policy"], "policy_revision": data["policy_revision"], "paused": data["paused"], "sources": data["sources"],
                    **({"previous_paused": previous_paused} if action in ("pause", "resume") else {})}

        return state.update(apply)

    @server.tool(annotations=write)
    async def conflict_review(trade_id: str, context_auth: str = "") -> dict:
        """Authenticated ownership review with fresh original-account evidence; records a short-lived review fingerprint. Never creates/cancels an order. Unproved/mixed exposure remains reserved."""
        authorize(context_auth, operator=True)
        return await trading.conflict_review(trade_id)

    @server.tool(annotations=write)
    async def integration(action: Literal["ingest", "native_forward", "snapshot", "resolve_sources", "seed_context", "setup_bind", "setup_grant", "setup_failed", "setup_revoke", "setup_status", "presentation_snapshot", "source_configure", "source_prepare", "source_packet", "source_record", "source_apply", "source_fail", "source_overview", "source_notice", "delivery_next", "delivery_complete", "delivery_retry", "runtime_reload", "activity"], token: str, source: SourceEvent | None = None,
                          aliases: dict[str, str] | None = None, cutoffs: dict[str, int] | None = None,
                          context_ids: dict[str, list[int]] | None = None,
                          setup_binding: dict | None = None, lease_id: str | None = None,
                          generation: str | None = None, selection: dict | None = None,
                          technical: dict | None = None,
                          processing_mode: Literal["legacy", "shadow", "hybrid"] | None = None,
                          processing_owner: str | None = None, processing_version: str | None = None,
                          job_id: str | None = None, decision_result: dict | None = None,
                          failure_code: str | None = None, delivery_id: str | None = None,
                          failure_detail: dict | None = None,
                          decision_status: dict | None = None,
                          notice_sources: Annotated[list[str] | None, Field(max_length=100)] = None,
                          retry_max_attempts: Annotated[int, Field(strict=True, ge=1, le=5)] = 3,
                          retry_backoff_seconds: Annotated[int, Field(strict=True, ge=1, le=300)] = 15,
                          retry_max_age_seconds: Annotated[int, Field(strict=True, ge=30, le=3600)] = 900,
                          delivery_part: int | None = None, delivery_success: bool | None = None,
                          delivery_message_id: Annotated[str | None, Field(max_length=64)] = None,
                          offset: Annotated[int, Field(strict=True, ge=0)] = 0,
                          limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 100) -> dict:
        """Internal native-plugin access, filtered out of agent tools; token never enters model context."""
        if authority is None:
            raise AqlaError("Integration key is not configured")
        authority.internal(token)
        if action == "runtime_reload":
            from .models import ExpressSettings, ReplaySettings
            if not technical or set(technical) != {"express", "waverider_replay"}:
                raise AqlaError("Only explicit technical controls may be reloaded")
            selected = ExpressSettings.model_validate(technical["express"])
            if settings._configuration_file is not None:
                fresh = Settings.load(settings._configuration_file)
                if fresh.express.model_dump(mode="json") != selected.model_dump(mode="json") or fresh.waverider_replay.model_dump(mode="json") != technical["waverider_replay"]:
                    raise AqlaError("Technical request was superseded by the current saved configuration")
            if selected.master_path != settings.express.master_path:
                raise AqlaError("Technical reload cannot change the approved master path")
            if selected.mode == "live" and not express.qualified():
                raise AqlaError("Express master qualification is missing")
            settings.express.mode = selected.mode
            settings.waverider_replay = ReplaySettings.model_validate(technical["waverider_replay"])
            for engine in trading.engines.values():
                engine.exchange.settings.waverider_replay = settings.waverider_replay
            return {"reloaded": True}
        if action == "activity":
            data = state.read("notice_history", "notifications")
            rows = list(data.get("notice_history", {}).values())
            rows += [{"notice": row, "disposition": "pending", "recorded_at": row.get("observed_at", 0)} for row in data["notifications"].values()]
            rows.sort(key=lambda row: row.get("recorded_at", 0), reverse=True)
            return {"rows": rows[offset:offset + limit], "total": len(rows), "next_offset": offset + limit if offset + limit < len(rows) else None}
        if action == "source_configure":
            configured = processor.configure(processing_mode, processing_owner, retry_max_attempts=retry_max_attempts,
                retry_backoff_seconds=retry_backoff_seconds, retry_max_age_seconds=retry_max_age_seconds, decision_status=decision_status)
            outbox.bind(processing_owner)
            return configured
        if action == "source_prepare":
            return processor.prepare(processing_owner, processing_version)
        if action == "source_packet":
            return processor.packet_page(processing_owner, job_id, offset)
        if action == "source_record":
            from aqla_agent.decisions import DecisionResult
            if decision_result is None:
                raise AqlaError("A typed native decision is required")
            return processor.record(processing_owner, job_id, DecisionResult.model_validate(decision_result))
        if action == "source_apply":
            return await processor.apply(processing_owner, job_id)
        if action == "source_fail":
            return processor.fail(processing_owner, job_id, failure_code, detail=failure_detail)
        if action == "source_overview":
            return {**processor.overview(), "delivery": outbox.overview()}
        if action == "source_notice":
            if processor.mode != "legacy":
                processor.require_owner(processing_owner)
            from aqla_agent.recovery_notices import record_source_notice
            def notice(data):
                record_source_notice(data, failure_code, processing_version, notice_sources or ())
            state.update_metadata(("notifications", "source_status"), notice)
            return {"recorded": True}
        if action in ("delivery_next", "delivery_complete", "delivery_retry"):
            if action == "delivery_next":
                return outbox.next_part(processing_owner)
            if action == "delivery_retry":
                return outbox.retry(processing_owner)
            if type(delivery_part) is not int or type(delivery_success) is not bool:
                raise AqlaError("A confirmed native send result is required")
            return outbox.complete(processing_owner, delivery_id, delivery_part, delivery_success, delivery_message_id)
        if action == "presentation_snapshot":
            return state.presentation_snapshot(selection)
        if action == "setup_revoke":
            owner_id = (setup_binding or {}).get("owner_id")
            if not isinstance(owner_id, str) or not owner_id:
                raise AqlaError("Private owner binding is required")
            setup_gate.revoke(owner_id)
            return {"revoked": True}
        if action.startswith("setup_"):
            try:
                binding = SetupBinding(**(setup_binding or {}))
            except TypeError:
                raise AqlaError("Private native Setup binding is required") from None
            if binding.owner_id != settings.telegram_owner_id:
                raise AqlaError("Setup binding must identify the configured Telegram owner")
            if action == "setup_bind":
                return {"generation": setup_gate.bind(binding), "locked_seconds": setup_gate.status(binding)["locked_seconds"]}
            if action == "setup_failed":
                return setup_gate.failed(binding)
            if action == "setup_grant":
                setup_gate.register(binding, lease_id, None, generation)
                return {"registered": True, "remaining_seconds": setup_gate.status(binding, lease_id)["remaining_seconds"]}
            return setup_gate.status(binding, lease_id)
        if action == "seed_context":
            snapshot = state.read("sources", "source_aliases")
            allowed = {*snapshot["sources"], *snapshot.get("source_aliases", {}).values()}
            if cutoffs is None or any(chat not in allowed or cutoff < 0 for chat, cutoff in cutoffs.items()):
                raise AqlaError("Context cutoffs must identify subscribed source messages")
            if context_ids is None or set(context_ids) != set(cutoffs) or any(
                len(ids) > settings.max_context_messages or ids != sorted(set(ids)) or any(identity < 1 or identity > cutoffs[chat] for identity in ids)
                for chat, ids in context_ids.items()
            ):
                raise AqlaError("Supply the bounded sorted context IDs for every seeded source")
            def seed(data):
                if any(chat in data.get("context_cutoffs", {}) and data["context_cutoffs"][chat] != cutoff for chat, cutoff in cutoffs.items()):
                    raise AqlaError("An existing context cutoff cannot be replaced")
                for chat, cutoff in cutoffs.items():
                    data.setdefault("context_cutoffs", {}).setdefault(chat, cutoff)
                    data.setdefault("context_ids", {}).setdefault(chat, context_ids[chat])
                    data["cursors"].setdefault(chat, cutoff)
                return data["context_cutoffs"]
            return {"context_cutoffs": state.update(seed)}
        if action == "resolve_sources":
            selected = state.read("sources")["sources"]
            if aliases is None or any(key not in selected or not value.lstrip("-").isdigit() for key, value in aliases.items()):
                raise AqlaError("Reader source resolution must bind only configured sources to actual Telegram IDs")
            def resolve(data):
                data["source_aliases"] = aliases
                from aqla_agent.ranking import source_rank
                for source in aliases.values():
                    source_rank(data, source)
            state.update(resolve)
            return {"source_aliases": aliases}
        if action in ("ingest", "native_forward"):
            if source is None:
                raise AqlaError("Missing source event")
            return trading.ingest(source, native_forward=action == "native_forward")
        return state.reader_snapshot(settings.max_context_messages)

    if profile == "full":
        def public_guard(context_auth, request):
            from aqla_agent.setup_actions import public_action_requires_setup
            def check(data):
                claims = authorize(context_auth, operator=True)
                if public_action_requires_setup(request, data, account_key="hyperliquid:" + settings.network, account=exchange.account, network=settings.network):
                    require_setup(claims)
            check(state.read("operations", "trades"))
            return check

        @server.tool(annotations=read)
        async def hl_info(request: InfoRequest) -> dict:
            """Typed public /info requests, including native perps, spot, HIP-3 and outcome data."""
            payload = validate_info(request)
            kind = payload.pop("type")
            result = await exchange.info(kind, **payload)
            return {"network": settings.network, "observed_at": now_ms(), "data": result,
                    "history_may_be_truncated": kind in ("userFills", "userFillsByTime", "historicalOrders", "userTwapSliceFills", "userFunding", "userNonFundingLedgerUpdates")}

        @server.tool(annotations=write)
        async def hl_exchange(operation_id: str, request: SdkRequest, context_auth: str = "") -> dict:
            """Typed official SDK operations with durable identities and correct account/signing scope."""
            return await exchange.sdk_action(operation_id, request, prepare_guard=public_guard(context_auth, request))

        @server.tool(annotations=write)
        async def hl_action(operation_id: str, request: NativeRequest, context_auth: str = "") -> dict:
            """Strict native action contracts including non-SDK TWAP, trailing, staking and deployer variants."""
            return await exchange.native_action(operation_id, request, prepare_guard=public_guard(context_auth, request))

        @server.tool(annotations=write)
        async def hl_multisig(action: Literal["prepare", "submit"], operation_id: str, multi_sig_user: Address,
                              request: SdkRequest | NativeRequest, nonce: int | None = None,
                              signatures: list[Signature] | None = None, expires_after: int | None = None, context_auth: str = "") -> dict:
            """Prepare typed multisig signing data or verify collected signatures and submit exactly once."""
            from .multisig import prepare, submit
            guard = public_guard(context_auth, request)
            if action == "prepare":
                result = await prepare(exchange, operation_id, multi_sig_user, request, nonce, expires_after)
                guard(state.read("operations", "trades"))
                return result
            return await submit(exchange, operation_id, multi_sig_user, request, nonce, signatures, expires_after, prepare_guard=guard)

        @server.tool(annotations=write)
        async def hl_stream(action: Literal["subscribe", "unsubscribe", "read", "post_info", "post_exchange", "post_action"],
                            subscription: Subscription | None = None, subscription_id: str | None = None,
                            after: int = 0, limit: int = 100, info_request: InfoRequest | None = None,
                            exchange_request: SdkRequest | None = None, operation_id: str | None = None,
                            native_request: NativeRequest | None = None, context_auth: str = "") -> dict:
            """Typed native feeds, bounded event reads and correlated posts; reconnect never resends actions."""
            if action == "subscribe":
                if subscription is None:
                    raise AqlaError("A typed subscription is required")
                return await streams.subscribe(subscription)
            if action == "unsubscribe":
                if subscription_id is None:
                    raise AqlaError("subscription_id is required")
                return await streams.unsubscribe(subscription_id)
            if action == "read":
                return streams.read(after, limit)
            if action == "post_info":
                if info_request is None:
                    raise AqlaError("A typed info request is required")
                return {"data": await streams.post("info", validate_info(info_request))}
            if action == "post_action":
                if native_request is None or operation_id is None:
                    raise AqlaError("Typed native request and stable operation_id are required")
                return await exchange.native_action(operation_id, native_request, sender=lambda payload: streams.post("action", payload), prepare_guard=public_guard(context_auth, native_request))
            if exchange_request is None or operation_id is None:
                raise AqlaError("A typed exchange request and stable operation_id are required")
            return await exchange.sdk_action(operation_id, exchange_request, sender=lambda payload: streams.post("action", payload), prepare_guard=public_guard(context_auth, exchange_request))

    # Test/diagnostic handles, not MCP-exposed methods.
    server.aqla_state, server.aqla_exchange, server.aqla_trading = state, exchange, trading
    server.aqla_authority = authority
    server.aqla_streams = streams
    server.aqla_express = express
    return server


async def close_server(server):
    with contextlib.suppress(Exception):
        await server.aqla_trading.close()
        await server.aqla_streams.close()
    server.aqla_state.close()
