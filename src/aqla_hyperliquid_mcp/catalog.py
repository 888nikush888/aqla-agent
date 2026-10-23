"""Finite, typed API contracts. Models contain only documented operation fields."""
from __future__ import annotations

import inspect
import json
import types
from functools import reduce
from operator import or_
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, get_args, get_origin, get_type_hints, is_typeddict

from hyperliquid.exchange import Exchange
from hyperliquid.utils.types import Cloid
from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, TypeAdapter, create_model

from .models import Address, AqlaError, Model

Interval = Literal["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "3d", "1w", "1M"]
CloidValue = Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{32}$")]
Coin = Annotated[str, Field(min_length=1, max_length=100)]


class CandleRequest(Model):
    coin: Coin
    interval: Interval
    startTime: int = Field(ge=0)
    endTime: int = Field(ge=0)


INFO_SPECS = json.loads(Path(__file__).with_name("info_specs.json").read_text(encoding="utf-8"))


def make_info_models():
    result = {}
    for name, spec in INFO_SPECS.items():
        fields = {"type": (Literal[name], name)}
        for key, item in spec["fields"].items():
            if key in ("user", "builder", "vaultAddress"):
                annotation = Address
            elif key == "req":
                annotation = CandleRequest
            elif key == "oid":
                annotation = Annotated[int, Field(gt=0, lt=2**64)] | CloidValue
            elif item["type"].lower() in ("number", "int", "uint64"):
                annotation = Annotated[int, Field(ge=0)]
            elif item["type"].lower() in ("bool", "boolean"):
                annotation = StrictBool
            else:
                annotation = StrictStr
            if item["optional"]:
                fields[key] = (annotation | None, None)
            else:
                fields[key] = (annotation, ...)
        result[name] = create_model("Info" + name[:1].upper() + name[1:], __base__=Model, **fields)
    return result


INFO_MODELS = make_info_models()
if TYPE_CHECKING:
    InfoRequest = Model
else:
    # Runtime schemas come from the pinned native API catalog.
    InfoRequest = Annotated[reduce(or_, INFO_MODELS.values()), Field(discriminator="type")]
INFO_ADAPTER: TypeAdapter[Model] = TypeAdapter(InfoRequest)


def validate_info(request):
    model = INFO_ADAPTER.validate_python(request)
    data = model.model_dump(mode="json", exclude_none=True)
    window = data.get("req", data)
    if window.get("startTime", 0) > window.get("endTime", 2**63 - 1):
        raise AqlaError("History window is reversed")
    if "nSigFigs" in data and data["nSigFigs"] not in (2, 3, 4, 5):
        raise AqlaError("nSigFigs must be 2, 3, 4 or 5")
    if "mantissa" in data and (data.get("nSigFigs") != 5 or data["mantissa"] not in (1, 2, 5)):
        raise AqlaError("mantissa requires five significant figures and value 1, 2 or 5")
    return data


# Fixed SDK allowlist; an input name can never select an arbitrary Python attribute.
SDK_NAMES = (
    "order", "bulk_orders", "modify_order", "bulk_modify_orders_new", "market_open", "market_close",
    "cancel", "cancel_by_cloid", "bulk_cancel", "bulk_cancel_by_cloid", "schedule_cancel",
    "update_leverage", "update_isolated_margin", "set_referrer", "create_sub_account", "usd_class_transfer",
    "send_asset", "sub_account_transfer", "sub_account_spot_transfer", "vault_usd_transfer", "usd_transfer",
    "spot_transfer", "token_delegate", "withdraw_from_bridge", "approve_builder_fee", "convert_to_multi_sig_user",
    "spot_deploy_register_token", "spot_deploy_user_genesis", "spot_deploy_enable_freeze_privilege",
    "spot_deploy_freeze_user", "spot_deploy_revoke_freeze_privilege", "spot_deploy_enable_quote_token",
    "spot_deploy_genesis", "spot_deploy_register_spot", "spot_deploy_register_hyperliquidity",
    "spot_deploy_set_deployer_trading_fee_share", "perp_deploy_register_asset", "perp_deploy_set_oracle",
    "c_signer_unjail_self", "c_signer_jail_self", "c_validator_register", "c_validator_change_profile",
    "c_validator_unregister", "use_big_blocks", "agent_enable_dex_abstraction", "agent_set_abstraction",
    "user_dex_abstraction", "user_set_abstraction", "noop", "gossip_priority_bid",
)
SDK_METHODS = {name: vars(Exchange)[name] for name in SDK_NAMES}
SDK_HELPERS = {name: vars(Exchange)[name] for name in ("c_signer_inner", "spot_deploy_token_action_inner")}
_converted: dict[object, type[Model]] = {}


def sdk_type(annotation):
    if annotation in (str, int, bool, float):
        return {str: StrictStr, int: StrictInt, bool: StrictBool, float: StrictFloat}[annotation]
    if annotation is Cloid:
        return CloidValue
    if annotation is type(None):
        return type(None)
    if is_typeddict(annotation):
        if annotation in _converted:
            return _converted[annotation]
        hints = get_type_hints(annotation, include_extras=True)
        fields = {}
        for key, item in hints.items():
            required = key in annotation.__required_keys__ or (annotation.__name__ == "OrderRequest" and key != "cloid") or annotation.__name__ == "ModifyRequest"
            if get_origin(item) and getattr(get_origin(item), "__name__", "") in ("NotRequired", "Required"):
                item = get_args(item)[0]
            fields[key] = (sdk_type(item), ... if required else None)
        model = create_model("SDK" + annotation.__name__, __base__=Model, **fields)
        _converted[annotation] = model
        return model
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Literal:
        return annotation
    if origin in (types.UnionType, __import__("typing").Union):
        return reduce(or_, (sdk_type(item) for item in args))
    if origin is list:
        return list[sdk_type(args[0])]
    if origin is dict:
        return dict[sdk_type(args[0]), sdk_type(args[1])]
    if origin is tuple:
        return tuple[tuple(sdk_type(item) for item in args)]
    raise TypeError(f"SDK annotation has no explicit API schema: {annotation}")


def make_sdk_models():
    models = {}
    unannotated = {"noop": {"nonce": int}, "gossip_priority_bid": {"slot_id": int, "ip": str, "max_gas": int}}
    for name, method in SDK_METHODS.items():
        hints = get_type_hints(method)
        fields = {"operation": (Literal[name], name)}
        for key, parameter in inspect.signature(method).parameters.items():
            if key == "self":
                continue
            annotation = sdk_type(hints.get(key, unannotated.get(name, {}).get(key)))
            if key == "schema":
                fields["schema_"] = (annotation, Field(alias="schema"))
            else:
                fields[key] = (annotation, ... if parameter.default is inspect.Parameter.empty else parameter.default)
        models[name] = create_model("Action" + "".join(part.title() for part in name.split("_")), __base__=Model, **fields)
    return models


SDK_MODELS = make_sdk_models()
if TYPE_CHECKING:
    SdkRequest = Model
else:
    # Runtime schemas come from the pinned native API catalog.
    SdkRequest = Annotated[reduce(or_, SDK_MODELS.values()), Field(discriminator="operation")]
SDK_ADAPTER: TypeAdapter[Model] = TypeAdapter(SdkRequest)


def validate_sdk(request):
    model = SDK_ADAPTER.validate_python(request)
    values = model.model_dump(mode="python", by_alias=True)
    for key in ("sz", "limit_px", "amount", "order_sz", "start_px"):
        if key in values and values[key] is not None and values[key] <= 0 and not (model.operation == "update_isolated_margin" and key == "amount"):
            raise AqlaError(f"{key} must be positive")
    if "leverage" in values and values["leverage"] < 1:
        raise AqlaError("Leverage must be positive")

    def nested(value):
        if isinstance(value, dict):
            if "limit" in value or "trigger" in value:
                present = [key for key in ("limit", "trigger") if value.get(key) is not None]
                if len(present) != 1:
                    raise AqlaError("Order type requires exactly one limit or trigger contract")
            for key in ("sz", "limit_px"):
                if value.get("operation") == "market_close" and key == "sz" and value.get(key) is None:
                    continue
                if key in value and (value[key] is None or value[key] <= 0):
                    raise AqlaError(f"Order {key} must be positive")
            for item in value.values():
                nested(item)
        elif isinstance(value, list):
            for item in value:
                nested(item)

    nested(values)
    return values


def cloid_arguments(value, key="", *, top=True):
    if isinstance(value, dict):
        return {name: cloid_arguments(item, name, top=False) for name, item in value.items() if top or item is not None}
    if isinstance(value, list):
        return [cloid_arguments(item, key, top=False) for item in value]
    if key in ("cloid", "oid") and isinstance(value, str) and value.startswith("0x"):
        return Cloid.from_str(value)
    return value
