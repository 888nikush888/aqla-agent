"""Coverage audit against an independent pinned documentation/SDK manifest."""
from __future__ import annotations

import json
from pathlib import Path

from .actions import NATIVE_MODELS, OutcomeOperation, PerpDeploy, StarProxy
from .catalog import INFO_MODELS, SDK_METHODS
from .streams import SUBSCRIPTION_MODELS

SDK_WIRE_HANDLERS = {
    "agentEnableDexAbstraction": "agent_enable_dex_abstraction", "agentSetAbstraction": "agent_set_abstraction",
    "approveBuilderFee": "approve_builder_fee", "scheduleCancel": "schedule_cancel", "sendAsset": "send_asset",
    "spotDeploy": "spot_deploy_register_token", "spotSend": "spot_transfer", "tokenDelegate": "token_delegate",
    "updateIsolatedMargin": "update_isolated_margin", "updateLeverage": "update_leverage",
    "usdClassTransfer": "usd_class_transfer", "usdSend": "usd_transfer", "userDexAbstraction": "user_dex_abstraction",
    "userSetAbstraction": "user_set_abstraction", "vaultTransfer": "vault_usd_transfer", "withdraw3": "withdraw_from_bridge",
    "noop": "noop",
}
SPECIAL_SDK = {"approve_agent": "hl_action:approveAgent", "multi_sig": "hl_multisig"}
EXPECTED_SUBSCRIPTIONS = {
    "allMids", "notification", "webData3", "twapStates", "clearinghouseState", "openOrders", "candle", "l2Book", "trades",
    "orderUpdates", "userEvents", "userFills", "userFundings", "userNonFundingLedgerUpdates", "activeAssetCtx", "activeAssetData",
    "userTwapSliceFills", "userTwapHistory", "bbo", "spotState", "allDexsClearinghouseState", "allDexsAssetCtxs", "outcomeMetaUpdates", "fastAssetCtxs",
}


def manifest():
    return json.loads(Path(__file__).with_name("api_manifest.json").read_text(encoding="utf-8"))


def audit():
    expected = manifest()
    sdk_handlers = {wire: method for wire, method in SDK_WIRE_HANDLERS.items() if method in SDK_METHODS}
    exchange_implemented = set(NATIVE_MODELS) | set(sdk_handlers) | {"multiSig"}
    missing = {
        "info": sorted(set(expected["expected_info_types"]) - set(INFO_MODELS)),
        "exchange": sorted(set(expected["expected_exchange_types"]) - exchange_implemented),
        "sdk": sorted(set(expected["expected_sdk_methods"]) - set(SDK_METHODS) - set(SPECIAL_SDK)),
        "subscriptions": sorted(EXPECTED_SUBSCRIPTIONS - set(SUBSCRIPTION_MODELS)),
    }
    # Verify models publish real strict input schemas, not only matching names.
    for model in [*INFO_MODELS.values(), *NATIVE_MODELS.values(), *SUBSCRIPTION_MODELS.values()]:
        schema = model.model_json_schema()
        if schema.get("additionalProperties") is not False:
            missing.setdefault("invalid_schemas", []).append(model.__name__)
    return {"complete": not any(missing.values()), "missing": missing, "scope": expected["scope"],
            "snapshot_date": expected["snapshot_date"], "info_types": len(INFO_MODELS), "exchange_types": len(expected["expected_exchange_types"]),
            "sdk_operations": len(expected["expected_sdk_methods"]), "subscriptions": len(SUBSCRIPTION_MODELS),
            "deployer_variants": {"perp": len(PerpDeploy.variants), "outcome": len(OutcomeOperation.variants), "star_proxy": len(StarProxy.variants)},
            "contract_tests_required": True, "live_exchange_verified": False}


def markdown():
    expected = manifest()
    lines = ["# Hyperliquid API-Abdeckung", "", f"Vertragsstand: {expected['snapshot_date']}. Die Matrix beschreibt implementierte, typisierte Verträge; keine behauptete Live-Börsenabnahme.", "",
             expected["scope"], "", "| Familie | Vertrag | Werkzeug/Handler | Prüfdatei |", "|---|---|---|---|"]
    for name in expected["expected_info_types"]:
        lines.append(f"| Info | `{name}` | `hl_info` / validierter InfoRequest | `test_api_contracts.py` |")
    for name in expected["expected_exchange_types"]:
        handler = "hl_action" if name in NATIVE_MODELS else "hl_multisig" if name == "multiSig" else "hl_exchange:" + SDK_WIRE_HANDLERS.get(name, "MISSING")
        test = "test_native_actions.py" if name in NATIVE_MODELS else "test_multisig.py" if name == "multiSig" else "test_sdk_actions.py"
        lines.append(f"| Exchange | `{name}` | `{handler}` | `{test}` |")
    for name in expected["expected_sdk_methods"]:
        handler = "hl_exchange:" + name if name in SDK_METHODS else SPECIAL_SDK.get(name, "MISSING")
        lines.append(f"| Python-SDK | `{name}` | `{handler}` | `test_sdk_actions.py` / Spezialvertrag |")
    for name in sorted(EXPECTED_SUBSCRIPTIONS):
        lines.append(f"| WebSocket | `{name}` | `hl_stream` | `test_streams.py` |")
    for family, variants in (("HIP-3", PerpDeploy.variants), ("HIP-4", OutcomeOperation.variants), ("HIP-3 Star", StarProxy.variants)):
        for name in variants:
            lines.append(f"| {family} | `{name}` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |")
    lines += ["", "Preis-/Mengenraster, Signerrollen, Netzwerk-, Deployer- und Testnet-Grenzen bleiben maßgeblich. Ein API-Wallet hat keine Masterwallet-Befugnisse.",
              "", "WebSocket-Posts unterstützen Info, SDK-Aktionen und native Aktionen. Reconnects abonnieren Reads erneut, senden Writes aber niemals erneut.",
              "", "Die unabhängige Manifestdatei enthält Dokumentprüfsummen und Quellcommits. `--check-coverage` vergleicht diese erwarteten Verträge mit den vorhandenen Handlern und Eingabeschemas; Contracttests prüfen deren tatsächliche Wire-/Signingpfade.",
              "", "Primärquellen: [Hyperliquid API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api), [HIP-3](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/hip-3-deployer-actions), [HIP-4](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/hip-4-deployer-actions), [Python-SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/tree/2fdb18f9517675ea03695a0962bd19eece9c83f0).", ""]
    return "\n".join(lines)
