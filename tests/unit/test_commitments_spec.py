"""Tests for the purchasable specification carried on commitments.

A commitment recommendation without a specification is not actionable. "Buy 4
RDS reservations" does not say `db.r6g.large · Multi-AZ · Aurora PostgreSQL`,
and a reservation only discounts usage matching its exact specification — so the
things these tests guard are:

1. **Per-service field names.** Cost Explorer buries the spec in a
   service-specific sub-structure and names every field differently. A typo is
   silent: you get a blank spec, not an error. Two services break the pattern
   outright — OpenSearch splits its type across `InstanceClass` + `InstanceSize`,
   DynamoDB has no instance at all and lives under a different container.
2. **`MultiAZ` is a bool.** `False` means "Single-AZ", which is information. Drop
   it as falsy and a reader is left assuming Multi-AZ.
3. **The aggregate is a budget, not an order.** One finding can span several
   distinct specs, so the per-line breakdown has to survive into both the
   markdown report and the JSON envelope.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from commitments import api, collect
from commitments.analyze import (
    SPEC_UNAVAILABLE,
    analyze_expiry,
    analyze_ri_recommendation,
    analyze_sp_recommendation,
)
from commitments.report import render

ONE_YEAR_SECONDS = 31536000


# ------------------------------------------------- recommendation spec shapes


@pytest.mark.unit
class TestDescribeRecommendationSpec:
    def test_ec2_reports_type_az_and_platform(self):
        spec = api.describe_recommendation_spec(
            {
                "InstanceDetails": {
                    "EC2InstanceDetails": {
                        "Family": "m5",
                        "InstanceType": "m5.xlarge",
                        "Region": "ap-northeast-1",
                        "AvailabilityZone": "ap-northeast-1a",
                        "Platform": "Linux/UNIX",
                        "Tenancy": "default",
                        "CurrentGeneration": True,
                        "SizeFlexEligible": False,
                    }
                }
            }
        )
        assert spec["instance_type"] == "m5.xlarge"
        assert spec["family"] == "m5"
        assert spec["region"] == "ap-northeast-1"
        assert spec["attributes"]["AZ"] == "ap-northeast-1a"
        assert spec["label"].startswith("m5.xlarge · ap-northeast-1a")
        assert spec["label"].endswith("ap-northeast-1")

    def test_rds_reports_the_deployment_option_and_engine(self):
        """The two dimensions that decide whether a reservation applies at all."""
        spec = api.describe_recommendation_spec(
            {
                "InstanceDetails": {
                    "RDSInstanceDetails": {
                        "Family": "db.r6g",
                        "InstanceType": "db.r6g.large",
                        "Region": "us-east-1",
                        "DatabaseEngine": "Aurora PostgreSQL",
                        "DatabaseEdition": "",
                        "DeploymentOption": "Multi-AZ",
                        "LicenseModel": "No license required",
                        "CurrentGeneration": True,
                        "SizeFlexEligible": True,
                    }
                }
            }
        )
        assert spec["instance_type"] == "db.r6g.large"
        assert spec["attributes"]["deployment"] == "Multi-AZ"
        assert spec["attributes"]["engine"] == "Aurora PostgreSQL"
        assert "edition" not in spec["attributes"], "blank fields must not pad the spec"
        assert spec["size_flex_eligible"] is True
        # License model is part of what a reservation matches on (BYOL and
        # license-included are priced and sold separately), so it stays.
        assert spec["label"] == (
            "db.r6g.large · Multi-AZ · Aurora PostgreSQL · "
            "No license required · us-east-1"
        )

    def test_single_az_rds_is_stated_rather_than_implied(self):
        spec = api.describe_recommendation_spec(
            {
                "InstanceDetails": {
                    "RDSInstanceDetails": {
                        "InstanceType": "db.t4g.medium",
                        "DeploymentOption": "Single-AZ",
                        "DatabaseEngine": "PostgreSQL",
                    }
                }
            }
        )
        assert "Single-AZ" in spec["label"]

    def test_elasticache_reads_node_type_not_instance_type(self):
        spec = api.describe_recommendation_spec(
            {
                "InstanceDetails": {
                    "ElastiCacheInstanceDetails": {
                        "Family": "cache.r6g",
                        "NodeType": "cache.r6g.large",
                        "Region": "us-west-2",
                        "ProductDescription": "redis",
                    }
                }
            }
        )
        assert spec["instance_type"] == "cache.r6g.large"
        assert spec["attributes"]["engine"] == "redis"

    @pytest.mark.parametrize(
        ("key", "payload"),
        [
            (
                "RedshiftInstanceDetails",
                {"Family": "ra3", "NodeType": "ra3.xlplus", "Region": "us-east-1"},
            ),
            (
                "MemoryDBInstanceDetails",
                {"Family": "db.r6g", "NodeType": "db.r6g.large", "Region": "us-east-1"},
            ),
        ],
    )
    def test_node_based_services_still_yield_a_type(self, key, payload):
        spec = api.describe_recommendation_spec({"InstanceDetails": {key: payload}})
        assert spec["instance_type"] == payload["NodeType"]
        assert spec["region"] == "us-east-1"

    def test_opensearch_joins_the_two_fields_it_splits_the_type_across(self):
        """`ESInstanceDetails` has no InstanceType — only class and size."""
        spec = api.describe_recommendation_spec(
            {
                "InstanceDetails": {
                    "ESInstanceDetails": {
                        "InstanceClass": "r6g",
                        "InstanceSize": "large.search",
                        "Region": "eu-west-1",
                        "CurrentGeneration": True,
                    }
                }
            }
        )
        assert spec["instance_type"] == "r6g.large.search"
        assert spec["family"] == "", "ESInstanceDetails has no Family field"

    def test_opensearch_missing_size_does_not_leave_a_trailing_dot(self):
        spec = api.describe_recommendation_spec(
            {"InstanceDetails": {"ESInstanceDetails": {"InstanceClass": "r6g"}}}
        )
        assert spec["instance_type"] == "r6g"

    def test_dynamodb_lives_under_a_different_container_and_has_no_instance(self):
        spec = api.describe_recommendation_spec(
            {
                "ReservedCapacityDetails": {
                    "DynamoDBCapacityDetails": {
                        "CapacityUnits": "1000",
                        "Region": "us-east-1",
                    }
                }
            }
        )
        assert spec["instance_type"] == ""
        assert spec["attributes"]["capacity units"] == "1000 capacity units"
        assert spec["label"] == "1000 capacity units · us-east-1"

    def test_unknown_shape_degrades_to_empty_rather_than_raising(self):
        """A new service must cost the caller the spec, not the whole finding."""
        assert api.describe_recommendation_spec({}) == {}
        assert api.describe_recommendation_spec({"InstanceDetails": {}}) == {}
        assert (
            api.describe_recommendation_spec(
                {"InstanceDetails": {"QuantumInstanceDetails": {"Foo": "bar"}}}
            )
            == {}
        )

    def test_previous_generation_is_recorded(self):
        """A 3-year commitment on an old generation locks out the cheaper one."""
        spec = api.describe_recommendation_spec(
            {
                "InstanceDetails": {
                    "EC2InstanceDetails": {
                        "InstanceType": "m4.large",
                        "CurrentGeneration": False,
                    }
                }
            }
        )
        assert spec["current_generation"] is False

    def test_every_spec_key_is_distinct_so_none_shadows_another(self):
        assert len(set(api.RECOMMENDATION_SPEC_KEYS)) == len(api.RECOMMENDATION_SPECS)


@pytest.mark.unit
class TestAttributeDisplay:
    def test_a_bare_number_carries_its_label(self):
        assert api._attribute_display("capacity units", "1000") == "1000 capacity units"

    def test_a_named_value_reads_as_itself(self):
        assert api._attribute_display("engine", "Aurora MySQL") == "Aurora MySQL"

    def test_blank_is_dropped(self):
        assert api._attribute_display("AZ", "  ") == ""
        assert api._attribute_display("AZ", None) == ""


# --------------------------------------------------- inventory spec attributes


class StubDescribe:
    def __init__(self, response):
        self.response = response

    def _respond(self, **kwargs):
        return self.response

    describe_reserved_instances = _respond
    describe_reserved_db_instances = _respond
    describe_reserved_cache_nodes = _respond
    describe_reserved_nodes = _respond


def clients_returning(response) -> api.Clients:
    stub = StubDescribe(response)
    return api.Clients(
        ce=stub,
        coh=stub,
        account_id="111122223333",
        profile=None,
        make_client=lambda service, region: stub,
    )


def rds_reservation(**overrides) -> dict:
    row = {
        "ReservedDBInstanceId": "rds-1",
        "DBInstanceCount": 2,
        "DBInstanceClass": "db.r6g.large",
        "StartTime": datetime(2025, 10, 1, tzinfo=timezone.utc),
        "Duration": ONE_YEAR_SECONDS,
        "State": "active",
        "MultiAZ": True,
        "ProductDescription": "postgresql",
    }
    row.update(overrides)
    return {"ReservedDBInstances": [row]}


@pytest.mark.unit
class TestReservationAttributes:
    def test_rds_inventory_reports_multi_az_and_engine(self):
        result = api.get_reservation_inventory(
            clients_returning(rds_reservation()), "rds", "ap-northeast-1"
        )
        [item] = result["items"]
        assert item["attributes"]["deployment"] == "Multi-AZ"
        assert item["attributes"]["engine"] == "postgresql"
        assert item["spec"] == "db.r6g.large · Multi-AZ · postgresql"

    def test_multi_az_false_is_single_az_not_missing(self):
        """A bool has to be tested against None; False is meaningful here."""
        result = api.get_reservation_inventory(
            clients_returning(rds_reservation(MultiAZ=False)), "rds", "us-east-1"
        )
        [item] = result["items"]
        assert item["attributes"]["deployment"] == "Single-AZ"
        assert "Single-AZ" in item["spec"]

    def test_absent_multi_az_is_omitted_rather_than_guessed(self):
        payload = rds_reservation()
        del payload["ReservedDBInstances"][0]["MultiAZ"]
        result = api.get_reservation_inventory(
            clients_returning(payload), "rds", "us-east-1"
        )
        [item] = result["items"]
        assert "deployment" not in item["attributes"]

    def test_ec2_zonal_scope_reports_the_availability_zone(self):
        """A zonal reservation only covers one AZ, so a renewal must match it."""
        result = api.get_reservation_inventory(
            clients_returning(
                {
                    "ReservedInstances": [
                        {
                            "ReservedInstancesId": "ri-1",
                            "InstanceCount": 3,
                            "InstanceType": "m5.large",
                            "Start": datetime(2025, 10, 1, tzinfo=timezone.utc),
                            "End": datetime(2026, 10, 1, tzinfo=timezone.utc),
                            "State": "active",
                            "Scope": "Availability Zone",
                            "AvailabilityZone": "ap-northeast-1c",
                            "ProductDescription": "Linux/UNIX",
                            "OfferingClass": "convertible",
                            "InstanceTenancy": "default",
                        }
                    ]
                }
            ),
            "ec2",
            "ap-northeast-1",
        )
        [item] = result["items"]
        assert item["attributes"]["AZ"] == "ap-northeast-1c"
        assert item["attributes"]["scope"] == "Availability Zone"
        assert item["attributes"]["class"] == "convertible"
        assert item["spec"].startswith("m5.large · Availability Zone · ap-northeast-1c")

    def test_savings_plan_spec_is_the_family_or_empty_by_design(self):
        """A Compute plan commits to dollars, so a blank spec is correct."""

        class StubSP:
            def describe_savings_plans(self, **kwargs):
                return {
                    "savingsPlans": [
                        {
                            "savingsPlanId": "sp-1",
                            "savingsPlanType": "EC2Instance",
                            "commitment": "5.0",
                            "start": "2025-10-01T00:00:00Z",
                            "end": "2026-10-01T00:00:00Z",
                            "state": "active",
                            "ec2InstanceFamily": "m5",
                            "region": "ap-northeast-1",
                            "paymentOption": "No Upfront",
                        },
                        {
                            "savingsPlanId": "sp-2",
                            "savingsPlanType": "Compute",
                            "commitment": "8.0",
                            "start": "2025-10-01T00:00:00Z",
                            "end": "2026-10-01T00:00:00Z",
                            "state": "active",
                        },
                    ]
                }

        stub = StubSP()
        result = api.get_savings_plan_inventory(
            api.Clients(
                ce=stub,
                coh=stub,
                account_id="111122223333",
                profile=None,
                make_client=lambda service, region=None: stub,
            )
        )
        ec2_plan, compute_plan = result["items"]
        assert ec2_plan["spec"] == "m5"
        assert compute_plan["spec"] == ""
        assert compute_plan["attributes"] == {}


# ------------------------------------------------------------ RI line items


def ri_rec(details: list[dict], monthly: str = "900.0") -> dict:
    return {
        "service": "Amazon Relational Database Service",
        "label": "RDS",
        "term": "ONE_YEAR",
        "payment": "ALL_UPFRONT",
        "summary": {
            "TotalEstimatedMonthlySavingsAmount": monthly,
            "TotalEstimatedMonthlySavingsPercentage": "31.0",
        },
        "details": details,
    }


def rds_detail(
    instance_type: str,
    deployment: str,
    recommended: str,
    savings: str,
    **overrides,
) -> dict:
    detail = {
        "RecommendedNumberOfInstancesToPurchase": recommended,
        "MinimumNumberOfInstancesUsedPerHour": recommended,
        "AverageNumberOfInstancesUsedPerHour": recommended,
        "EstimatedMonthlySavingsAmount": savings,
        "EstimatedMonthlyOnDemandCost": "2000.0",
        "UpfrontCost": "6000.0",
        "AverageUtilization": "94.0",
        "AccountId": "111122223333",
        "InstanceDetails": {
            "RDSInstanceDetails": {
                "Family": instance_type.rsplit(".", 1)[0],
                "InstanceType": instance_type,
                "Region": "ap-northeast-1",
                "DeploymentOption": deployment,
                "DatabaseEngine": "Aurora PostgreSQL",
                "CurrentGeneration": True,
                "SizeFlexEligible": True,
            }
        },
    }
    detail.update(overrides)
    return detail


@pytest.mark.unit
class TestRiLineItems:
    def test_each_line_carries_its_own_spec_and_savings(self):
        f = analyze_ri_recommendation(
            ri_rec(
                [
                    rds_detail("db.r6g.large", "Multi-AZ", "4", "600.0"),
                    rds_detail("db.t4g.medium", "Single-AZ", "2", "300.0"),
                ]
            )
        )
        assert [i.spec for i in f.line_items] == [
            "db.r6g.large · Multi-AZ · Aurora PostgreSQL · ap-northeast-1",
            "db.t4g.medium · Single-AZ · Aurora PostgreSQL · ap-northeast-1",
        ]
        assert [i.monthly_savings for i in f.line_items] == [600.0, 300.0]
        assert all(i.region == "ap-northeast-1" for i in f.line_items)
        assert all(i.unit == "units" for i in f.line_items)

    def test_lines_are_ranked_by_savings_not_api_order(self):
        f = analyze_ri_recommendation(
            ri_rec(
                [
                    rds_detail("db.t4g.medium", "Single-AZ", "2", "100.0"),
                    rds_detail("db.r6g.large", "Multi-AZ", "4", "800.0"),
                ]
            )
        )
        assert f.line_items[0].spec.startswith("db.r6g.large")

    def test_a_multi_spec_total_is_disclosed_as_a_budget_not_an_order(self):
        f = analyze_ri_recommendation(
            ri_rec(
                [
                    rds_detail("db.r6g.large", "Multi-AZ", "4", "600.0"),
                    rds_detail("db.t4g.medium", "Single-AZ", "2", "300.0"),
                ]
            )
        )
        joined = " ".join(f.rationale)
        assert "2 distinct instance specifications" in joined
        assert "budget, not an order" in joined

    def test_a_single_spec_recommendation_adds_no_such_caveat(self):
        f = analyze_ri_recommendation(
            ri_rec([rds_detail("db.r6g.large", "Multi-AZ", "4", "600.0")])
        )
        assert not any("distinct instance specifications" in n for n in f.rationale)

    def test_achievable_per_line_is_whole_reservations(self):
        """Reservations are sold whole, so a line never asks for 2.4 of one."""
        f = analyze_ri_recommendation(
            ri_rec(
                [
                    rds_detail("db.r6g.large", "Multi-AZ", "4", "600.0"),
                    rds_detail("db.t4g.medium", "Single-AZ", "3", "300.0"),
                ]
            )
        )
        for item in f.line_items:
            assert item.achievable == int(item.achievable)
            assert item.achievable <= item.recommended

    def test_missing_sub_structure_is_labelled_rather_than_left_blank(self):
        detail = rds_detail("db.r6g.large", "Multi-AZ", "4", "600.0")
        del detail["InstanceDetails"]
        f = analyze_ri_recommendation(ri_rec([detail]))
        assert f.line_items[0].spec == SPEC_UNAVAILABLE

    def test_capacity_unit_services_fall_back_to_their_own_field_names(self):
        f = analyze_ri_recommendation(
            {
                "service": "Amazon DynamoDB Service",
                "label": "DynamoDB",
                "term": "ONE_YEAR",
                "payment": "NO_UPFRONT",
                "summary": {"TotalEstimatedMonthlySavingsAmount": "400.0"},
                "details": [
                    {
                        "RecommendedNumberOfCapacityUnitsToPurchase": "100",
                        "MinimumNumberOfCapacityUnitsUsedPerHour": "95",
                        "AverageNumberOfCapacityUnitsUsedPerHour": "100",
                        "EstimatedMonthlySavingsAmount": "400.0",
                        "ReservedCapacityDetails": {
                            "DynamoDBCapacityDetails": {
                                "CapacityUnits": "100",
                                "Region": "us-east-1",
                            }
                        },
                    }
                ],
            }
        )
        [item] = f.line_items
        assert item.recommended == pytest.approx(100.0)
        assert item.floor == pytest.approx(95.0)
        assert item.spec == "100 capacity units · us-east-1"

    def test_absent_utilization_is_none_not_zero(self):
        """Zero utilization would read as a warning that AWS never issued."""
        detail = rds_detail("db.r6g.large", "Multi-AZ", "4", "600.0")
        del detail["AverageUtilization"]
        f = analyze_ri_recommendation(ri_rec([detail]))
        assert f.line_items[0].utilization_pct is None


@pytest.mark.unit
class TestSpLineItems:
    def sp_rec(self, details: list[dict]) -> dict:
        return {
            "sp_type": "EC2_INSTANCE_SP",
            "term": "ONE_YEAR",
            "payment": "NO_UPFRONT",
            "lookback": "THIRTY_DAYS",
            "account_scope": "PAYER",
            "summary": {
                "HourlyCommitmentToPurchase": "10.0",
                "EstimatedMonthlySavingsAmount": "1000.0",
                "EstimatedSavingsPercentage": "20.5",
                "CurrentOnDemandSpend": "8000.0",
            },
            "details": details,
            "recommendation_id": "rec-1",
        }

    def test_ec2_instance_plan_reports_family_and_region(self):
        f = analyze_sp_recommendation(
            self.sp_rec(
                [
                    {
                        "HourlyCommitmentToPurchase": "6.0",
                        "CurrentMinimumHourlyOnDemandSpend": "6.0",
                        "CurrentAverageHourlyOnDemandSpend": "7.0",
                        "CurrentMaximumHourlyOnDemandSpend": "9.0",
                        "EstimatedMonthlySavingsAmount": "700.0",
                        "EstimatedAverageUtilization": "98.0",
                        "SavingsPlansDetails": {
                            "Region": "ap-northeast-1",
                            "InstanceFamily": "m5",
                            "OfferingId": "off-1",
                        },
                    },
                    {
                        "HourlyCommitmentToPurchase": "4.0",
                        "CurrentMinimumHourlyOnDemandSpend": "4.0",
                        "CurrentAverageHourlyOnDemandSpend": "5.0",
                        "EstimatedMonthlySavingsAmount": "300.0",
                        "SavingsPlansDetails": {
                            "Region": "us-east-1",
                            "InstanceFamily": "c6g",
                        },
                    },
                ]
            )
        )
        assert [(i.spec, i.region) for i in f.line_items] == [
            ("m5", "ap-northeast-1"),
            ("c6g", "us-east-1"),
        ]
        assert all(i.unit == "USD/hour" for i in f.line_items)

    def test_a_compute_plan_says_it_is_flexible_rather_than_blank(self):
        f = analyze_sp_recommendation(
            self.sp_rec(
                [
                    {
                        "HourlyCommitmentToPurchase": "10.0",
                        "CurrentMinimumHourlyOnDemandSpend": "9.0",
                        "CurrentAverageHourlyOnDemandSpend": "10.0",
                        "EstimatedMonthlySavingsAmount": "1000.0",
                    }
                ]
            )
        )
        assert f.line_items[0].spec == "any instance family"

    def test_dollar_commitments_are_not_rounded_to_whole_units(self):
        f = analyze_sp_recommendation(
            self.sp_rec(
                [
                    {
                        "HourlyCommitmentToPurchase": "6.5",
                        "CurrentMinimumHourlyOnDemandSpend": "4.0",
                        "CurrentAverageHourlyOnDemandSpend": "8.0",
                        "EstimatedMonthlySavingsAmount": "700.0",
                    }
                ]
            )
        )
        [item] = f.line_items
        assert item.achievable != int(item.achievable) or item.achievable == 6.5


# ---------------------------------------------------------------- report surface


def payload(findings, expiry=None) -> dict:
    return {
        "meta": {
            "account_id": "111122223333",
            "profile": "test",
            "generated_at": "2026-09-04 00:00 UTC",
            "lookback": "THIRTY_DAYS",
            "account_scope": "PAYER",
        },
        "findings": findings,
        "posture": {"blockers": [], "notes": []},
        "reconciliation": {"status": "unavailable", "reason": "test"},
        "eligible_spend": {"periods": []},
        "expiry": expiry,
        "errors": [],
    }


@pytest.mark.integration
class TestReportShowsTheSpec:
    def test_line_items_table_names_each_instance_type_and_deployment(self):
        f = analyze_ri_recommendation(
            ri_rec(
                [
                    rds_detail("db.r6g.large", "Multi-AZ", "4", "600.0"),
                    rds_detail("db.t4g.medium", "Single-AZ", "2", "300.0"),
                ]
            )
        )
        md = render(payload([f]))
        assert "Line items — what to buy" in md
        assert "db.r6g.large · Multi-AZ" in md
        assert "db.t4g.medium · Single-AZ" in md

    def test_size_flexibility_and_generation_are_flagged_per_line(self):
        detail = rds_detail("db.m4.large", "Single-AZ", "2", "300.0")
        detail["InstanceDetails"]["RDSInstanceDetails"]["CurrentGeneration"] = False
        detail["InstanceDetails"]["RDSInstanceDetails"]["SizeFlexEligible"] = True
        md = render(payload([analyze_ri_recommendation(ri_rec([detail]))]))
        assert "size-flexible" in md
        assert "previous generation" in md

    def test_a_line_with_nothing_to_buy_is_not_listed(self):
        detail = rds_detail("db.r6g.large", "Multi-AZ", "0", "0.0")
        md = render(payload([analyze_ri_recommendation(ri_rec([detail]))]))
        assert "Line items — what to buy" not in md

    def test_rounding_shortfall_is_disclosed_rather_than_padded(self):
        """Per-line whole-unit rounding can undershoot the family total."""
        f = analyze_ri_recommendation(
            ri_rec(
                [
                    rds_detail("db.r6g.large", "Multi-AZ", "5", "600.0", **{
                        "MinimumNumberOfInstancesUsedPerHour": "3",
                        "AverageNumberOfInstancesUsedPerHour": "5",
                    }),
                    rds_detail("db.t4g.medium", "Single-AZ", "3", "300.0", **{
                        "MinimumNumberOfInstancesUsedPerHour": "2",
                        "AverageNumberOfInstancesUsedPerHour": "3",
                    }),
                ]
            )
        )
        allocated = sum(i.achievable for i in f.line_items)
        md = render(payload([f]))
        if allocated < f.safe_hourly_commitment:
            assert "unallocated against the" in md
        assert "carries no" in md and "unused-commitment risk" in md

    def test_expiry_table_shows_what_a_renewal_has_to_match(self):
        expiry, _ = None, None
        inventory = [
            {
                "family": "reserved-instance",
                "label": "RDS Reserved Instance",
                "commitment_id": "rds-1",
                "instance_type": "db.r6g.large",
                "spec": "db.r6g.large · Multi-AZ · postgresql",
                "attributes": {"deployment": "Multi-AZ", "engine": "postgresql"},
                "quantity": 2,
                "unit": "units",
                "region": "ap-northeast-1",
                "start": "2025-10-01",
                "end": "2026-10-01",
                "state": "active",
                "term_months": 12,
                "payment_option": "All Upfront",
            }
        ]
        expiry = analyze_expiry(inventory, date(2026, 9, 4), 90, ri_utilization_pct=95.0)
        md = render(payload([], expiry=expiry))
        assert "| Spec |" in md
        assert "db.r6g.large · Multi-AZ · postgresql" in md
        assert "Single-AZ vs Multi-AZ" in md

    def test_a_specless_expiring_commitment_renders_a_dash(self):
        expiry = analyze_expiry(
            [
                {
                    "family": "savings-plan",
                    "label": "Compute Savings Plan",
                    "commitment_id": "sp-1",
                    "instance_type": "",
                    "spec": "",
                    "attributes": {},
                    "quantity": 5.0,
                    "unit": "USD/hour",
                    "region": "",
                    "start": "2025-10-01",
                    "end": "2026-10-01",
                    "state": "active",
                    "term_months": 12,
                    "payment_option": "No Upfront",
                }
            ],
            date(2026, 9, 4),
            90,
            sp_utilization_pct=99.0,
        )
        md = render(payload([], expiry=expiry))
        assert "| — |" in md
        assert "commits to dollars, not to a family" in md


# --------------------------------------------------------------- JSON envelope


@pytest.mark.unit
class TestEnvelopeCarriesLineItems:
    def test_line_items_reach_the_json_a_caller_reads(self):
        f = analyze_ri_recommendation(
            ri_rec(
                [
                    rds_detail("db.r6g.large", "Multi-AZ", "4", "600.0"),
                    rds_detail("db.t4g.medium", "Single-AZ", "2", "300.0"),
                ]
            )
        )
        [rec] = collect.envelope([f])["recommendations"]
        assert len(rec["line_items"]) == 2
        first = rec["line_items"][0]
        assert first["spec"] == "db.r6g.large · Multi-AZ · Aurora PostgreSQL · ap-northeast-1"
        assert first["region"] == "ap-northeast-1"
        assert first["commitment_unit"] == "units"
        assert first["aws_recommended_commitment"] == 4.0
        assert first["minimum_observed_units"] == 4.0
        assert first["estimated_monthly_savings"] == 600.0
        assert first["size_flexible"] is True
        assert first["current_generation"] is True
        assert first["account_id"] == "111122223333"

    def test_absent_utilization_serializes_as_null_not_zero(self):
        detail = rds_detail("db.r6g.large", "Multi-AZ", "4", "600.0")
        del detail["AverageUtilization"]
        [rec] = collect.envelope([analyze_ri_recommendation(ri_rec([detail]))])[
            "recommendations"
        ]
        assert rec["line_items"][0]["estimated_utilization_percentage"] is None

    def test_savings_plan_lines_are_serialized_in_dollars_per_hour(self):
        f = analyze_sp_recommendation(
            {
                "sp_type": "COMPUTE_SP",
                "term": "ONE_YEAR",
                "payment": "NO_UPFRONT",
                "lookback": "THIRTY_DAYS",
                "account_scope": "PAYER",
                "summary": {
                    "HourlyCommitmentToPurchase": "10.0",
                    "EstimatedMonthlySavingsAmount": "1000.0",
                    "EstimatedSavingsPercentage": "20.0",
                    "CurrentOnDemandSpend": "8000.0",
                },
                "details": [
                    {
                        "HourlyCommitmentToPurchase": "10.0",
                        "CurrentMinimumHourlyOnDemandSpend": "9.0",
                        "CurrentAverageHourlyOnDemandSpend": "10.0",
                        "EstimatedMonthlySavingsAmount": "1000.0",
                    }
                ],
            }
        )
        [rec] = collect.envelope([f])["recommendations"]
        assert rec["line_items"][0]["commitment_unit"] == "USD/hour"

    def test_a_finding_with_no_line_items_serializes_an_empty_list(self):
        """Absent is not the same as unknown; the key is always present."""
        f = analyze_ri_recommendation(ri_rec([], monthly="0.0"))
        assert f is None or collect.envelope([f])["recommendations"][0][
            "line_items"
        ] == []
