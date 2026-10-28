"""Which recommendations are alternatives for the same usage, and which to buy.

Cost Explorer answers "what Savings Plan should I buy?" and "what RIs should I
buy?" as independent questions over the same on-demand usage. This module puts
every finding on one axis — the usage pool it discounts — so a report can
recommend one option per pool and present the rest as alternatives, instead of
adding up three ways to discount the same EC2 spend.

Pure functions over `analyze.Finding`; nothing here calls AWS.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable

from .api import SP_TYPE_LABELS

if TYPE_CHECKING:
    from .analyze import Finding

# What a selected finding is for. Only `recommended` findings count toward a
# total; an `alternative` covers the same usage as a recommended one and buying
# both double-commits it.
ROLE_RECOMMENDED = "recommended"
ROLE_ALTERNATIVE = "alternative"
ROLE_DO_NOT_BUY = "do-not-buy"


# ---------------------------------------------------------------------------
# Usage pools
#
# Cost Explorer sizes each Savings Plan type and each RI service independently
# against the same on-demand usage. A Compute SP, an EC2 Instance SP and EC2
# RIs are three ways to discount ONE pool of EC2 spend; a Database SP and the
# RDS/ElastiCache/DynamoDB reservations are two ways to discount one pool of
# database spend. Summing across them counts that spend two or three times.
#
# Within a pool, each *option* is one way to buy: a Savings Plan type on its
# own, or the set of reservations for that pool (RDS and ElastiCache
# reservations cover different usage, so they add up within the option).
# The pool's contribution to a total is its best option, never the sum.
#
# Database Savings Plans cover Aurora, RDS, DynamoDB and ElastiCache among
# the services here — not MemoryDB, Redshift or OpenSearch — so those keep
# their own pools.
# ---------------------------------------------------------------------------

POOL_LABELS = {
    "compute": "EC2 / Fargate / Lambda compute",
    "database": "Database instances (RDS, Aurora, ElastiCache, DynamoDB)",
    "sagemaker": "SageMaker",
    "redshift": "Redshift",
    "opensearch": "OpenSearch",
    "memorydb": "MemoryDB",
}

SP_POOLS = {
    "COMPUTE_SP": "compute",
    "EC2_INSTANCE_SP": "compute",
    "SAGEMAKER_SP": "sagemaker",
    "DATABASE_SP": "database",
}

RI_POOLS = {
    "Amazon Elastic Compute Cloud - Compute": "compute",
    "Amazon Relational Database Service": "database",
    "Amazon ElastiCache": "database",
    "Amazon DynamoDB Service": "database",
    "Amazon Redshift": "redshift",
    "Amazon OpenSearch Service": "opensearch",
    "Amazon Elasticsearch Service": "opensearch",
    "Amazon MemoryDB Service": "memorydb",
}

# The legacy Elasticsearch service name and OpenSearch can both describe the
# same domains, so their reservations are alternatives rather than a set.
LEGACY_ES_SERVICE = "Amazon Elasticsearch Service"


def ri_option(service: str) -> str:
    pool = RI_POOLS.get(service, service)
    if service == LEGACY_ES_SERVICE:
        return "elasticsearch-legacy-reservations"
    return f"{pool}-reservations"


# Cost Optimization Hub's resource types placed on the same (pool, option) axis
# as the Cost Explorer findings, so the two can be totalled the same way.
COH_POOL_OPTIONS = {
    "ComputeSavingsPlans": ("compute", SP_TYPE_LABELS["COMPUTE_SP"]),
    "Ec2InstanceSavingsPlans": ("compute", SP_TYPE_LABELS["EC2_INSTANCE_SP"]),
    "SageMakerSavingsPlans": ("sagemaker", SP_TYPE_LABELS["SAGEMAKER_SP"]),
    "Ec2ReservedInstances": ("compute", "compute-reservations"),
    "RdsReservedInstances": ("database", "database-reservations"),
    "ElastiCacheReservedInstances": ("database", "database-reservations"),
    "DynamoDbReservedCapacity": ("database", "database-reservations"),
    "RedshiftReservedInstances": ("redshift", "redshift-reservations"),
    "OpenSearchReservedInstances": ("opensearch", "opensearch-reservations"),
    "MemoryDbReservedInstances": ("memorydb", "memorydb-reservations"),
}


# ---------------------------------------------------------------------------
# Selection: one permutation per family, then one option per usage pool
# ---------------------------------------------------------------------------

_TERM_RANK = {"ONE_YEAR": 0, "THREE_YEARS": 1}
_PAYMENT_RANK = {"NO_UPFRONT": 0, "PARTIAL_UPFRONT": 1, "ALL_UPFRONT": 2}


def _rank(f: Finding) -> tuple:
    # A purchase that cannot pay back never outranks one that can, whatever
    # savings figure it carries.
    return (
        f.do_not_buy,
        -f.safe_monthly_savings,
        _TERM_RANK.get(f.term, 9),
        _PAYMENT_RANK.get(f.payment, 9),
    )


def select_best_findings(findings: list[Finding]) -> list[Finding]:
    """Pick the strongest permutation per commitment family.

    Every (term, payment) combination is fetched, but a report that lists all of
    them buries the decision. Rank by risk-adjusted savings among purchases
    that pay back, breaking ties toward the shorter term and less upfront cash
    — the lower-risk purchase.
    """
    best: dict[str, Finding] = {}
    for f in findings:
        key = f"{f.family}:{f.label}"
        if key not in best or _rank(f) < _rank(best[key]):
            best[key] = f
    return sorted(best.values(), key=_rank)


def option_totals(findings: list[Finding], value: Callable[[Finding], float]) -> dict[str, dict[str, float]]:
    """{pool: {option: total}} over findings that pay back."""
    totals: dict[str, dict[str, float]] = {}
    for f in findings:
        if f.do_not_buy:
            continue
        options = totals.setdefault(f.pool, {})
        options[f.option] = options.get(f.option, 0.0) + value(f)
    return totals


def _best_option(options: dict[str, float]) -> str | None:
    if not options:
        return None
    return max(sorted(options), key=lambda o: options[o])


def plan_purchases(findings: list[Finding]) -> tuple[list[Finding], list[dict[str, Any]]]:
    """Mark each finding recommended, alternative, or do-not-buy.

    Within a usage pool only the option with the most achievable savings is
    recommended; the others cover the same usage and are alternatives. Returns
    (findings ordered recommended-first by pool, pool summaries).
    """
    totals = option_totals(findings, lambda f: f.safe_monthly_savings)
    chosen = {pool: _best_option(options) for pool, options in totals.items()}

    def role(f: Finding) -> str:
        if f.do_not_buy:
            return ROLE_DO_NOT_BUY
        return ROLE_RECOMMENDED if chosen.get(f.pool) == f.option else ROLE_ALTERNATIVE

    planned = [replace(f, role=role(f)) for f in findings]
    pool_value = {pool: options.get(chosen[pool], 0.0) for pool, options in totals.items()}
    role_rank = {ROLE_RECOMMENDED: 0, ROLE_ALTERNATIVE: 1, ROLE_DO_NOT_BUY: 2}
    planned.sort(
        key=lambda f: (
            role_rank[f.role],
            -pool_value.get(f.pool, 0.0),
            f.pool,
            -f.safe_monthly_savings,
        )
    )
    pools = [
        {
            "pool": pool,
            "label": POOL_LABELS.get(pool, pool),
            "recommended_option": chosen[pool],
            "monthly_savings": round(pool_value[pool], 2),
            "alternatives": {
                option: round(total, 2)
                for option, total in sorted(options.items(), key=lambda kv: -kv[1])
                if option != chosen[pool]
            },
        }
        for pool, options in sorted(totals.items(), key=lambda kv: -pool_value[kv[0]])
    ]
    return planned, pools


def recommended(findings: list[Finding]) -> list[Finding]:
    """The findings a total may add up — one option per usage pool."""
    return [f for f in findings if f.role == ROLE_RECOMMENDED]
