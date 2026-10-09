"""Source performance in net R, never channel claims, account ROE or trade counts per leg."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from aqla_hyperliquid_mcp.models import AqlaError, Model, digest, now_ms

TIERS = ("F", "E", "D", "C", "B", "A", "S")
Tier = Literal["S", "A", "B", "C", "D", "E", "F"]
FACTORS = {"F": "0.25", "E": "0.5", "D": "0.75", "C": "1", "B": "1.25", "A": "1.5", "S": "2"}
THRESHOLDS = {"F": 0, "E": 30, "D": 45, "C": 60, "B": 70, "A": 80, "S": 90}
PROOF = {"F": 0, "E": 5, "D": 10, "C": 15, "B": 30, "A": 50, "S": 75}


class RankingPolicy(Model):
    enabled: bool = False
    window: int = Field(default=50, ge=5, le=1000)
    factors: dict[Tier, Decimal] = Field(default_factory=lambda: {key: Decimal(value) for key, value in FACTORS.items()})
    thresholds: dict[Tier, Decimal] = Field(default_factory=lambda: {key: Decimal(value) for key, value in THRESHOLDS.items()})
    minimum_proof: dict[Tier, int] = Field(default_factory=lambda: dict(PROOF))
    promotion_margin: Decimal = Field(default=Decimal(5), ge=0, le=20)
    promotion_confirmations: int = Field(default=5, ge=1, le=100)
    demotion_margin: Decimal = Field(default=Decimal(5), ge=0, le=20)
    demotion_confirmations: int = Field(default=2, ge=1, le=100)
    demotion_minimum: int = Field(default=5, ge=1, le=100)
    hard_drawdown_r: Decimal = Field(default=Decimal(5), gt=0, le=1000)
    weights: list[Decimal] = Field(default_factory=lambda: [Decimal(".4"), Decimal(".25"), Decimal(".2"), Decimal(".15")], min_length=4, max_length=4)

    @model_validator(mode="after")
    def consistent(self):
        if any(set(mapping) != set(TIERS) for mapping in (self.factors, self.thresholds, self.minimum_proof)):
            raise ValueError("Configure every source tier exactly once")
        if any(not value.is_finite() or value <= 0 for value in self.factors.values()):
            raise ValueError("Rank risk factors must be positive and finite")
        if any(not 0 <= self.thresholds[key] <= 100 or self.minimum_proof[key] < 0 for key in TIERS):
            raise ValueError("Invalid tier score/proof thresholds")
        if [self.thresholds[key] for key in TIERS] != sorted(set(self.thresholds.values())):
            raise ValueError("Tier thresholds must strictly increase")
        if [self.minimum_proof[key] for key in TIERS] != sorted(self.minimum_proof.values()):
            raise ValueError("Proof thresholds must increase with source tier")
        if any(not 0 <= weight <= 1 for weight in self.weights) or sum(self.weights) != 1:
            raise ValueError("Ranking weights must sum to one")
        return self


def source_key(data, chat):
    return data.get("source_aliases", {}).get(chat, chat)


def source_rank(data, chat):
    key = source_key(data, chat)
    row = data.setdefault("source_ranks", {}).get(key)
    if row is None:
        # An alias resolved after migration inherits its selector's original start grant.
        selector = next((name for name, identity in data.get("source_aliases", {}).items() if identity == key and name in data["source_ranks"]), None)
        row = data["source_ranks"].pop(selector) if selector else {"tier": "F", "bootstrap": False, "revision": 1, "evaluated": [], "up": None, "down": 0}
        data["source_ranks"][key] = row
    return row


def snapshot_rank(data, chat):
    policy = RankingPolicy.model_validate(data["ranking_policy"])
    rank = source_rank(data, chat)
    tier = rank["tier"]
    return {"source_id": source_key(data, chat), "tier": tier, "source_revision": rank["revision"],
            "policy_revision": data["ranking_revision"], "network": "mainnet" if TIERS.index(tier) >= 3 else "testnet",
            "factor": str(policy.factors[tier]), "score": rank.get("metrics", {}).get("score"),
            "provisional": len(rank.get("evidence", [])) < (policy.minimum_proof["C"] if rank.get("bootstrap") else policy.demotion_minimum), "bootstrap": rank.get("bootstrap", False)}


def weighted_parameters(parameters, rank):
    result = dict(parameters)
    factor = Decimal(rank["factor"])
    result["risk_percent"] = str(Decimal(str(parameters["risk_percent"])) * factor)
    if Decimal(result["risk_percent"]) > 100:
        raise AqlaError("Rank-weighted RPR exceeds 100%; adjust the profile or ranking factors")
    if parameters.get("fixed_notional_quote") is not None:
        result["fixed_notional_quote"] = str(Decimal(str(parameters["fixed_notional_quote"])) * factor)
    if rank["network"] == "mainnet" and rank.get("tier") in ("B", "A", "S") and factor > 1:
        result["max_notional_quote"] = str(Decimal(str(parameters["max_notional_quote"])) * factor)
    return result


def clip(value):
    return min(Decimal(1), max(Decimal(0), value))


def metrics(results, policy):
    values = [Decimal(row["net_r"]) for row in results[-policy.window:]]
    if not values:
        return {"score": None, "count": 0, "mean_r": None, "profit_factor": None, "drawdown_r": "0", "win_rate": None}
    gains = sum((value for value in values if value > 0), Decimal(0))
    losses = -sum((value for value in values if value < 0), Decimal(0))
    factor = gains / losses if losses else None
    pf_part = clip((factor - 1) / Decimal("1.5")) if factor is not None else Decimal(1) if gains else Decimal(0)
    cumulative = peak = drawdown = Decimal(0)
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    mean = sum(values) / len(values)
    wins = sum(value > 0 for value in values)
    components = [clip(mean / Decimal(".5")), pf_part, clip(1 - drawdown / Decimal(5)), Decimal(wins + 1) / (len(values) + 2)]
    score = sum(weight * value for weight, value in zip(policy.weights, components, strict=True)) * 100
    return {"score": str(score), "count": len(values), "mean_r": str(mean), "profit_factor": str(factor) if factor is not None else "infinite" if gains else "0",
            "drawdown_r": str(drawdown), "win_rate": str(Decimal(wins) / len(values) * 100), "net_r_sum": str(sum(values))}


FamilyKey = tuple[str, str]


@dataclass
class PendingLegs:
    total: int = 0
    families: dict[FamilyKey | None, int] = field(default_factory=dict)

    def add(self, family: FamilyKey | None) -> None:
        self.total += 1
        self.families[family] = self.families.get(family, 0) + 1

    def has_other_family(self, family: FamilyKey) -> bool:
        return self.total > self.families.get(family, 0)


@dataclass
class OutcomeIndex:
    families: dict[FamilyKey, list[dict]] = field(default_factory=dict)
    pending_refs: set[str] = field(default_factory=set)
    pending_legs: dict[tuple[str, frozenset[str]], PendingLegs] = field(default_factory=dict)


def outcome_index(data) -> OutcomeIndex:
    index = OutcomeIndex()
    for trade in data["trades"].values():
        plan = trade["plan"]
        key = None
        if trade.get("fill_events") or Decimal(trade.get("filled_quantity", "0")) != 0:
            family = plan.get("setup_id") or digest([trade["chat_id"], sorted(plan["source_refs"])])[:32]
            key = (plan.get("rank_snapshot", {}).get("source_id") or source_key(data, trade["chat_id"]), family)
            index.families.setdefault(key, []).append(trade)
        if not trade.get("fill_events") and trade["status"] not in ("closed", "cancelled", "rejected"):
            pending_key = (source_key(data, trade["chat_id"]), frozenset(plan["source_refs"]))
            index.pending_legs.setdefault(pending_key, PendingLegs()).add(key)
    for event in data.get("events", {}).values():
        if (source := event.get("source_id")) is not None and any(target.get("status") in ("pending", "planned", "waiting_entry", "opening", "entry_pending", "uncertain")
               for target in event.get("targets", {}).values()):
            index.pending_refs.add(source)
    return index


def outcomes(data):
    from .monthly import report_row
    index = outcome_index(data)
    result, excluded = {}, {}
    for (source, family), trades in index.families.items():
        values, reasons, timestamps = [], [], []
        # An unfilled leg may still open later. Finalize the family only when no
        # original target remains pending, planned or actively waiting for entry.
        refs = frozenset(trades[0]["plan"]["source_refs"])
        pending = index.pending_legs.get((source, refs))
        if refs & index.pending_refs or pending and pending.has_other_family((source, family)):
            reasons.append("Weitere Ausführung dieses Setups noch nicht abgeschlossen")
        for trade in trades:
            if trade["status"] not in ("closed", "cancelled", "rejected"):
                reasons.append("Trade noch offen")
                continue
            row = report_row(data, trade)
            initial = trade.get("initial_sl_risk")
            if not initial:
                # Conservative legacy reconstruction: only a source-backed absolute SL.
                stop = trade.get("backup_finalization", {}).get("stop_loss") or trade["plan"]["stop_loss"]
                reference = Decimal(trade.get("entry_vwap") or 0)
                valid = Decimal(stop) < reference if trade["plan"]["side"] == "long" else Decimal(stop) > reference
                if valid and reference > 0:
                    initial = {"amount": str(Decimal(trade["filled_quantity"]) * abs(reference - Decimal(stop)))}
                elif reference > 0:
                    reasons.append("Systemfehler: bestätigter Einstieg außerhalb der ursprünglichen SL-Struktur")
            incident = (trade.get("external_conflict") or trade.get("manual_closure") or trade.get("execution_incident")
                        or any(order.get("reason") == "operator_close" for order in trade.get("exit_orders", [])))
            if incident or not initial or Decimal(initial["amount"]) <= 0:
                reasons.append("Systemfehler/manueller Eingriff oder anfängliches SL-Risiko nicht belegt")
            elif not row["complete"]:
                reasons.extend(row["issues"])
            else:
                values.append(Decimal(row["net_pnl"]) / Decimal(initial["amount"]))
                timestamps.append(row["closed_at"])
        if reasons or not values:
            excluded.setdefault(source, []).append({"setup_id": family, "reasons": sorted(set(reasons)), "trade_ids": [row["trade_id"] for row in trades]})
        else:
            result.setdefault(source, []).append({"setup_id": family, "net_r": str(sum(values) / len(values)), "closed_at": max(timestamps),
                                                  "trade_ids": [row["trade_id"] for row in trades]})
    for rows in result.values():
        rows.sort(key=lambda row: (row["closed_at"], row["setup_id"]))
    return result, excluded


def evaluate(data):
    policy = RankingPolicy.model_validate(data["ranking_policy"])
    if not policy.enabled:
        return []
    complete, exclusions = outcomes(data)
    changes = []
    for source in set(data["source_ranks"]) | set(complete):
        rank = source_rank(data, source)
        results = complete.get(source, [])
        current_metrics = metrics(results, policy)
        rank.update(metrics=current_metrics, exclusions=exclusions.get(source, []), evidence=results)
        if rank.get("up"):
            following = rank["up"]["tier"]
            if (current_metrics["score"] is None or len(results) < policy.minimum_proof[following]
                    or Decimal(current_metrics["score"]) < policy.thresholds[following] + policy.promotion_margin
                    or TIERS.index(following) >= 3 and Decimal(current_metrics["drawdown_r"]) >= policy.hard_drawdown_r):
                rank["up"] = None
        old = rank["tier"]
        reason = None
        unseen = [row for row in results if row["setup_id"] not in rank["evaluated"]]
        for evidence in unseen:
            # Late funding may complete an old setup after newer setups were
            # already graded. Assess it with all previously evaluated valid
            # evidence, not a historical prefix that omits today's performance.
            considered = {*rank["evaluated"], evidence["setup_id"]}
            prefix = [row for row in results if row["setup_id"] in considered]
            observation = metrics(prefix, policy)
            score, drawdown = Decimal(observation["score"]), Decimal(observation["drawdown_r"])
            index = TIERS.index(rank["tier"])
            if drawdown >= policy.hard_drawdown_r and index >= 3:
                rank.update(tier="D", up=None, down=0)
                reason = "Drawdown-Grenze erreicht; Mainnet-Bewährung zurückgesetzt"
            elif index and len(prefix) >= policy.demotion_minimum and score <= policy.thresholds[rank["tier"]] - policy.demotion_margin:
                rank["down"] += 1
                rank["up"] = None
                if rank["down"] >= policy.demotion_confirmations:
                    rank.update(tier=TIERS[index - 1], down=0)
                    reason = "Score unter Abstiegsgrenze in bestätigten neuen Abschlüssen"
            else:
                rank["down"] = 0
                if index < 6:
                    next_tier = TIERS[index + 1]
                    eligible = (len(prefix) >= policy.minimum_proof[next_tier] and score >= policy.thresholds[next_tier] + policy.promotion_margin
                                and (TIERS.index(next_tier) < 3 or drawdown < policy.hard_drawdown_r))
                    pending = rank.get("up")
                    if eligible:
                        if pending and pending["tier"] == next_tier:
                            pending["confirmations"] += 1
                            if pending["confirmations"] >= policy.promotion_confirmations:
                                rank.update(tier=next_tier, up=None)
                                reason = "Mindestnachweise und zusätzliche Aufstiegsbestätigungen erfüllt"
                                following_index = TIERS.index(next_tier) + 1
                                if following_index < len(TIERS):
                                    following = TIERS[following_index]
                                    if (len(prefix) >= policy.minimum_proof[following] and score >= policy.thresholds[following] + policy.promotion_margin
                                            and (following_index < 3 or drawdown < policy.hard_drawdown_r)):
                                        rank["up"] = {"tier": following, "confirmations": 0}
                        else:
                            rank["up"] = {"tier": next_tier, "confirmations": 0}
                    else:
                        rank["up"] = None
            rank["evaluated"].append(evidence["setup_id"])
        # Corrections may demote immediately, but cannot create extra promotion confirmations.
        if Decimal(current_metrics["drawdown_r"]) >= policy.hard_drawdown_r and TIERS.index(rank["tier"]) >= 3:
            rank.update(tier="D", up=None, down=0)
            reason = "Drawdown-Grenze durch ergänzte Belege erreicht"
        if rank["tier"] != old:
            if "manual_change" in rank:
                rank["origin"] = "auto"
            rank["revision"] += 1
            event = {"source_id": source, "old": old, "new": rank["tier"], "metrics": current_metrics,
                     "revision": rank["revision"], "time": now_ms(), "network": "mainnet" if TIERS.index(rank["tier"]) >= 3 else "testnet",
                     "reason": reason, "proof_total": len(results), "factor": str(policy.factors[rank["tier"]])}
            if "manual_change" in rank:
                event["origin"] = "auto"
            identity = digest([source, event["revision"]])[:32]
            data["ranking_history"][identity] = event
            data["notifications"].setdefault("rank:" + identity, {"rank_change": event})
            changes.append(event)
    return changes


def status_view(data):
    policy = RankingPolicy.model_validate(data["ranking_policy"])
    return {source: {key: value for key, value in row.items() if key not in ("evaluated", "evidence", "exclusions")}
            | {"proof_total": len(row.get("evidence", [])), "excluded_count": len(row.get("exclusions", [])),
               "evidence": row.get("evidence", [])[-policy.window:], "exclusions": row.get("exclusions", [])[-20:]}
            for source, row in data["source_ranks"].items()}


def set_manual_rank(data, selector, tier, revision, operator):
    from .sources import resolve_source
    if not selector or tier not in TIERS or type(revision) is not int or not operator:
        raise AqlaError("Supply a source, valid tier and current source rank revision")
    source = source_key(data, resolve_source(data, selector))
    known = {*data.get("sources", []), *data.get("source_aliases", {}).values(), *data.get("source_ranks", {})}
    if source not in known:
        raise AqlaError("Select an existing source by name or Telegram ID")
    rank = source_rank(data, source)
    previous = rank.get("manual_change", {})
    if (rank["tier"] == tier and rank.get("origin") == "manual" and previous.get("operator") == operator
            and revision in (rank["revision"], previous.get("expected_revision"))):
        return {"source_id": source, "rank": rank, "changed": False}
    if revision != rank["revision"]:
        raise AqlaError("Read the current source rank revision before changing its tier")
    policy = RankingPolicy.model_validate(data["ranking_policy"])
    complete, exclusions = outcomes(data)
    evidence = complete.get(source, [])
    current_metrics = metrics(evidence, policy)
    if TIERS.index(tier) >= 3 and TIERS.index(tier) > TIERS.index(rank["tier"]) and Decimal(current_metrics["drawdown_r"]) >= policy.hard_drawdown_r:
        raise AqlaError("Current hard drawdown blocks a manual Mainnet promotion")
    old = rank["tier"]
    event = {"source_id": source, "old": old, "new": tier, "operator": operator, "time": now_ms(),
             "reason": "Manueller Ausgangsrang; normale Bewertung und Drawdown-Schutz bleiben aktiv",
             "origin": "manual", "expected_revision": revision, "revision": revision + 1, "metrics": current_metrics,
             "network": "mainnet" if TIERS.index(tier) >= 3 else "testnet", "proof_total": len(evidence), "factor": str(policy.factors[tier])}
    rank.update(tier=tier, revision=revision + 1, bootstrap=False, origin="manual", up=None, down=0,
                manual_change=event, metrics=current_metrics, evidence=evidence, exclusions=exclusions.get(source, []))
    rank["evaluated"] = list(dict.fromkeys([*rank.get("evaluated", []), *(row["setup_id"] for row in evidence)]))
    identity = digest([source, event["revision"]])[:32]
    data["ranking_history"][identity] = event
    data["notifications"].setdefault("rank:" + identity, {"rank_change": event})
    return {"source_id": source, "rank": rank, "changed": True}
