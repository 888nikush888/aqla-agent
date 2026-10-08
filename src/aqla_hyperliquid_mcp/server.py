from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from aqla_agent.messages import telegram_view
from aqla_agent.monthly import MonthlyFailure, MonthlyJobBinding, MonthlyPublication, MonthlySection
from aqla_agent.notifications import (
    NotificationAckResult,
    NotificationDetailsResult,
    NotificationReadResult,
    acknowledge_notifications,
    notification_page,
)
from aqla_agent.pipelines import (
    AccountLimits,
    Pipeline,
    TradingProfile,
    Venue,
    route_control,
    sync_legacy_policy,
)
from aqla_agent.ranking import RankingPolicy, status_view
from aqla_agent.reporting import Reporting
from aqla_agent.router import Router, recovery_diagnostics

from . import __version__
from .actions import NATIVE_MODELS, NativeRequest
from .api import Hyperliquid
from .auth import Authority
from .catalog import INFO_MODELS, SDK_MODELS, InfoRequest, SdkRequest, validate_info
from .coverage import audit
from .models import Address, AqlaError, Interpretation, Policy, Settings, SourceEvent, now_ms
from .multisig import Signature
from .projections import ReaderOverview as ReaderOverview
from .projections import reader_overview as reader_overview
from .state import State
from .streams import SUBSCRIPTION_MODELS, Streams, Subscription
from .trades import OPEN, UNFINISHED


def create_server(settings: Settings, *, profile="agent", exchange=None, state=None):
    state = state or State(settings)
    exchange = exchange or Hyperliquid(settings, state)
    trading = Router(settings, state, exchange)
    reporting = Reporting(state, trading)
    streams = Streams(settings.base_url, timeout=settings.timeout_seconds)
    authority = Authority.from_file(settings.integration_key_file) if settings.integration_key_file else None

    def authorize(token, operator=False):
        if authority is None:
            raise AqlaError("Trusted Hermes integration key is not configured")
        return authority.verify(token, operator=operator)

    async def maintenance():
        while True:
            started = asyncio.get_running_loop().time()
            try:
                async with trading.lock:
                    reports = await trading.recover()
                if reports:
                    meaningful = [row for row in reports if row.get("error") or row.get("replacement_unconfirmed")]
                    if meaningful:
                        state.update(lambda data, reports=meaningful: data["notifications"].__setitem__(str(now_ms()), {"reports": reports}))
            except Exception:
                # Native logging only; no second Telegram sender and no resubmission.
                import logging
                logging.getLogger(__name__).warning("Trade reconciliation unavailable; new entries remain gated")
            await asyncio.sleep(max(0.1, 10 - (asyncio.get_running_loop().time() - started)))

    @asynccontextmanager
    async def lifespan(_):
        await trading.recover()
        task = asyncio.create_task(maintenance(), name="aqla-protection")
        async def report_evidence():
            while True:
                try:
                    await reporting.tick()
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning("Monthly accounting evidence unavailable; completeness remains explicit")
                await asyncio.sleep(60)
        report_task = asyncio.create_task(report_evidence(), name="aqla-report-evidence")
        try:
            yield None
        finally:
            task.cancel()
            report_task.cancel()
            await asyncio.gather(task, report_task, return_exceptions=True)
            await trading.close()
            await streams.close()
            state.close()

    server = MCPServer("aqla-hyperliquid", version=__version__, lifespan=lifespan,
                       instructions="Hermes interprets received sources. Python enforces user rules. Unknown exchange results are never automatically retried. Keep operation IDs stable.")
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
    async def status() -> dict:
        """Read effective user rules, sources, pending work, owned trades and unresolved operations."""
        snapshot = state.snapshot()
        ready = trading.ready()
        inventory = await trading.inventory_status()
        page = notification_page(snapshot, authority.key if authority else None, ready, require_key=False)
        return {"network": settings.network, "account": exchange.account, "paused": snapshot["paused"],
                "policy": snapshot["policy"], "policy_revision": snapshot["policy_revision"], "sources": snapshot["sources"],
                "source_names": snapshot.get("source_names", {}), "source_names_revision": snapshot.get("source_names_revision", 0),
                "pending": [row["event_id"] for row in snapshot["events"].values() if row["status"] in UNFINISHED],
                "trades": [row for row in snapshot["trades"].values() if row["status"] in OPEN],
                "unresolved_operations": [trading.public_operation(row) for row in snapshot["operations"].values() if row["phase"] in ("prepared", "sending", "unknown")],
                "notifications": page["notifications"], "notification_versions": page["notification_versions"],
                "notifications_page": {"pending": page["pending"], "next_cursor": page["next_cursor"], "pagination_available": authority is not None},
                "configuration_revision": snapshot["configuration_revision"], "profiles": snapshot["profiles"],
                "pipelines": snapshot["pipelines"], "exchanges": ready,
                "account_inventory": inventory,
                "account_recovery": recovery_diagnostics(snapshot),
                "execution_accounts": trading.accounts_ready(), "ranking_policy": snapshot["ranking_policy"],
                "ranking_revision": snapshot["ranking_revision"], "source_ranks": status_view(snapshot),
                "telegram_view": {**page["telegram_view"], "status": telegram_view({**snapshot, "notifications": {}}, trading.accounts_ready() if snapshot["ranking_policy"]["enabled"] else ready,
                                                                                 sheets_enabled=settings.google_sheets.enabled, inventory=inventory)["status"]},
                "journal": {"enabled": settings.google_sheets.enabled, "pending": len(snapshot.get("journal_pending", {})), "last_synced_at": snapshot.get("journal_synced_at")}}

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
        from aqla_agent.monthly import MonthlyJob, month_bounds
        from aqla_agent.monthly import monthly_report as calculate
        if isinstance(offset, bool) or isinstance(limit, bool) or offset < 0 or not 1 <= limit <= 500:
            raise AqlaError("Monthly page requires offset >= 0 and limit between 1 and 500")
        month_bounds(month)
        snapshot = state.snapshot()
        stored_job = snapshot.get("monthly_jobs", {}).get(month)
        diagnostic = MonthlyJob.model_validate(stored_job).model_dump(mode="json") if stored_job else None
        if diagnostics_only:
            return {"month": month, "publication_job": diagnostic}
        report = calculate(snapshot, month)
        totals = {key: len(report[key]) for key in ("rows", "open_trades", "undated_closed_trades")}
        return {**report, **{key: report[key][offset:offset + limit] for key in totals},
                "page": {"offset": offset, "limit": limit, "totals": totals}, "publication_job": diagnostic}

    @server.tool(annotations=read)
    async def trade_diagnostics(trade_id: str | None = None, symbol: str | None = None) -> dict:
        """Read source, exact sent order type, own fills, price checks and proved closure reason, including closed trades."""
        from aqla_agent.lifecycle import close_reason
        snapshot = state.snapshot()
        if trade_id is None:
            if not symbol:
                raise AqlaError("Supply an Aqla trade ID or a market symbol")
            candidates = [row for row in snapshot["trades"].values() if row["plan"]["market"]["symbol"].casefold() == symbol.casefold()]
            candidates.sort(key=lambda row: (row.get("closed_at", row["plan"]["created_at"]), row["trade_id"]), reverse=True)
            if len(candidates) != 1:
                return {"candidates": [{"trade_id": row["trade_id"], "market": row["plan"]["market"]["symbol"], "status": row["status"], "created_at": row["plan"]["created_at"], "closed_at": row.get("closed_at")} for row in candidates[:20]],
                        "selection_required": True}
            trade_id = candidates[0]["trade_id"]
        trade = snapshot["trades"].get(trade_id)
        if trade is None:
            raise AqlaError("Unknown Aqla trade ID")
        operations = [trading.public_operation(row) for row in snapshot["operations"].values()
                      if trade_id in row["operation_id"]]
        return {"trade_id": trade_id, "status": trade["status"], "plan": trade["plan"],
                "sources": [row["source"] for row in snapshot["events"].values() if row["source_id"] in trade["plan"]["source_refs"]],
                "source_names": snapshot.get("source_names", {}), "orders": trade["orders"], "exit_orders": trade.get("exit_orders", []),
                "order_observations": trade.get("order_observations", {}), "fill_events": trade.get("fill_events", {}),
                "entry_vwap": trade.get("entry_vwap"), "entry_accounting": trade.get("entry_accounting"),
                "initial_sl_risk": trade.get("initial_sl_risk"), "source_tp_finalization": trade.get("percent_finalization"),
                "source_tp_updates": trade.get("percent_updates", {}), "execution_incident": trade.get("execution_incident"),
                "price_checks": trade.get("price_checks", []), "waiting_snapshot": trade.get("waiting_snapshot"),
                "waiting_error": trade.get("waiting_error"), "unsent_reason": trade.get("unsent_reason"),
                "close_reason": trade.get("close_reason", close_reason(trade)), "lifecycle_events": trade.get("lifecycle_events", {}),
                "operations": operations, "historical_missing_quotes_are_not_inferred": True}

    @server.tool(annotations=read)
    async def source_context(event_id: str) -> dict:
        """Read source message, bounded same-channel/reply context and open trade bindings."""
        return trading.context(event_id)

    @server.tool(annotations=write)
    async def interpret_source(event_id: str, interpretation: Interpretation, context_auth: str = "") -> dict:
        """Save contextual classification/evidence for an actual received source; does not trade."""
        authorize(context_auth)
        return trading.record_interpretation(event_id, interpretation)

    @server.tool(annotations=write)
    async def trade_preview(event_id: str, interpretation: Interpretation, context_auth: str = "") -> dict:
        """Calculate and durably save an immutable source-bound plan under current user rules."""
        authorize(context_auth)
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
        return trading.public_operation(row)

    @server.tool(annotations=write)
    async def operation_reconcile(operation_id: str, context_auth: str = "") -> dict:
        """Read exact order identities and update evidence; never resubmit an exchange action."""
        authorize(context_auth)
        return await trading.reconcile(operation_id)

    @server.tool(annotations=read)
    async def notifications_read(cursor: str | None = None, limit: int = 100) -> NotificationReadResult:
        """Read a bounded versioned page; after ACK/stale begin again with no cursor."""
        # Notification messages use saved account/plan facts; no inventory/status
        # overview is rendered here, so another complete readiness snapshot is unnecessary.
        return state.notification_page(authority.key if authority else None, cursor=cursor, limit=limit)

    @server.tool(annotations=read)
    async def notifications_details(notification_id: str, version: str, cursor: str | None = None, limit: int = 16384) -> NotificationDetailsResult:
        """Read lossless canonical JSON fragments of the exact saved notification version."""
        return state.notification_details(authority.key if authority else None, notification_id, version, cursor=cursor, limit=limit)

    @server.tool(annotations=write)
    async def notifications_ack(notification_ids: list[str], context_auth: str = "", versions: dict[str, str] | None = None) -> NotificationAckResult:
        """Acknowledge status facts processed for the native Hermes response; not a Telegram delivery receipt."""
        authorize(context_auth)
        def acknowledge(data):
            return acknowledge_notifications(data, notification_ids, versions)
        return state.update_metadata(("notifications",), acknowledge)

    @server.tool(annotations=write)
    async def control(action: Literal["set_policy", "pause", "resume", "set_sources", "set_source_name", "close_trade", "clear_notification", "set_profile", "set_pipeline", "delete_profile", "delete_pipeline", "set_exchange_limits", "set_exchange_enabled", "set_ranking_policy", "set_account_limits", "set_account_enabled"],
                      expected_revision: int | None = None, policy: Policy | None = None, sources: list[str] | None = None,
                      trade_id: str | None = None, notification_id: str | None = None, context_auth: str = "",
                      profile: TradingProfile | None = None, pipeline: Pipeline | None = None, exchange_id: Venue | None = None,
                      limits: AccountLimits | None = None, enabled: bool | None = None, identity: str | None = None,
                      source_id: str | None = None, source_name: str | None = None,
                      ranking_policy: RankingPolicy | None = None, account_key: str | None = None) -> dict:
        """Apply an authenticated natural-language user request. Source turns cannot change rules."""
        claims = authorize(context_auth, operator=True)
        if action == "set_ranking_policy":
            if ranking_policy is None or expected_revision != state.snapshot()["ranking_revision"]:
                raise AqlaError("Supply the complete ranking policy and its current revision")
            def set_ranking(data):
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
                values["configuration_revision"] += 1
                return {"configuration_revision": values["configuration_revision"], "account": account_key,
                        "limits": values["account_limits"][account_key], "enabled": values["account_enabled"][account_key]}
            return state.update(set_account)
        if action == "set_source_name":
            from aqla_agent.sources import set_source_name
            return state.update(lambda data: set_source_name(data, source_id, source_name, expected_revision))
        if action == "close_trade":
            if trade_id is None:
                raise AqlaError("trade_id is required")
            return await trading.close_trade(trade_id, claims["request_id"])
        if action == "set_exchange_enabled" and enabled is True:
            if expected_revision != state.snapshot()["configuration_revision"]:
                raise AqlaError("Read the current configuration revision before activation")
            await trading.activate(exchange_id)
        if action in ("set_profile", "set_pipeline", "delete_profile", "delete_pipeline", "set_exchange_limits", "set_exchange_enabled"):
            return state.update(lambda data: route_control(data, action, expected_revision=expected_revision, profile=profile, pipeline=pipeline,
                                                          exchange=exchange_id, limits=limits, enabled=enabled, identity=identity))

        def apply(data):
            if action == "set_policy":
                if policy is None or expected_revision != data["policy_revision"]:
                    raise AqlaError("Read current rule revision and supply a complete validated policy")
                data["policy"] = policy.model_dump(mode="json")
                data["policy_revision"] += 1
                sync_legacy_policy(data)
            elif action in ("pause", "resume"):
                data["paused"] = action == "pause"
            elif action == "set_sources":
                if sources is None or any(not source for source in sources):
                    raise AqlaError("Supply the complete explicit source list")
                from aqla_agent.sources import resolve_source
                resolved = [resolve_source(data, item) for item in sources]
                data["sources"] = sorted(set(resolved))
                from aqla_agent.ranking import source_rank
                for source in data["sources"]:
                    source_rank(data, source)
                data["source_aliases"] = {key: value for key, value in data.get("source_aliases", {}).items() if key in data["sources"]}
            elif action == "clear_notification":
                data["notifications"].pop(notification_id, None)
            return {"policy": data["policy"], "policy_revision": data["policy_revision"], "paused": data["paused"], "sources": data["sources"]}

        return state.update(apply)

    @server.tool(annotations=write)
    async def integration(action: Literal["ingest", "native_forward", "snapshot", "resolve_sources", "seed_context", "journal_snapshot", "journal_ack", "monthly_job", "monthly_ack", "monthly_request", "monthly_failure", "monthly_page"], token: str, source: SourceEvent | None = None,
                          aliases: dict[str, str] | None = None, cutoffs: dict[str, int] | None = None,
                          context_ids: dict[str, list[int]] | None = None,
                          accepted: dict[str, str] | None = None, month: str | None = None,
                          publication: MonthlyPublication | None = None, job: MonthlyJobBinding | None = None,
                          failure: MonthlyFailure | None = None, section: MonthlySection | None = None,
                          offset: Annotated[int, Field(strict=True, ge=0)] = 0,
                          limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 100) -> dict:
        """Internal native-plugin access, filtered out of agent tools; token never enters model context."""
        if authority is None:
            raise AqlaError("Integration key is not configured")
        authority.internal(token)
        if action == "monthly_job":
            return state.monthly_transaction("job")
        if action == "monthly_failure":
            if failure is None:
                raise AqlaError("A bound monthly publication failure is required")
            return state.monthly_transaction("failure", failure=failure)
        if action == "monthly_page":
            if job is None or section is None:
                raise AqlaError("Monthly internal page requires its selected job and section")
            return state.monthly_transaction("page", binding=job, section=section, offset=offset, limit=limit)
        if action == "monthly_request":
            from datetime import datetime

            from aqla_agent.monthly import BERLIN, month_bounds
            month_bounds(month)
            if month > datetime.now(BERLIN).strftime("%Y-%m"):
                raise AqlaError("Zukünftige Monate haben noch keinen Monatsbericht")
            return state.monthly_transaction("request", month=month)
        if action == "monthly_ack":
            if publication is None or job is None:
                raise AqlaError("Confirmed Sheets publication and selected monthly job are required")
            import re
            expected = settings.google_sheets.spreadsheet_id
            if not re.fullmatch(r"https://docs\.google\.com/spreadsheets/d/" + re.escape(expected or "") + r"/edit#gid=\d+", publication.url):
                raise AqlaError("Monthly publication URL does not match the configured spreadsheet")
            return state.monthly_transaction("ack", month=month, fingerprint=publication.fingerprint,
                url=publication.url, revision=publication.revision, job=job)
        if action == "journal_snapshot":
            return state.journal_batch(settings.google_sheets.batch_size)
        if action == "journal_ack":
            if accepted is None:
                raise AqlaError("Missing journal acknowledgements")
            return state.acknowledge_journal(accepted)
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
        @server.tool(annotations=read)
        async def hl_info(request: InfoRequest) -> dict:
            """Typed public /info requests, including native perps, spot, HIP-3 and outcome data."""
            payload = validate_info(request)
            kind = payload.pop("type")
            result = await exchange.info(kind, **payload)
            return {"network": settings.network, "observed_at": now_ms(), "data": result,
                    "history_may_be_truncated": kind in ("userFills", "userFillsByTime", "historicalOrders", "userTwapSliceFills", "userFunding", "userNonFundingLedgerUpdates")}

        @server.tool(annotations=write)
        async def hl_exchange(operation_id: str, request: SdkRequest) -> dict:
            """Typed official SDK operations with durable identities and correct account/signing scope."""
            return await exchange.sdk_action(operation_id, request)

        @server.tool(annotations=write)
        async def hl_action(operation_id: str, request: NativeRequest) -> dict:
            """Strict native action contracts including non-SDK TWAP, trailing, staking and deployer variants."""
            return await exchange.native_action(operation_id, request)

        @server.tool(annotations=write)
        async def hl_multisig(action: Literal["prepare", "submit"], operation_id: str, multi_sig_user: Address,
                              request: SdkRequest | NativeRequest, nonce: int | None = None,
                              signatures: list[Signature] | None = None, expires_after: int | None = None) -> dict:
            """Prepare typed multisig signing data or verify collected signatures and submit exactly once."""
            from .multisig import prepare, submit
            if action == "prepare":
                return await prepare(exchange, operation_id, multi_sig_user, request, nonce, expires_after)
            return await submit(exchange, operation_id, multi_sig_user, request, nonce, signatures, expires_after)

        @server.tool(annotations=write)
        async def hl_stream(action: Literal["subscribe", "unsubscribe", "read", "post_info", "post_exchange", "post_action"],
                            subscription: Subscription | None = None, subscription_id: str | None = None,
                            after: int = 0, limit: int = 100, info_request: InfoRequest | None = None,
                            exchange_request: SdkRequest | None = None, operation_id: str | None = None,
                            native_request: NativeRequest | None = None) -> dict:
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
                return await exchange.native_action(operation_id, native_request, sender=lambda payload: streams.post("action", payload))
            if exchange_request is None or operation_id is None:
                raise AqlaError("A typed exchange request and stable operation_id are required")
            return await exchange.sdk_action(operation_id, exchange_request, sender=lambda payload: streams.post("action", payload))

    # Test/diagnostic handles, not MCP-exposed methods.
    server.aqla_state, server.aqla_exchange, server.aqla_trading = state, exchange, trading
    server.aqla_authority = authority
    server.aqla_streams = streams
    return server


async def close_server(server):
    with contextlib.suppress(Exception):
        await server.aqla_trading.close()
        await server.aqla_streams.close()
    server.aqla_state.close()
