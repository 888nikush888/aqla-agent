from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from .actions import NATIVE_MODELS, NativeRequest
from .api import Hyperliquid
from .auth import Authority
from .catalog import INFO_MODELS, SDK_MODELS, InfoRequest, SdkRequest, validate_info
from .coverage import audit
from .models import Address, AqlaError, Interpretation, Policy, Settings, SourceEvent, now_ms
from .multisig import Signature
from .state import State
from .streams import SUBSCRIPTION_MODELS, Streams, Subscription
from .trades import OPEN, UNFINISHED, Trading


def create_server(settings: Settings, *, profile="agent", exchange=None, state=None):
    state = state or State(settings)
    exchange = exchange or Hyperliquid(settings, state)
    trading = Trading(exchange, state)
    streams = Streams(settings.base_url, timeout=settings.timeout_seconds)
    authority = Authority.from_file(settings.integration_key_file) if settings.integration_key_file else None

    def authorize(token, operator=False):
        if authority is None:
            raise AqlaError("Trusted Hermes integration key is not configured")
        return authority.verify(token, operator=operator)

    async def maintenance():
        while True:
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
            await asyncio.sleep(10)

    @asynccontextmanager
    async def lifespan(_):
        await trading.recover()
        task = asyncio.create_task(maintenance(), name="aqla-protection")
        try:
            yield None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await exchange.close()
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
                "state": "atomic_json_single_writer", "source_images": False,
                "automatic_source_products": ["perp"],
                "info_types": sorted(INFO_MODELS), "sdk_exchange_operations": sorted(SDK_MODELS),
                "native_action_types": sorted(NATIVE_MODELS),
                "subscriptions": sorted(SUBSCRIPTION_MODELS),
                "coverage_complete": audit()["complete"], "pending_coverage": audit()["missing"], "coverage_scope": audit()["scope"],
                "exchange_atomicity_guaranteed": False, "live_exchange_verified": False}

    @server.tool(annotations=read)
    async def status() -> dict:
        """Read effective user rules, sources, pending work, owned trades and unresolved operations."""
        snapshot = state.snapshot()
        return {"network": settings.network, "account": exchange.account, "paused": snapshot["paused"],
                "policy": snapshot["policy"], "policy_revision": snapshot["policy_revision"], "sources": snapshot["sources"],
                "pending": [row["event_id"] for row in snapshot["events"].values() if row["status"] in UNFINISHED],
                "trades": [row for row in snapshot["trades"].values() if row["status"] in OPEN],
                "unresolved_operations": [row for row in snapshot["operations"].values() if row["phase"] in ("prepared", "sending", "unknown")],
                "notifications": snapshot["notifications"]}

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
        return await trading.preview(event_id, interpretation)

    @server.tool(annotations=write)
    async def trade_execute(plan_id: str, context_auth: str = "") -> dict:
        """Execute exactly the saved plan once; repeated IDs never dispatch a second entry."""
        authorize(context_auth)
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
        return row

    @server.tool(annotations=write)
    async def operation_reconcile(operation_id: str, context_auth: str = "") -> dict:
        """Read exact order identities and update evidence; never resubmit an exchange action."""
        authorize(context_auth)
        return await exchange.reconcile(operation_id)

    @server.tool(annotations=write)
    async def notifications_ack(notification_ids: list[str], context_auth: str = "") -> dict:
        """Acknowledge status facts processed for the native Hermes response; not a Telegram delivery receipt."""
        authorize(context_auth)
        def acknowledge(data):
            removed = []
            for identity in notification_ids:
                if data["notifications"].pop(identity, None) is not None:
                    removed.append(identity)
            return {"processed": removed, "delivery_owned_by": "Hermes"}
        return state.update(acknowledge)

    @server.tool(annotations=write)
    async def control(action: Literal["set_policy", "pause", "resume", "set_sources", "close_trade", "clear_notification"],
                      expected_revision: int | None = None, policy: Policy | None = None, sources: list[str] | None = None,
                      trade_id: str | None = None, notification_id: str | None = None, context_auth: str = "") -> dict:
        """Apply an authenticated natural-language user request. Source turns cannot change rules."""
        claims = authorize(context_auth, operator=True)
        if action == "close_trade":
            if trade_id is None:
                raise AqlaError("trade_id is required")
            async with trading.lock:
                return await trading.close_position(trade_id, Decimal(100), "operator-close:" + claims["request_id"])

        def apply(data):
            if action == "set_policy":
                if policy is None or expected_revision != data["policy_revision"]:
                    raise AqlaError("Read current rule revision and supply a complete validated policy")
                data["policy"] = policy.model_dump(mode="json")
                data["policy_revision"] += 1
            elif action in ("pause", "resume"):
                data["paused"] = action == "pause"
            elif action == "set_sources":
                if sources is None or any(not source for source in sources):
                    raise AqlaError("Supply the complete explicit source list")
                data["sources"] = sorted(set(sources))
                data["source_aliases"] = {key: value for key, value in data.get("source_aliases", {}).items() if key in data["sources"]}
            elif action == "clear_notification":
                data["notifications"].pop(notification_id, None)
            return {"policy": data["policy"], "policy_revision": data["policy_revision"], "paused": data["paused"], "sources": data["sources"]}

        return state.update(apply)

    @server.tool(annotations=write)
    async def integration(action: Literal["ingest", "native_forward", "snapshot", "resolve_sources", "seed_context"], token: str, source: SourceEvent | None = None,
                          aliases: dict[str, str] | None = None, cutoffs: dict[str, int] | None = None) -> dict:
        """Internal native-plugin access, filtered out of agent tools; token never enters model context."""
        if authority is None:
            raise AqlaError("Integration key is not configured")
        authority.internal(token)
        if action == "seed_context":
            snapshot = state.snapshot()
            allowed = {*snapshot["sources"], *snapshot.get("source_aliases", {}).values()}
            if cutoffs is None or any(chat not in allowed or cutoff < 0 for chat, cutoff in cutoffs.items()):
                raise AqlaError("Context cutoffs must identify subscribed source messages")
            def seed(data):
                for chat, cutoff in cutoffs.items():
                    data.setdefault("context_cutoffs", {}).setdefault(chat, cutoff)
                    data["cursors"].setdefault(chat, 0)
                return data["context_cutoffs"]
            return {"context_cutoffs": state.update(seed)}
        if action == "resolve_sources":
            selected = state.snapshot()["sources"]
            if aliases is None or any(key not in selected or not value.lstrip("-").isdigit() for key, value in aliases.items()):
                raise AqlaError("Reader source resolution must bind only configured sources to actual Telegram IDs")
            state.update(lambda data: data.update(source_aliases=aliases))
            return {"source_aliases": aliases}
        if action in ("ingest", "native_forward"):
            if source is None:
                raise AqlaError("Missing source event")
            return trading.ingest(source, native_forward=action == "native_forward")
        snapshot = state.snapshot()
        tracked = {}
        active_refs = {ref for trade in snapshot["trades"].values() if trade["status"] in OPEN for ref in trade["plan"]["source_refs"]}
        for row in snapshot["events"].values():
            chat = row["source"]["chat_id"]
            tracked.setdefault(chat, []).append(row["source"]["message_id"])
        for chat, values in tracked.items():
            recent = sorted(set(values))[-settings.max_context_messages:]
            active = [row["source"]["message_id"] for row in snapshot["events"].values() if row["source"]["chat_id"] == chat and row["source_id"] in active_refs]
            tracked[chat] = sorted(set([*recent, *active]))
        return {"sources": snapshot["sources"], "cursors": snapshot["cursors"],
                "context_cutoffs": snapshot.get("context_cutoffs", {}),
                "source_aliases": snapshot.get("source_aliases", {}), "tracked_ids": tracked,
                "pending": [row["event_id"] for row in snapshot["events"].values() if row["status"] in UNFINISHED][:50],
                "notifications": snapshot["notifications"]}

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
        await server.aqla_exchange.close()
        await server.aqla_streams.close()
    server.aqla_state.close()
