"""Normalized venue contracts and durable dispatch for the new exchanges."""
from __future__ import annotations

import asyncio
import copy
from decimal import Decimal
from types import SimpleNamespace

from aqla_hyperliquid_mcp.models import TERMINAL_ORDER, AccountSnapshot, AqlaError, digest, now_ms


def adapter_settings(root, venue):
    selected = root.venues.get(venue)
    if selected is None:
        return None
    account = selected.account if venue == "aster" else str(selected.account_index) if selected.account_index is not None else None
    if not account or not selected.private_key_file or not selected.private_key_file.is_file():
        return None
    return SimpleNamespace(**{**root.model_dump(), **selected.model_dump(), "account": account,
                              "telegram_owner_id": root.telegram_owner_id, "vault_address": None, "venue": venue})


class VenueAdapter:
    """Canonical order intents; each signed leg is checkpointed before its only send."""
    fail_closed_protection = True

    def __init__(self, settings, state):
        self.settings, self.state = settings, state
        self.account = settings.account.lower()
        self.mutations = asyncio.Lock()
        self.market_cache = {}
        self.wallet = True

    def cloid(self, operation, index=0):
        return "0x" + digest([self.settings.venue, self.settings.network, self.account, operation, index])[:32]

    async def info(self, kind, **params):
        if kind == "clearinghouseState":
            return await self.positions()
        if kind == "userFillsByTime":
            return await self.fills(params["startTime"], params["endTime"])
        raise AqlaError("Unsupported internal venue observation")

    async def account_snapshot(self, market):
        raw = await self.positions()
        row = next((item["position"] for item in raw["assetPositions"] if item["position"]["coin"] == market.symbol), None)
        return AccountSnapshot(equity=raw["marginSummary"]["accountValue"], available_margin=raw["withdrawable"],
                               position_size=Decimal(row["szi"]) if row else Decimal(0))

    def native_market(self, asset):
        row = next((market for market in self.market_cache.values() if market.asset_id == asset), None)
        if row is None:
            raise AqlaError("Resolve the immutable native market before signing")
        return row

    async def mutate(self, operation_id, action, *, deadline=None, prepare_guard=None, async_prepare_guard=None):
        async with self.mutations:
            request = {"network": self.settings.network, "account": self.account, "action": action, "deadline": deadline}
            existing = self.state.snapshot()["operations"].get(operation_id)
            if existing:
                if existing["request_hash"] != digest(request):
                    raise AqlaError("An operation identity is already bound to a different request")
                return existing
            if deadline is not None and now_ms() >= deadline:
                raise AqlaError("Entry deadline expired before signing")
            await self.verify_signer()
            if async_prepare_guard:
                await async_prepare_guard()
            signed = await self.build_requests(action, deadline)
            record = {"operation_id": operation_id, "exchange": self.settings.venue, "request": request, "request_hash": digest(request),
                      "wire_action": copy.deepcopy(action), "nonce": signed[0]["nonce"] if signed else 0,
                      "expires_after": deadline, "created_at": now_ms(), "phase": "prepared", "result": None,
                      "signed_requests": signed, "legs": []}
            def prepare(data):
                if prepare_guard:
                    prepare_guard(data)
                if deadline is not None and now_ms() >= deadline:
                    raise AqlaError("Entry deadline expired before persistence")
                self.state.assert_operation_binding(self.settings, record)
                data["operations"][operation_id] = record
            self.state.update(prepare)
            if async_prepare_guard:
                try:
                    await async_prepare_guard()
                except BaseException as error:
                    message = str(error) if isinstance(error, AqlaError) else "Entry guard failed before any exchange send"
                    self.state.update(lambda data: data["operations"][operation_id].update(phase="rejected", outcome="not_sent", result={"error": message}))
                    if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                        raise
                    return self.state.snapshot()["operations"][operation_id]
            try:
                def begin_send(data):
                    self.state.assert_operation_binding(self.settings, record)
                    if prepare_guard:
                        prepare_guard(data)
                    if deadline is not None and now_ms() >= deadline:
                        raise AqlaError("Entry deadline expired before exchange send")
                    data["operations"][operation_id].update(phase="sending")
                self.state.update(begin_send)
            except AqlaError as error:
                message = str(error)
                self.state.update(lambda data: data["operations"][operation_id].update(phase="rejected", outcome="not_sent", result={"error": message}))
                return self.state.snapshot()["operations"][operation_id]
            try:
                responses = []
                for item in signed:
                    # No send of later legs when the entry was definitively rejected.
                    response = await self.send(item)
                    responses.append(response)
                    legs = [{"index": index, "disposition": "rejected" if self.rejected(value) else "accepted", "status": value} for index, value in enumerate(responses)] if action["type"] == "order" and len(signed) == len(action["orders"]) else []
                    self.state.update(lambda data, rows=responses, legs=legs: data["operations"][operation_id].update(result=rows, legs=legs))
                    if self.rejected(response):
                        break
                rejected = any(self.rejected(row) for row in responses)
                accepted = any(not self.rejected(row) for row in responses)
                phase = "unknown" if any(row.get("pending") for row in responses) else "rejected" if rejected and (not accepted or action["type"] != "order") else "acknowledged"
                self.state.update(lambda data: data["operations"][operation_id].update(phase=phase, outcome="partial" if rejected and accepted else "rejected" if rejected else "accepted"))
            except BaseException as error:
                self.state.update(lambda data: data["operations"][operation_id].update(phase="unknown", result={"error": "Exchange outcome unknown; reconcile exact identities, do not resubmit"}))
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
            return self.state.snapshot()["operations"][operation_id]

    async def reconcile(self, operation_id):
        row = self.state.snapshot()["operations"].get(operation_id)
        if row is None:
            raise AqlaError("Unknown exchange operation")
        self.state.assert_operation_binding(self.settings, row)
        action = row["wire_action"]
        identities = [order["c"] for order in action.get("orders", [])]
        if action["type"] == "cancelByCloid":
            identities = [item["cloid"] for item in action["cancels"]]
        if action["type"] == "cancel":
            identities = [item["o"] for item in action["cancels"]]
        observations = [await self.order_status(identity) for identity in identities]
        proved = bool(identities) and all(item.get("status") == "order" and item["order"]["status"] not in ("unknownOid", None)
                                       and (action["type"] not in ("cancel", "cancelByCloid") or item["order"]["status"] in TERMINAL_ORDER) for item in observations)
        if not identities and row.get("signed_requests"):
            proved = await self.prove_configuration(row)
        if proved:
            self.state.update(lambda data: data["operations"][operation_id].update(phase="reconciled", evidence=observations))
        return {"operation": self.public_operation(self.state.snapshot()["operations"][operation_id]), "resolved": proved,
                "evidence": observations, "resubmitted": False, "absence_is_proof": False}

    @staticmethod
    def public_operation(row):
        if isinstance(row, list):
            return [VenueAdapter.public_operation(value) for value in row]
        if isinstance(row, dict):
            return {key: VenueAdapter.public_operation(value) for key, value in row.items() if key != "signed_requests"}
        return row

    async def prove_configuration(self, row):
        return False

    @staticmethod
    def rejected(response):
        return isinstance(response, dict) and isinstance(response.get("code"), int) and response["code"] not in (0, 200)

    async def close(self):
        await self.client.aclose()
