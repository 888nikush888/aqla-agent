"""Exercise every allowlisted SDK handler against the controlled wire boundary."""
import copy

import pytest

from aqla_hyperliquid_mcp.catalog import SDK_MODELS, validate_sdk
from aqla_hyperliquid_mcp.models import now_ms

A = "0x" + "1" * 40
ORDER = {"coin": "BTC", "is_buy": True, "sz": 1.0, "limit_px": 100.0, "order_type": {"limit": {"tif": "Gtc"}}, "reduce_only": False}
CASES = {
    "order": {"name": "BTC", "is_buy": True, "sz": 1.0, "limit_px": 100.0, "order_type": {"limit": {"tif": "Gtc"}}},
    "bulk_orders": {"order_requests": [ORDER]},
    "modify_order": {"oid": 1, "name": "BTC", "is_buy": True, "sz": 1.0, "limit_px": 100.0, "order_type": {"limit": {"tif": "Gtc"}}},
    "bulk_modify_orders_new": {"modify_requests": [{"oid": 1, "order": ORDER}]},
    "market_open": {"name": "BTC", "is_buy": True, "sz": 1.0},
    "market_close": {"coin": "BTC"},
    "cancel": {"name": "BTC", "oid": 1},
    "cancel_by_cloid": {"name": "BTC", "cloid": "0x" + "1" * 32},
    "bulk_cancel": {"cancel_requests": [{"coin": "BTC", "oid": 1}]},
    "bulk_cancel_by_cloid": {"cancel_requests": [{"coin": "BTC", "cloid": "0x" + "1" * 32}]},
    "schedule_cancel": {"time": None},
    "update_leverage": {"name": "BTC", "leverage": 2},
    "update_isolated_margin": {"name": "BTC", "amount": -1.0},
    "set_referrer": {"code": "CODE"},
    "create_sub_account": {"name": "test-subaccount"},
    "usd_class_transfer": {"amount": 1.0, "to_perp": True},
    "send_asset": {"destination": A, "source_dex": "", "destination_dex": "xyz", "token": "USDC", "amount": 1.0},
    "sub_account_transfer": {"sub_account_user": A, "is_deposit": True, "usd": 1000000},
    "sub_account_spot_transfer": {"sub_account_user": A, "is_deposit": True, "token": "USDC", "amount": 1.0},
    "vault_usd_transfer": {"vault_address": A, "is_deposit": True, "usd": 1000000},
    "usd_transfer": {"amount": 1.0, "destination": A},
    "spot_transfer": {"amount": 1.0, "destination": A, "token": "USDC"},
    "token_delegate": {"validator": A, "wei": 1, "is_undelegate": False},
    "withdraw_from_bridge": {"amount": 1.0, "destination": A},
    "approve_builder_fee": {"builder": A, "max_fee_rate": "0.01%"},
    "convert_to_multi_sig_user": {"authorized_users": [A], "threshold": 1},
    "spot_deploy_register_token": {"token_name": "ABC", "sz_decimals": 2, "wei_decimals": 8, "max_gas": 1, "full_name": "ABC token"},
    "spot_deploy_user_genesis": {"token": 1, "user_and_wei": [(A, "1")], "existing_token_and_wei": []},
    "spot_deploy_enable_freeze_privilege": {"token": 1},
    "spot_deploy_freeze_user": {"token": 1, "user": A, "freeze": True},
    "spot_deploy_revoke_freeze_privilege": {"token": 1},
    "spot_deploy_enable_quote_token": {"token": 1},
    "spot_deploy_genesis": {"token": 1, "max_supply": "10000", "no_hyperliquidity": True},
    "spot_deploy_register_spot": {"base_token": 1, "quote_token": 0},
    "spot_deploy_register_hyperliquidity": {"spot": 1, "start_px": 1.0, "order_sz": 1.0, "n_orders": 1, "n_seeded_levels": None},
    "spot_deploy_set_deployer_trading_fee_share": {"token": 1, "share": "0.1"},
    "perp_deploy_register_asset": {"dex": "xyz", "max_gas": None, "coin": "xyz:BTC", "sz_decimals": 2, "oracle_px": "100", "margin_table_id": 1, "only_isolated": True, "schema": None},
    "perp_deploy_set_oracle": {"dex": "xyz", "oracle_pxs": {"BTC": "100"}, "all_mark_pxs": [{"BTC": "100"}], "external_perp_pxs": {"BTC": "100"}},
    "c_signer_unjail_self": {},
    "c_signer_jail_self": {},
    "c_validator_register": {"node_ip": "127.0.0.1", "name": "test", "description": "test", "delegations_disabled": True, "commission_bps": 1, "signer": A, "unjailed": True, "initial_wei": 1},
    "c_validator_change_profile": {"node_ip": None, "name": "test", "description": None, "unjailed": True, "disable_delegations": None, "commission_bps": None, "signer": None},
    "c_validator_unregister": {},
    "use_big_blocks": {"enable": True},
    "agent_enable_dex_abstraction": {},
    "agent_set_abstraction": {"abstraction": "i"},
    "user_dex_abstraction": {"user": A, "enabled": True},
    "user_set_abstraction": {"user": A, "abstraction": "disabled"},
    "noop": {"nonce": now_ms()},
    "gossip_priority_bid": {"slot_id": 1, "ip": "127.0.0.1", "max_gas": 1},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,params", CASES.items())
async def test_each_sdk_operation_produces_one_bound_signed_dispatch(setup, operation, params):
    settings, state, fake, exchange, _ = setup
    settings.signer_role = "master"
    fake.position = 1.0 if operation == "market_close" else 0.0
    request = {"operation": operation, **copy.deepcopy(params)}
    record = await exchange.sdk_action("sdk:" + operation, request)
    assert record["phase"] == "acknowledged"
    assert len(fake.actions) == 1
    assert record["nonce"] == state.snapshot()["nonces"]["testnet:" + exchange.wallet.address.lower()]
    assert record["wire_action"] == fake.actions[0]
    again = await exchange.sdk_action("sdk:" + operation, request)
    assert again["operation_id"] == record["operation_id"]
    assert len(fake.actions) == 1


def test_fixture_set_matches_explicit_callable_allowlist():
    assert set(CASES) == set(SDK_MODELS)


def test_invalid_nested_orders_fail_before_reaching_sdk():
    with pytest.raises(ValueError):
        validate_sdk({"operation": "bulk_orders", "order_requests": [{}]})
    with pytest.raises(ValueError):
        validate_sdk({"operation": "bulk_orders", "order_requests": [{**ORDER, "order_type": {"limit": {"tif": "Gtc"}, "trigger": {"isMarket": True, "triggerPx": 90.0, "tpsl": "sl"}}}]})
    with pytest.raises(ValueError):
        validate_sdk({"operation": "market_open", "name": "BTC", "is_buy": True, "sz": float("nan")})
