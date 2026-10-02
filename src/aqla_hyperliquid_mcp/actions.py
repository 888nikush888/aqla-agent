"""Additional native HyperCore contracts missing from the pinned Python SDK.

Field and variant names follow the official Exchange/HIP-3/HIP-4 documentation.
Signing fields for user-signed actions are cross-checked against the maintained
SDK source at nktkas/hyperliquid commit db80d598f6e4672edc090fe69994ecec97ebc980.
"""
from __future__ import annotations

from decimal import Decimal
from functools import reduce
from operator import or_
from typing import Annotated, ClassVar, Literal

from hyperliquid.utils import signing
from pydantic import AfterValidator, Field, StrictBool, StrictInt, TypeAdapter, create_model, model_validator

from .catalog import CloidValue, Coin
from .models import Address, AqlaError, Model

UInt = Annotated[StrictInt, Field(ge=0, lt=2**64)]
Asset = Annotated[StrictInt, Field(ge=0, lt=2**32)]
HexBytes = Annotated[str, Field(pattern=r"^0x(?:[0-9a-fA-F]{2})*$")]
DecimalString = Annotated[str, Field(pattern=r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")]
UnsignedString = Annotated[str, Field(pattern=r"^(?:0|[1-9]\d*)(?:\.\d+)?$")]
Venue = Annotated[str, Field(pattern=r"^[a-z]{2,4}$")]


def nonzero(value):
    if Decimal(value) <= 0:
        raise ValueError("Value must be positive")
    return value


PositiveString = Annotated[UnsignedString, AfterValidator(nonzero)]
def fraction(value):
    if Decimal(value) > 1:
        raise ValueError("Fraction must be <= 1")
    return value


FractionString = Annotated[UnsignedString, AfterValidator(fraction)]


class OneVariant(Model):
    variants: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="after")
    def one_variant(self):
        supplied = self.model_fields_set.intersection(self.variants)
        if len(supplied) != 1:
            raise ValueError("Exactly one documented action variant must be supplied")
        for name in supplied:
            if getattr(self, name) is None and type(self).model_fields[name].annotation is not type(None):
                raise ValueError(f"Variant {name} requires a payload")
        return self


class Limit(Model):
    tif: Literal["Alo", "Ioc", "Gtc"]


class Trigger(Model):
    isMarket: StrictBool
    triggerPx: PositiveString
    tpsl: Literal["tp", "sl"]


class OrderType(OneVariant):
    variants = ("limit", "trigger")
    limit: Limit | None = None
    trigger: Trigger | None = None


class OrderWire(Model):
    a: Asset
    b: StrictBool
    p: PositiveString
    s: PositiveString
    r: StrictBool
    t: OrderType
    c: CloidValue | None = None


class Builder(Model):
    b: Address
    f: UInt


class Orders(Model):
    orders: list[OrderWire] = Field(min_length=1, max_length=100)
    grouping: Literal["na", "normalTpsl", "positionTpsl"] = "na"
    builder: Builder | None = None


class PlaceOrders(Orders):
    type: Literal["order"] = "order"


class CancelItem(Model):
    a: Asset
    o: Annotated[StrictInt, Field(gt=0, lt=2**64)]


class CancelItems(Model):
    cancels: list[CancelItem] = Field(min_length=1, max_length=100)
    f: Literal[True] | None = None  # Native false must be omitted before hashing.


class Cancel(CancelItems):
    type: Literal["cancel"] = "cancel"


class CancelByCloidItem(Model):
    asset: Asset
    cloid: CloidValue


class CancelByCloid(Model):
    type: Literal["cancelByCloid"] = "cancelByCloid"
    cancels: list[CancelByCloidItem] = Field(min_length=1, max_length=100)
    f: Literal[True] | None = None


class ModifyItem(Model):
    oid: Annotated[StrictInt, Field(gt=0, lt=2**64)] | CloidValue
    order: OrderWire


class Modify(ModifyItem):
    type: Literal["modify"] = "modify"
    a: Literal[True] | None = None


class BatchModify(Model):
    type: Literal["batchModify"] = "batchModify"
    modifies: list[ModifyItem] = Field(min_length=1, max_length=100)
    a: Literal[True] | None = None


class Twap(Model):
    a: Asset
    b: StrictBool
    s: PositiveString
    r: StrictBool
    m: Annotated[StrictInt, Field(ge=5, le=1440)]
    t: StrictBool


class Retracement(OneVariant):
    variants = ("pct", "px")
    pct: Annotated[str, Field(pattern=r"^(?:0|[1-9]\d*)(?:\.\d+)?%$")] | None = None
    px: PositiveString | None = None

    @model_validator(mode="after")
    def positive_pct(self):
        if self.pct is not None and Decimal(self.pct[:-1]) <= 0:
            raise ValueError("Retracement percentage must be positive")
        return self


class RegisterAssetRequest(Model):
    coin: Coin
    szDecimals: Annotated[StrictInt, Field(ge=0, le=6)]
    oraclePx: PositiveString
    marginTableId: Annotated[StrictInt, Field(gt=0)]
    onlyIsolated: StrictBool


class RegisterAssetRequest2(Model):
    coin: Coin
    szDecimals: Annotated[StrictInt, Field(ge=0, le=6)]
    oraclePx: PositiveString
    marginTableId: Annotated[StrictInt, Field(gt=0)]
    marginMode: Literal["strictIsolated", "noCross", "normal"]


class DexSchema(Model):
    fullName: str
    collateralToken: Asset
    oracleUpdater: Address | None = None
    isStar: StrictBool | None = None


class RegisterAsset(Model):
    maxGas: UInt | None = None
    assetRequest: RegisterAssetRequest
    dex: Venue
    schema_: DexSchema | None = Field(default=None, alias="schema")


class RegisterAsset2(RegisterAsset):
    assetRequest: RegisterAssetRequest2


class Oracle(Model):
    dex: Venue
    oraclePxs: list[tuple[Coin, PositiveString]]
    markPxs: list[list[tuple[Coin, PositiveString]]]
    externalPerpPxs: list[tuple[Coin, PositiveString]]


class MarginTier(Model):
    lowerBound: UInt
    maxLeverage: Annotated[StrictInt, Field(ge=1, le=50)]


class MarginTable(Model):
    description: str
    marginTiers: list[MarginTier] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def tiers(self):
        bounds = [tier.lowerBound for tier in self.marginTiers]
        leverages = [tier.maxLeverage for tier in self.marginTiers]
        if bounds != sorted(set(bounds)) or leverages != sorted(leverages, reverse=True):
            raise ValueError("Margin tiers require increasing bounds and decreasing leverage")
        return self


class InsertMarginTable(Model):
    dex: Venue
    marginTable: MarginTable


class Halt(Model):
    coin: Coin
    isHalted: StrictBool


class FeeRecipient(Model):
    dex: Venue
    feeRecipient: Address


class SubDeployer(Model):
    variant: str = Field(min_length=1, max_length=100)
    user: Address
    allowed: StrictBool


class SubDeployers(Model):
    dex: Venue
    subDeployers: list[SubDeployer]


class DeployerFee(Model):
    scale: UnsignedString
    growthMode: StrictBool

    @model_validator(mode="after")
    def fee(self):
        value = Decimal(self.scale)
        if value > 3 and not self.growthMode or value >= 10:
            raise ValueError("Fee scale exceeds its mode limit")
        return self


class PerpAnnotation(Model):
    coin: Coin
    category: str = Field(max_length=15)
    description: str = Field(max_length=400)
    displayName: Annotated[str, Field(max_length=9)] | None
    keywords: list[Annotated[str, Field(max_length=10)]] = Field(max_length=2)


class StarSend(Model):
    destination: Address
    amount: PositiveString


class CancelAll(Model):
    assets: Annotated[list[Asset], Field(min_length=1, max_length=10)] | None = None


class StarProxy(OneVariant):
    variants = ("modifyApproval", "modifyBackstopLiquidatorApproval", "setReduceOnly", "cancel", "cancelAll", "order", "sendAsset")
    modifyApproval: StrictBool | None = None
    modifyBackstopLiquidatorApproval: StrictBool | None = None
    setReduceOnly: StrictBool | None = None
    cancel: CancelItems | None = None
    cancelAll: CancelAll | None = None
    order: Orders | None = None
    sendAsset: StarSend | None = None

    @model_validator(mode="after")
    def proxy_reduce_only(self):
        if self.order and any(not order.r for order in self.order.orders):
            raise ValueError("HIP-3 star proxy orders must be reduce-only")
        return self


class StarOracle(Model):
    oraclePxs: list[tuple[Coin, PositiveString]]


class StarOperation(OneVariant):
    variants = ("proxy", "setOracle")
    proxy: tuple[Address, StarProxy] | None = None
    setOracle: StarOracle | None = None


class Star(Model):
    dex: Venue
    operation: StarOperation


class PerpDeploy(OneVariant):
    type: Literal["perpDeploy"] = "perpDeploy"
    variants = ("registerAsset", "registerAsset2", "setOracle", "setFundingMultipliers", "setFundingInterestRates", "setFundingClamps",
                "haltTrading", "insertMarginTable", "setMarginTableIds", "setFeeRecipient", "setOpenInterestCaps", "setSubDeployers",
                "setMarginModes", "setDeployerFees", "setPerpAnnotation", "disableDex", "star")
    registerAsset: RegisterAsset | None = None
    registerAsset2: RegisterAsset2 | None = None
    setOracle: Oracle | None = None
    setFundingMultipliers: list[tuple[Coin, UnsignedString]] | None = None
    setFundingInterestRates: list[tuple[Coin, DecimalString]] | None = None
    setFundingClamps: list[tuple[Coin, UnsignedString]] | None = None
    haltTrading: Halt | None = None
    insertMarginTable: InsertMarginTable | None = None
    setMarginTableIds: list[tuple[Coin, Annotated[StrictInt, Field(gt=0)]]] | None = None
    setFeeRecipient: FeeRecipient | None = None
    setOpenInterestCaps: list[tuple[Coin, UInt | None]] | None = None
    setSubDeployers: SubDeployers | None = None
    setMarginModes: list[tuple[Coin, Literal["strictIsolated", "noCross", "normal"]]] | None = None
    setDeployerFees: list[tuple[str, DeployerFee]] | None = None
    setPerpAnnotation: PerpAnnotation | None = None
    disableDex: Venue | None = None
    star: Star | None = None


class OutcomeAmount(Model):
    outcome: UInt
    amount: PositiveString


class OutcomeMerge(Model):
    outcome: UInt
    amount: PositiveString | None


class QuestionMerge(Model):
    question: UInt
    amount: PositiveString | None


class OutcomeNegate(OutcomeAmount):
    question: UInt


class UserOutcome(OneVariant):
    type: Literal["userOutcome"] = "userOutcome"
    variants = ("splitOutcome", "mergeOutcome", "mergeQuestion", "negateOutcome")
    splitOutcome: OutcomeAmount | None = None
    mergeOutcome: OutcomeMerge | None = None
    mergeQuestion: QuestionMerge | None = None
    negateOutcome: OutcomeNegate | None = None


class Template(Model):
    id: str = Field(min_length=1, max_length=100)
    keywordToValue: list[tuple[str, Annotated[str, Field(max_length=100, pattern=r"^[^{}|]*$")]]]


class FeeTemplate(Template):
    deployerFeeScale: UnsignedString

    @model_validator(mode="after")
    def fee_scale(self):
        if Decimal(self.deployerFeeScale) > 10:
            raise ValueError("Outcome deployer fee scale must be in [0,10]")
        return self


class QuestionTemplate(Model):
    questionTemplateInstance: FeeTemplate
    namedOutcomeTemplateInstances: list[Template] = Field(min_length=1, max_length=100)


class AssociateOutcome(Model):
    question: UInt
    namedOutcomeTemplateInstance: Template


class OutcomeSettlement(Model):
    outcome: UInt
    settleFraction: FractionString
    details: Literal[""]
    nameAndDescription: tuple[str, str]
    sideNames: tuple[str, str]


class QuestionSettlement(Model):
    question: UInt
    outcomeSettlements: list[OutcomeSettlement] = Field(min_length=1, max_length=100)
    nameAndDescription: tuple[str, str]

    @model_validator(mode="after")
    def winner(self):
        fractions = [Decimal(row.settleFraction) for row in self.outcomeSettlements]
        if any(value not in (0, 1) for value in fractions) or fractions.count(Decimal(1)) != 1:
            raise ValueError("Question settlement requires exactly one winner")
        if len({row.outcome for row in self.outcomeSettlements}) != len(fractions):
            raise ValueError("Duplicate outcome in question settlement")
        return self


class OutcomeOperation(OneVariant):
    variants = ("registerStandaloneOutcomeFromTemplate", "registerQuestionFromTemplate", "registerAndAssociateNamedOutcomeFromTemplate",
                "settleOutcome", "settleQuestion2", "setSubDeployers")
    registerStandaloneOutcomeFromTemplate: FeeTemplate | None = None
    registerQuestionFromTemplate: QuestionTemplate | None = None
    registerAndAssociateNamedOutcomeFromTemplate: AssociateOutcome | None = None
    settleOutcome: OutcomeSettlement | None = None
    settleQuestion2: QuestionSettlement | None = None
    setSubDeployers: list[SubDeployer] | None = None


class OutcomeDeploy(Model):
    type: Literal["outcomeDeploy"] = "outcomeDeploy"
    venue: Venue
    operation: OutcomeOperation


class Activate(Model):
    venueName: Venue


class ActivateOutcomeDeployer(OneVariant):
    type: Literal["activateOutcomeDeployer"] = "activateOutcomeDeployer"
    variants = ("activate", "deactivate")
    activate: Activate | None = None
    deactivate: None = None


NATIVE_MODELS = {name: model for name, model in (("order", PlaceOrders), ("cancel", Cancel), ("cancelByCloid", CancelByCloid),
                                                ("modify", Modify), ("batchModify", BatchModify), ("perpDeploy", PerpDeploy),
                                                ("userOutcome", UserOutcome), ("outcomeDeploy", OutcomeDeploy), ("activateOutcomeDeployer", ActivateOutcomeDeployer))}


def native(name, **fields):
    model = create_model("Native" + name[:1].upper() + name[1:], __base__=Model, type=(Literal[name], name), **fields)
    NATIVE_MODELS[name] = model
    return model


native("twapOrder", twap=(Twap, ...))
native("twapCancel", a=(Asset, ...), t=(UInt, ...))
native("trailingStop", asset=(Asset, ...), isBuy=(StrictBool, ...), sz=(PositiveString, ...), reduceOnly=(StrictBool, ...),
       retracement=(Retracement, ...), activationPx=(PositiveString | None, ...))
native("reserveRequestWeight", weight=(Annotated[StrictInt, Field(gt=0)], ...), destination=(Address | None, ...))
native("cDeposit", wei=(Annotated[StrictInt, Field(gt=0, lt=2**64)], ...))
native("cWithdraw", wei=(Annotated[StrictInt, Field(gt=0, lt=2**64)], ...))
native("approveAgent", agentAddress=(Address, ...), agentName=(str, ""))
native("agentSendAsset", destination=(Address, ...), sourceDex=(str, ...), destinationDex=(str, ...), token=(str, ...),
       amount=(PositiveString, ...), fromSubAccount=(Address | Literal[""], ""))
native("sendToEvmWithData", token=(str, ...), amount=(PositiveString, ...), sourceDex=(str, ...), destinationRecipient=(str, ...),
       addressEncoding=(Literal["hex", "base58"], ...), destinationChainId=(Annotated[StrictInt, Field(ge=0, lt=2**32)], ...),
       gasLimit=(UInt, ...), data=(HexBytes, ...))
native("hip3LiquidatorTransfer", dex=(Venue, ...), ntl=(Annotated[StrictInt, Field(gt=0, multiple_of=1_000_000_000)], ...), isDeposit=(StrictBool, ...))
native("validatorL1Stream", riskFreeRate=(DecimalString, ...))
native("authorizeAqav2Role", token=(Asset, ...), role=(Literal["technical", "treasury"], ...))
native("claimRewards")

NativeRequest = Annotated[reduce(or_, NATIVE_MODELS.values()), Field(discriminator="type")]
NATIVE_ADAPTER = TypeAdapter(NativeRequest)

USER_SIGNED_TYPES = {
    "cDeposit": ("HyperliquidTransaction:CDeposit", [("hyperliquidChain", "string"), ("wei", "uint64"), ("nonce", "uint64")]),
    "cWithdraw": ("HyperliquidTransaction:CWithdraw", [("hyperliquidChain", "string"), ("wei", "uint64"), ("nonce", "uint64")]),
    "approveAgent": ("HyperliquidTransaction:ApproveAgent", [("hyperliquidChain", "string"), ("agentAddress", "address"), ("agentName", "string"), ("nonce", "uint64")]),
    "sendToEvmWithData": ("HyperliquidTransaction:SendToEvmWithData", [("hyperliquidChain", "string"), ("token", "string"), ("amount", "string"),
                           ("sourceDex", "string"), ("destinationRecipient", "string"), ("addressEncoding", "string"),
                           ("destinationChainId", "uint32"), ("gasLimit", "uint64"), ("data", "bytes"), ("nonce", "uint64")]),
}


def native_payload(request):
    model = NATIVE_ADAPTER.validate_python(request)
    values = model.model_dump(mode="json", by_alias=True, exclude_unset=True)
    values = {"type": model.type, **{key: value for key, value in values.items() if key != "type"}}
    # Required native defaults such as grouping are not part of model_fields_set.
    if model.type == "order":
        values.setdefault("grouping", model.grouping)
    if model.type == "approveAgent":
        values.setdefault("agentName", model.agentName)
    if model.type == "agentSendAsset":
        values.setdefault("fromSubAccount", model.fromSubAccount)
    if model.type == "perpDeploy":
        for key in ("setFundingMultipliers", "setFundingInterestRates", "setFundingClamps", "setMarginTableIds", "setOpenInterestCaps", "setMarginModes", "setDeployerFees"):
            if key in values:
                values[key] = sorted(values[key], key=lambda pair: pair[0])
        for key in ("registerAsset", "registerAsset2"):
            if (values.get(key, {}).get("schema") or {}).get("isStar"):
                values["testnet_only"] = True
        if "star" in values:
            values["testnet_only"] = True
        if "setOracle" in values:
            oracle = values["setOracle"]
            for key in ("oraclePxs", "externalPerpPxs"):
                oracle[key] = sorted(oracle[key], key=lambda pair: pair[0])
            oracle["markPxs"] = [sorted(rows, key=lambda pair: pair[0]) for rows in oracle["markPxs"]]
        for key, minimum, maximum in (("setFundingMultipliers", 0, 10), ("setFundingInterestRates", Decimal("-0.01"), Decimal("0.01")), ("setFundingClamps", 0, Decimal("0.01"))):
            if key in values and any(not minimum <= Decimal(row[1]) <= maximum for row in values[key]):
                raise AqlaError(f"{key} is outside its documented range")
    if model.type in ("activateOutcomeDeployer", "outcomeDeploy"):
        values["testnet_only"] = True

    def sorted_pairs(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("keywordToValue", "oraclePxs", "externalPerpPxs"):
                    if len({pair[0] for pair in item}) != len(item):
                        raise AqlaError("Duplicate key in documented tuple-list contract")
                    value[key] = sorted(item, key=lambda pair: pair[0])
                else:
                    sorted_pairs(item)
        elif isinstance(value, list):
            for item in value:
                sorted_pairs(item)

    sorted_pairs(values)
    return values


def sign_native(wallet, action, nonce, expires, settings):
    values = dict(action)
    testnet_only = values.pop("testnet_only", False)
    if testnet_only and settings.network != "testnet":
        raise AqlaError("This documented action is currently testnet-only")
    if values["type"] in USER_SIGNED_TYPES:
        if settings.signer_role != "master":
            raise AqlaError("This user-signed action requires the master signer")
        primary, fields = USER_SIGNED_TYPES[values["type"]]
        values["nonce"] = nonce
        signature = signing.sign_user_signed_action(wallet, values, [{"name": key, "type": kind} for key, kind in fields], primary, settings.network == "mainnet")
        return {"action": values, "nonce": nonce, "signature": signature}
    if values["type"] == "agentSendAsset":
        values["nonce"] = nonce
    signature = signing.sign_l1_action(wallet, values, settings.vault_address, nonce, expires, settings.network == "mainnet")
    return {"action": values, "nonce": nonce, "signature": signature, "expiresAfter": expires, "vaultAddress": settings.vault_address}
