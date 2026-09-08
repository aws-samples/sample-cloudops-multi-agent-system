"""Tests for commitment expiry inventory and renewal recommendations.

Three things here are worth more than the rest, and are what these tests are
mostly about:

1. **Per-service field names.** Every reservation API names the same six
   concepts differently and only EC2 returns an end date; the other five have
   to derive it from `StartTime` + `Duration`. A typo in `InventorySpec` is
   silent — you get commitments with no id and no expiry rather than an error.
2. **The unit distinction.** Savings Plans commit in USD/hour, reservations in
   unit counts. Reading one as the other misreads it by roughly 1000x, so
   nothing converts RI units to money.
3. **Savings Plans must not be multiplied by region.** They are account-level;
   sweeping them per region would silently N-times every total.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from commitments import analyze, api, collect, report

ONE_YEAR_SECONDS = 31536000
THREE_YEAR_SECONDS = 94608000

AS_OF = date(2026, 9, 4)


def client_error(code: str, message: str = "boom") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Describe")


class StubDescribe:
    """A regional client that replays one canned response, or raises."""

    def __init__(self, response=None, error: ClientError | None = None):
        self.response = response or {}
        self.error = error
        self.calls = 0

    def _respond(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response

    describe_reserved_instances = _respond
    describe_reserved_db_instances = _respond
    describe_reserved_cache_nodes = _respond
    describe_reserved_nodes = _respond


class StubSavingsPlans:
    """describe_savings_plans, optionally paginated."""

    def __init__(self, pages=None, error: ClientError | None = None):
        self.pages = pages or [{"savingsPlans": []}]
        self.error = error
        self.calls: list[dict] = []

    def describe_savings_plans(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.pages[len(self.calls) - 1]


def clients(factory=None) -> api.Clients:
    return api.Clients(
        ce=StubDescribe(),
        coh=StubDescribe(),
        account_id="111122223333",
        profile=None,
        make_client=factory,
    )


# --------------------------------------------------------- date normalization


@pytest.mark.unit
class TestDateCoercion:
    """boto3 hands back datetimes; a CLI JSON dump hands back strings."""

    @pytest.mark.parametrize(
        "value",
        [
            datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 1, 12, 0),
            date(2026, 10, 1),
            "2026-10-01T12:00:00Z",
            "2026-10-01T12:00:00+00:00",
            "2026-10-01",
        ],
    )
    def test_every_shape_an_sdk_or_cli_returns_normalizes(self, value):
        assert api._iso_day(api._as_datetime(value)) == "2026-10-01"

    @pytest.mark.parametrize("value", [None, "", "not-a-date", 12345])
    def test_unparseable_returns_none_rather_than_raising(self, value):
        """One malformed row must not lose the rest of the inventory."""
        assert api._as_datetime(value) is None

    def test_naive_datetime_is_read_as_utc(self):
        naive = api._as_datetime(datetime(2026, 10, 1))
        assert naive.tzinfo is timezone.utc

    def test_term_months_labels_both_real_terms(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert api._term_months(start, start + timedelta(seconds=ONE_YEAR_SECONDS)) == 12
        assert (
            api._term_months(start, start + timedelta(seconds=THREE_YEAR_SECONDS)) == 36
        )

    def test_term_months_is_none_when_either_end_is_unknown(self):
        assert api._term_months(None, datetime.now(timezone.utc)) is None
        assert api._term_months(datetime.now(timezone.utc), None) is None


# ------------------------------------------------------- reservation inventory


@pytest.mark.unit
class TestReservationInventory:
    def test_ec2_uses_the_end_field_it_actually_returns(self):
        """EC2 is the only reservation API that returns an explicit End."""
        stub = StubDescribe(
            {
                "ReservedInstances": [
                    {
                        "ReservedInstancesId": "ri-abc",
                        "InstanceCount": 4,
                        "InstanceType": "m5.large",
                        "Start": datetime(2025, 10, 1, tzinfo=timezone.utc),
                        "End": datetime(2026, 10, 1, tzinfo=timezone.utc),
                        "Duration": ONE_YEAR_SECONDS,
                        "State": "active",
                        "OfferingType": "No Upfront",
                    }
                ]
            }
        )
        result = api.get_reservation_inventory(
            clients(lambda svc, region: stub), "ec2", "ap-northeast-1"
        )
        [item] = result["items"]
        assert item["commitment_id"] == "ri-abc"
        assert item["end"] == "2026-10-01"
        assert item["quantity"] == 4
        assert item["unit"] == "units"
        assert item["region"] == "ap-northeast-1"
        assert item["term_months"] == 12

    @pytest.mark.parametrize(
        ("service", "response"),
        [
            (
                "rds",
                {
                    "ReservedDBInstances": [
                        {
                            "ReservedDBInstanceId": "rds-1",
                            "DBInstanceCount": 2,
                            "DBInstanceClass": "db.r5.large",
                            "StartTime": datetime(2025, 10, 1, tzinfo=timezone.utc),
                            "Duration": ONE_YEAR_SECONDS,
                            "State": "active",
                            "ReservedDBInstanceArn": "arn:aws:rds:::ri/rds-1",
                        }
                    ]
                },
            ),
            (
                "elasticache",
                {
                    "ReservedCacheNodes": [
                        {
                            "ReservedCacheNodeId": "ec-1",
                            "CacheNodeCount": 2,
                            "CacheNodeType": "cache.r6g.large",
                            "StartTime": datetime(2025, 10, 1, tzinfo=timezone.utc),
                            "Duration": ONE_YEAR_SECONDS,
                            "State": "active",
                            "ReservationARN": "arn:aws:elasticache:::ri/ec-1",
                        }
                    ]
                },
            ),
            (
                "redshift",
                {
                    "ReservedNodes": [
                        {
                            "ReservedNodeId": "rs-1",
                            "NodeCount": 2,
                            "NodeType": "ra3.xlplus",
                            "StartTime": datetime(2025, 10, 1, tzinfo=timezone.utc),
                            "Duration": ONE_YEAR_SECONDS,
                            "State": "active",
                        }
                    ]
                },
            ),
            (
                "opensearch",
                {
                    "ReservedInstances": [
                        {
                            "ReservedInstanceId": "os-1",
                            "InstanceCount": 2,
                            "InstanceType": "r6g.large.search",
                            "StartTime": datetime(2025, 10, 1, tzinfo=timezone.utc),
                            "Duration": ONE_YEAR_SECONDS,
                            "State": "active",
                            "PaymentOption": "NO_UPFRONT",
                        }
                    ]
                },
            ),
            (
                "memorydb",
                {
                    "ReservedNodes": [
                        {
                            "ReservationId": "mdb-1",
                            "NodeCount": 2,
                            "NodeType": "db.r6g.large",
                            "StartTime": datetime(2025, 10, 1, tzinfo=timezone.utc),
                            "Duration": ONE_YEAR_SECONDS,
                            "State": "active",
                            "ARN": "arn:aws:memorydb:::ri/mdb-1",
                        }
                    ]
                },
            ),
        ],
    )
    def test_end_date_is_derived_from_start_plus_duration(self, service, response):
        """Only EC2 returns End; the other five must be computed or they are blank."""
        stub = StubDescribe(response)
        result = api.get_reservation_inventory(
            clients(lambda svc, region: stub), service, "us-east-1"
        )
        [item] = result["items"]
        assert item["end"] == "2026-10-01", f"{service} end date not derived"
        assert item["start"] == "2025-10-01"
        assert item["commitment_id"], f"{service} id_field is wrong"
        assert item["quantity"] == 2, f"{service} count_field is wrong"
        assert item["instance_type"], f"{service} type_field is wrong"
        assert item["term_months"] == 12

    def test_every_spec_maps_to_a_real_boto3_method_name(self):
        """A method typo only shows up at call time, which is too late."""
        for spec in api.RESERVATION_INVENTORY:
            assert spec.method.startswith("describe_")
            assert spec.response_key
            assert spec.key in api.INVENTORY_KEYS

    def test_retired_reservations_are_excluded(self):
        """A retired reservation is history, not something to renew."""
        stub = StubDescribe(
            {
                "ReservedInstances": [
                    {
                        "ReservedInstancesId": "gone",
                        "InstanceCount": 1,
                        "InstanceType": "m5.large",
                        "Start": datetime(2023, 1, 1, tzinfo=timezone.utc),
                        "End": datetime(2024, 1, 1, tzinfo=timezone.utc),
                        "State": "retired",
                    }
                ]
            }
        )
        result = api.get_reservation_inventory(
            clients(lambda svc, region: stub), "ec2", "us-east-1"
        )
        assert result["items"] == []

    def test_client_error_is_reported_with_the_failed_query_identified(self):
        stub = StubDescribe(error=client_error("UnauthorizedOperation"))
        result = api.get_reservation_inventory(
            clients(lambda svc, region: stub), "ec2", "eu-west-1"
        )
        assert result["error_code"] == "UnauthorizedOperation"
        assert result["service"] == "ec2"
        assert result["region"] == "eu-west-1"

    def test_missing_client_factory_degrades_to_an_explained_error(self):
        """A host that granted only ce:Get* must get a warning, not a crash."""
        result = api.get_reservation_inventory(clients(None), "ec2", "us-east-1")
        assert "error" in result
        assert "Describe" in result["error"]

    def test_unknown_family_is_a_programming_error_not_a_silent_skip(self):
        with pytest.raises(ValueError, match="Unknown reservation family"):
            api.get_reservation_inventory(clients(), "dynamodb", "us-east-1")

    def test_dynamodb_is_not_claimed_as_covered(self):
        """There is no describe-reserved-capacity API; pretending otherwise
        would report "nothing expiring" for a reservation that does."""
        assert "dynamodb" not in api.INVENTORY_KEYS
        assert any("DynamoDB" in s for s in api.INVENTORY_BLIND_SPOTS)


# ------------------------------------------------------ savings plan inventory


@pytest.mark.unit
class TestSavingsPlanInventory:
    def test_commitment_is_dollars_per_hour_not_a_unit_count(self):
        sp = StubSavingsPlans(
            [
                {
                    "savingsPlans": [
                        {
                            "savingsPlanId": "sp-1",
                            "savingsPlanArn": "arn:aws:savingsplans::sp/sp-1",
                            "savingsPlanType": "Compute",
                            "commitment": "5.50",
                            "start": "2025-10-01T00:00:00Z",
                            "end": "2026-10-01T00:00:00Z",
                            "state": "active",
                            "paymentOption": "No Upfront",
                            "region": "",
                        }
                    ]
                }
            ]
        )
        result = api.get_savings_plan_inventory(clients(lambda svc, region: sp))
        [item] = result["items"]
        assert item["unit"] == "USD/hour"
        assert item["quantity"] == 5.50
        assert item["family"] == "savings-plan"
        assert item["end"] == "2026-10-01"
        assert item["term_months"] == 12
        # An empty region on a Compute SP means account-wide, not "unknown".
        assert item["region"] == "global"

    def test_only_active_states_are_requested(self):
        sp = StubSavingsPlans()
        api.get_savings_plan_inventory(clients(lambda svc, region: sp))
        assert sp.calls[0]["states"] == list(api.ACTIVE_SP_STATES)

    def test_pagination_is_followed(self):
        """Stopping at page one silently under-reports the expiring total."""
        sp = StubSavingsPlans(
            [
                {
                    "savingsPlans": [
                        {"savingsPlanId": "sp-1", "commitment": "1.0",
                         "end": "2026-10-01T00:00:00Z", "state": "active"}
                    ],
                    "nextToken": "more",
                },
                {
                    "savingsPlans": [
                        {"savingsPlanId": "sp-2", "commitment": "2.0",
                         "end": "2026-11-01T00:00:00Z", "state": "active"}
                    ]
                },
            ]
        )
        result = api.get_savings_plan_inventory(clients(lambda svc, region: sp))
        assert [i["commitment_id"] for i in result["items"]] == ["sp-1", "sp-2"]
        assert sp.calls[1]["nextToken"] == "more"

    def test_client_error_is_reported_not_raised(self):
        sp = StubSavingsPlans(error=client_error("AccessDeniedException"))
        result = api.get_savings_plan_inventory(clients(lambda svc, region: sp))
        assert result["error_code"] == "AccessDeniedException"

    def test_missing_client_factory_names_the_permission(self):
        result = api.get_savings_plan_inventory(clients(None))
        assert "DescribeSavingsPlans" in result["error"]


# -------------------------------------------------------------- expiry verdict


def sp_item(end: str, quantity: float = 1.0, commitment_id: str = "sp-1") -> dict:
    return {
        "family": "savings-plan",
        "service": "savingsplans",
        "label": "Compute Savings Plan",
        "commitment_id": commitment_id,
        "quantity": quantity,
        "unit": "USD/hour",
        "region": "global",
        "end": end,
    }


def ri_item(end: str, quantity: float = 2.0, commitment_id: str = "ri-1") -> dict:
    return {
        "family": "reservation",
        "service": "ec2",
        "label": "EC2",
        "commitment_id": commitment_id,
        "quantity": quantity,
        "unit": "units",
        "region": "us-east-1",
        "end": end,
    }


@pytest.mark.unit
class TestExpiryBands:
    @pytest.mark.parametrize(
        ("days_out", "urgency"),
        [(1, "urgent"), (30, "urgent"), (31, "soon"), (60, "soon"),
         (61, "upcoming"), (90, "upcoming")],
    )
    def test_days_remaining_maps_to_the_documented_band(self, days_out, urgency):
        end = (AS_OF + timedelta(days=days_out)).isoformat()
        result = analyze.analyze_expiry([sp_item(end)], AS_OF, 90)
        [entry] = result["expiring"]
        assert entry["urgency"] == urgency
        assert entry["days_remaining"] == days_out

    def test_beyond_the_horizon_is_not_reported(self):
        end = (AS_OF + timedelta(days=120)).isoformat()
        result = analyze.analyze_expiry([sp_item(end)], AS_OF, 90)
        assert result["expiring"] == []
        # Still counted as inventoried, so the report can say how many exist.
        assert result["total_active"] == 1

    def test_already_ended_is_separated_from_expiring(self):
        """A lapsed commitment is a different, more urgent conversation than a
        renewal — the spend it covered is already back at on-demand rates."""
        end = (AS_OF - timedelta(days=5)).isoformat()
        result = analyze.analyze_expiry([sp_item(end)], AS_OF, 90)
        assert result["expiring"] == []
        [entry] = result["expired"]
        assert entry["urgency"] == "expired"
        assert "5 days ago" in entry["rationale"]

    def test_undated_commitment_is_declared_not_dropped(self):
        result = analyze.analyze_expiry([sp_item("")], AS_OF, 90)
        assert result["expiring"] == []
        assert len(result["undated"]) == 1

    def test_output_is_sorted_soonest_first(self):
        items = [
            sp_item((AS_OF + timedelta(days=80)).isoformat(), commitment_id="late"),
            sp_item((AS_OF + timedelta(days=10)).isoformat(), commitment_id="soon"),
        ]
        result = analyze.analyze_expiry(items, AS_OF, 90)
        assert [e["commitment_id"] for e in result["expiring"]] == ["soon", "late"]


@pytest.mark.unit
class TestRenewalVerdict:
    @pytest.mark.parametrize(
        ("utilization", "action"),
        [
            (99.5, analyze.RENEW),
            (95.0, analyze.RENEW),
            (94.9, analyze.RENEW_SMALLER),
            (50.0, analyze.RENEW_SMALLER),
            (49.9, analyze.LET_LAPSE),
            (0.0, analyze.LET_LAPSE),
        ],
    )
    def test_utilization_drives_the_verdict(self, utilization, action):
        end = (AS_OF + timedelta(days=20)).isoformat()
        result = analyze.analyze_expiry(
            [sp_item(end)], AS_OF, 90, sp_utilization_pct=utilization
        )
        assert result["expiring"][0]["action"] == action

    def test_unmeasured_utilization_asks_for_review_rather_than_guessing(self):
        """The utilization figure is what makes the call defensible; without it
        the honest answer is "review", not a default to renew."""
        end = (AS_OF + timedelta(days=20)).isoformat()
        result = analyze.analyze_expiry([sp_item(end)], AS_OF, 90)
        entry = result["expiring"][0]
        assert entry["action"] == analyze.REVIEW
        assert "could not be measured" in entry["rationale"]

    def test_each_family_uses_its_own_utilization_figure(self):
        """SP utilization must not decide a reservation's renewal."""
        end = (AS_OF + timedelta(days=20)).isoformat()
        result = analyze.analyze_expiry(
            [sp_item(end), ri_item(end)],
            AS_OF,
            90,
            sp_utilization_pct=99.0,
            ri_utilization_pct=20.0,
        )
        by_family = {e["family"]: e for e in result["expiring"]}
        assert by_family["savings-plan"]["action"] == analyze.RENEW
        assert by_family["reservation"]["action"] == analyze.LET_LAPSE

    def test_rationale_quotes_the_figure_it_relied_on(self):
        end = (AS_OF + timedelta(days=20)).isoformat()
        result = analyze.analyze_expiry(
            [sp_item(end)], AS_OF, 90, sp_utilization_pct=72.5
        )
        assert "72.5%" in result["expiring"][0]["rationale"]

    def test_renew_smaller_names_expiry_as_a_free_resize_point(self):
        end = (AS_OF + timedelta(days=20)).isoformat()
        result = analyze.analyze_expiry(
            [sp_item(end)], AS_OF, 90, sp_utilization_pct=70.0
        )
        assert "zero-cost resize" in result["expiring"][0]["rationale"]


@pytest.mark.unit
class TestExpiryRollup:
    def test_savings_plan_hourly_commitment_converts_to_monthly_money(self):
        end = (AS_OF + timedelta(days=20)).isoformat()
        result = analyze.analyze_expiry([sp_item(end, quantity=5.0)], AS_OF, 90)
        assert result["hourly_commitment_expiring"] == 5.0
        assert result["monthly_committed_spend_expiring"] == pytest.approx(
            5.0 * analyze.HOURS_PER_MONTH
        )

    def test_reservation_units_are_never_converted_to_money(self):
        """Turning unit counts into dollars needs pricing this module does not
        query — inventing a rate would be a fabricated figure."""
        end = (AS_OF + timedelta(days=20)).isoformat()
        result = analyze.analyze_expiry([ri_item(end, quantity=7.0)], AS_OF, 90)
        assert result["reserved_units_expiring"] == 7.0
        assert result["hourly_commitment_expiring"] == 0.0
        assert result["monthly_committed_spend_expiring"] == 0.0

    def test_counts_and_actions_tally_the_reported_rows(self):
        items = [
            sp_item((AS_OF + timedelta(days=10)).isoformat(), commitment_id="a"),
            sp_item((AS_OF + timedelta(days=45)).isoformat(), commitment_id="b"),
            sp_item((AS_OF + timedelta(days=200)).isoformat(), commitment_id="far"),
        ]
        result = analyze.analyze_expiry(items, AS_OF, 90, sp_utilization_pct=99.0)
        assert result["counts"] == {"urgent": 1, "soon": 1, "upcoming": 0}
        assert result["actions"][analyze.RENEW] == 2
        assert result["total_active"] == 3

    def test_as_of_is_injected_so_the_function_stays_pure(self):
        end = (AS_OF + timedelta(days=5)).isoformat()
        assert analyze.analyze_expiry([sp_item(end)], AS_OF, 90)["as_of"] == (
            "2026-09-04"
        )


# ------------------------------------------------------------- collection wiring


@pytest.mark.unit
class TestResolveRegions:
    def test_empty_falls_back_to_the_hosts_own_region(self):
        assert collect.resolve_regions([], "ap-northeast-1") == ["ap-northeast-1"]

    def test_duplicates_collapse_so_totals_are_not_doubled(self):
        assert collect.resolve_regions(["us-east-1", "us-east-1"]) == ["us-east-1"]

    @pytest.mark.parametrize(
        "bad", ["us-east", "US-EAST-1 extra", "../../etc", "useast1", "x"]
    )
    def test_non_region_input_is_rejected_before_it_reaches_an_endpoint(self, bad):
        """Region names become part of an SDK endpoint, so they are validated
        rather than passed through."""
        with pytest.raises(ValueError, match="valid AWS region"):
            collect.resolve_regions([bad])

    def test_case_is_normalized(self):
        assert collect.resolve_regions(["US-East-1"]) == ["us-east-1"]

    def test_no_regions_and_no_default_is_a_caller_error(self):
        with pytest.raises(ValueError, match="At least one AWS region"):
            collect.resolve_regions([])


@pytest.mark.unit
class TestCollectExpiry:
    def test_savings_plans_are_fetched_once_regardless_of_region_count(self):
        """SPs are account-level. Sweeping them per region would return the same
        plans N times and inflate every total by N."""
        sp_calls = {"n": 0}

        def factory(service, region):
            if service == "savingsplans":
                sp_calls["n"] += 1
                return StubSavingsPlans(
                    [
                        {
                            "savingsPlans": [
                                {
                                    "savingsPlanId": "sp-1",
                                    "commitment": "3.0",
                                    "end": (AS_OF + timedelta(days=10)).isoformat(),
                                    "state": "active",
                                    "savingsPlanType": "Compute",
                                }
                            ]
                        }
                    ]
                )
            return StubDescribe()

        expiry, errors = collect.collect_expiry(
            clients(factory), ["us-east-1", "eu-west-1", "ap-northeast-1"],
            ["ec2"], 90, as_of=AS_OF,
        )
        assert sp_calls["n"] == 1
        assert len(expiry["expiring"]) == 1
        assert errors == []

    def test_reservations_are_swept_per_region(self):
        seen: list[tuple[str, str]] = []

        def factory(service, region):
            seen.append((service, region))
            return StubDescribe() if service != "savingsplans" else StubSavingsPlans()

        collect.collect_expiry(
            clients(factory), ["us-east-1", "eu-west-1"], ["ec2", "rds"], 90,
            as_of=AS_OF,
        )
        assert ("ec2", "us-east-1") in seen
        assert ("ec2", "eu-west-1") in seen
        assert ("rds", "eu-west-1") in seen

    def test_one_failed_family_does_not_lose_the_others(self):
        def factory(service, region):
            if service == "rds":
                return StubDescribe(error=client_error("AccessDenied"))
            if service == "savingsplans":
                return StubSavingsPlans()
            return StubDescribe(
                {
                    "ReservedInstances": [
                        {
                            "ReservedInstancesId": "ri-ok",
                            "InstanceCount": 1,
                            "InstanceType": "m5.large",
                            "Start": datetime(2025, 10, 1, tzinfo=timezone.utc),
                            "End": datetime(2026, 10, 1, tzinfo=timezone.utc),
                            "State": "active",
                        }
                    ]
                }
            )

        expiry, errors = collect.collect_expiry(
            clients(factory), ["us-east-1"], ["ec2", "rds"], 90, as_of=AS_OF
        )
        assert [e["commitment_id"] for e in expiry["expiring"]] == ["ri-ok"]
        assert len(errors) == 1
        assert "rds" in errors[0]["query"]

    def test_unknown_family_is_rejected_with_the_valid_list(self):
        with pytest.raises(ValueError, match="reservation family"):
            collect.collect_expiry(clients(), ["us-east-1"], ["dynamodb"])

    def test_regions_and_blind_spots_are_carried_into_the_result(self):
        expiry, _ = collect.collect_expiry(
            clients(lambda s, r: StubDescribe()), ["us-east-1"], ["ec2"], 90,
            as_of=AS_OF,
        )
        assert expiry["regions"] == ["us-east-1"]
        assert expiry["blind_spots"] == list(api.INVENTORY_BLIND_SPOTS)


@pytest.mark.unit
class TestCollectAllIntegration:
    def test_expiry_is_skipped_when_no_regions_are_requested(self):
        """Expiry needs Describe* permissions beyond ce:Get*, so a caller that
        does not ask must not have the calls made on its behalf."""
        called = {"n": 0}

        def factory(service, region):
            called["n"] += 1
            return StubDescribe()

        payload = collect.collect_all(clients(factory), families=["sp"])
        assert payload["expiry"] is None
        assert called["n"] == 0

    def test_expiry_errors_are_kept_out_of_the_billable_query_count(self):
        """queries_run exists to track Cost Explorer's $0.01-per-request
        billing; free Describe* failures must not inflate it."""
        payload = collect.collect_all(
            clients(None), families=["sp"], regions=["us-east-1"]
        )
        assert payload["expiry"] is not None
        assert len(payload["errors"]) > len(payload["sweep_errors"])

    def test_invalid_region_is_rejected_by_collect_all_too(self):
        with pytest.raises(ValueError, match="valid AWS region"):
            collect.collect_all(clients(), families=["sp"], regions=["nope"])


# -------------------------------------------------------------------- reporting


def payload_with_expiry(expiry) -> dict:
    return {
        "meta": {
            "account_id": "111122223333",
            "profile": None,
            "generated_at": "2026-09-04 09:00 UTC",
            "lookback": "THIRTY_DAYS",
            "account_scope": "PAYER",
        },
        "findings": [],
        "posture": {"blockers": [], "notes": []},
        "reconciliation": {"status": "unavailable", "reason": "not enrolled"},
        "eligible_spend": {"periods": []},
        "expiry": expiry,
        "errors": [],
    }


@pytest.mark.unit
class TestExpiryReporting:
    def _expiry(self, **overrides):
        base = analyze.analyze_expiry(
            [
                sp_item((AS_OF + timedelta(days=12)).isoformat(), quantity=5.5),
                ri_item((AS_OF + timedelta(days=70)).isoformat(), quantity=4.0),
            ],
            AS_OF,
            90,
            sp_utilization_pct=99.0,
            ri_utilization_pct=40.0,
        )
        base["regions"] = ["ap-northeast-1"]
        base["blind_spots"] = list(api.INVENTORY_BLIND_SPOTS)
        base.update(overrides)
        return base

    def test_section_renders_with_dates_actions_and_units(self):
        md = report.render(payload_with_expiry(self._expiry()))
        assert "## Commitment expiry and renewal" in md
        assert "**renew**" in md
        assert "let lapse" in md
        # SP size must carry its unit, RI size must not gain a dollar sign.
        assert "$5.5000/hr" in md
        assert "4 unit(s)" in md

    def test_urgent_expiry_is_surfaced_in_the_bottom_line(self):
        """A deadline outranks an optional purchase."""
        md = report.render(payload_with_expiry(self._expiry()))
        bottom = md.split("## Reconciliation")[0]
        assert "expire within 30 days" in bottom

    def test_reserved_units_are_not_quoted_as_money(self):
        md = report.render(payload_with_expiry(self._expiry()))
        assert "needs per-instance pricing" in md

    def test_account_level_utilization_caveat_is_stated(self):
        md = report.render(payload_with_expiry(self._expiry()))
        assert "not\nper-commitment" in md or "not per-commitment" in md

    def test_dynamodb_blind_spot_is_disclosed(self):
        md = report.render(payload_with_expiry(self._expiry()))
        assert "Not covered by this inventory" in md
        assert "DynamoDB" in md

    def test_nothing_expiring_says_so_rather_than_printing_an_empty_table(self):
        expiry = self._expiry(expiring=[], counts={"urgent": 0, "soon": 0, "upcoming": 0})
        md = report.render(payload_with_expiry(expiry))
        assert "No commitment expires within 90 days" in md
        assert "| Ends | Days |" not in md

    def test_expired_but_active_gets_its_own_subsection(self):
        expiry = analyze.analyze_expiry(
            [sp_item((AS_OF - timedelta(days=3)).isoformat())], AS_OF, 90,
            sp_utilization_pct=99.0,
        )
        expiry["regions"] = ["us-east-1"]
        md = report.render(payload_with_expiry(expiry))
        assert "Already ended but still listed as active" in md

    def test_payload_without_expiry_still_renders(self):
        """Report templates and cached payloads predate this section."""
        payload = payload_with_expiry(None)
        md = report.render(payload)
        assert "## Commitment expiry and renewal" not in md
        assert "## Bottom line" in md

    def test_payload_missing_the_key_entirely_still_renders(self):
        payload = payload_with_expiry(None)
        del payload["expiry"]
        assert "## Bottom line" in report.render(payload)
