"""Contract tests: every AWS field name the commitments tool reads must exist.

Why these are separate from the rest of the suite
-------------------------------------------------
The other commitments tests feed hand-written fixtures through the collection
and analysis code. That proves the logic, but it cannot catch the one failure
mode this tool is most exposed to: a field name that does not exist.

`SpecShape` and `InventorySpec` are lookup tables of AWS response field names.
Every read goes through `.get(field)`, which returns `None` for a typo exactly
as it does for a field AWS genuinely omitted. So `InstanceTpye` does not raise —
it produces a recommendation with no instance type, or a commitment with no
identifier, and the report renders around the hole. A fixture written from the
same table as the code under test agrees with the typo and stays green.

botocore ships the service models the SDK itself dispatches on, so the real
field names are already on disk. These tests read them and assert every name in
our tables is a member of the corresponding output shape. That makes a typo, or
an AWS rename, a red test offline rather than a silent blank column in a live
report.

Scope and limits
----------------
This validates *names and shape membership*. It does not validate that a field
is populated for any given account — that needs a live account holding the
commitment or recommendation in question, and specifically an account with
OpenSearch or DynamoDB steady-state usage for those two shapes. What it does
guarantee is that when such an account is finally used, a blank column means
"AWS omitted it", never "we spelled it wrong".
"""

import botocore.session
import pytest
from commitments.api import (
    ACTIVE_SP_STATES,
    RECOMMENDATION_SPECS,
    RESERVATION_INVENTORY,
)


@pytest.fixture(scope="module")
def botocore_session():
    """One botocore session for the module — loading service models is slow."""
    return botocore.session.get_session()


def structure_members(shape):
    """Members of a shape, unwrapping list nesting.

    AWS models these as `list[structure]` at almost every level
    (`Recommendations`, `RecommendationDetails`, `ReservedInstances`), and only
    the member structure carries field names. Returns `{}` for a scalar so a
    caller gets an empty membership test rather than an AttributeError.
    """
    while shape.type_name == "list":
        shape = shape.member
    return shape.members if shape.type_name == "structure" else {}


def operation_for(service_model, method):
    """Map a boto3 snake_case method to its model operation name.

    Comparing `method.replace("_", "")` case-insensitively against the model's
    operation names avoids hand-maintaining a title-case map — the naive
    `str.title()` transform gets `DescribeReservedDBInstances` wrong, because
    boto3's `describe_reserved_db_instances` capitalizes DB but title() does not.
    """
    target = method.replace("_", "").lower()
    for name in service_model.operation_names:
        if name.lower() == target:
            return service_model.operation_model(name)
    return None


# ---------------------------------------------------------------------------
# Cost Explorer: the purchasable spec inside a recommendation
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def recommendation_detail_members(botocore_session):
    """Members of one `RecommendationDetails[]` entry from the CE model."""
    model = botocore_session.get_service_model("ce").operation_model(
        "GetReservationPurchaseRecommendation"
    )
    recommendations = structure_members(model.output_shape)["Recommendations"]
    details = structure_members(recommendations)["RecommendationDetails"]
    return structure_members(details)


@pytest.mark.parametrize("spec", RECOMMENDATION_SPECS, ids=lambda s: s.key)
def test_recommendation_spec_container_exists(spec, recommendation_detail_members):
    """The sub-object a spec lives under is a real field on the detail.

    Two containers are in play and the difference is easy to get wrong:
    everything instance-shaped hangs off `InstanceDetails`, but DynamoDB
    reserved capacity hangs off `ReservedCapacityDetails`.
    """
    assert spec.container in recommendation_detail_members, (
        f"{spec.key} is declared under RecommendationDetails.{spec.container}, "
        f"which the CE model does not define. Available: "
        f"{sorted(recommendation_detail_members)}"
    )


@pytest.mark.parametrize("spec", RECOMMENDATION_SPECS, ids=lambda s: s.key)
def test_recommendation_spec_key_exists(spec, recommendation_detail_members):
    """The service-specific sub-object itself exists inside its container."""
    container = structure_members(recommendation_detail_members[spec.container])
    assert spec.key in container, (
        f"{spec.container}.{spec.key} is not in the CE model. "
        f"Available: {sorted(container)}"
    )


@pytest.mark.parametrize("spec", RECOMMENDATION_SPECS, ids=lambda s: s.key)
def test_recommendation_spec_fields_exist(spec, recommendation_detail_members):
    """Every field a spec reads is a member of its sub-object.

    This is the assertion that would have caught `InstanceClass`/`InstanceSize`
    being wrong for OpenSearch, or `Family` being assumed present on a shape
    that has no family at all.
    """
    container = structure_members(recommendation_detail_members[spec.container])
    members = structure_members(container[spec.key])

    expected = [*spec.size_fields, spec.region_field]
    expected += [field for field, _label in spec.attribute_fields]
    if spec.family_field:
        expected.append(spec.family_field)

    missing = [field for field in expected if field not in members]
    assert not missing, (
        f"{spec.key} reads {missing}, which the CE model does not define. "
        f"Available: {sorted(members)}"
    )


@pytest.mark.parametrize("spec", RECOMMENDATION_SPECS, ids=lambda s: s.key)
def test_size_flex_fields_present_only_where_meaningful(
    spec, recommendation_detail_members
):
    """`SizeFlexEligible`/`CurrentGeneration` exist for instances, not DynamoDB.

    `describe_recommendation_spec` reads both unconditionally via
    `bool(raw.get(...))`. For DynamoDB reserved capacity AWS models neither
    field — there is no instance, so there is no size to flex and no generation
    to be current. `False` is therefore the correct answer, not a data gap, and
    this test pins that so nobody "fixes" the absence by inventing a field name.
    """
    container = structure_members(recommendation_detail_members[spec.container])
    members = structure_members(container[spec.key])
    flex_fields = ("SizeFlexEligible", "CurrentGeneration")

    if spec.key == "DynamoDBCapacityDetails":
        assert all(field not in members for field in flex_fields), (
            "DynamoDB capacity now models size flexibility — "
            "describe_recommendation_spec can report it for real instead of False"
        )
    else:
        missing = [field for field in flex_fields if field not in members]
        assert not missing, f"{spec.key} no longer models {missing}"


def test_every_recommendation_container_is_covered(recommendation_detail_members):
    """No CE detail sub-object goes unread without a deliberate decision.

    AWS adds services to this API over time. `describe_recommendation_spec`
    degrades to `{}` for an unknown shape, so a new service costs the report its
    spec column silently. Listing the known containers here turns that into a
    failing test the next time AWS adds one.
    """
    known = {spec.container for spec in RECOMMENDATION_SPECS}
    modelled = {
        name
        for name in recommendation_detail_members
        if name.endswith(("InstanceDetails", "CapacityDetails"))
    }
    assert modelled == known, (
        f"CE models detail containers {sorted(modelled - known)} that "
        f"RECOMMENDATION_SPECS does not cover"
    )


# ---------------------------------------------------------------------------
# Reservation inventory: the describe APIs behind expiry tracking
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def inventory_members(botocore_session):
    """`{spec.key: members of one response row}` for every inventory API."""
    resolved = {}
    for spec in RESERVATION_INVENTORY:
        service = botocore_session.get_service_model(spec.service)
        operation = operation_for(service, spec.method)
        assert operation is not None, (
            f"{spec.service} has no operation for {spec.method}"
        )
        top = structure_members(operation.output_shape)
        assert spec.response_key in top, (
            f"{spec.service}.{spec.method} has no {spec.response_key}; "
            f"available: {sorted(top)}"
        )
        resolved[spec.key] = structure_members(top[spec.response_key])
    return resolved


@pytest.mark.parametrize("spec", RESERVATION_INVENTORY, ids=lambda s: s.key)
def test_inventory_identity_fields_exist(spec, inventory_members):
    """id/count/type/payment fields exist — a typo here yields blank rows.

    Six APIs name the same four concepts six ways
    (`ReservedInstancesId` vs `ReservedDBInstanceId` vs `ReservationId`), which
    is precisely why the table exists and precisely why it needs checking.
    """
    members = inventory_members[spec.key]
    expected = (spec.id_field, spec.count_field, spec.type_field, spec.payment_field)
    missing = [field for field in expected if field not in members]
    assert not missing, (
        f"{spec.key} reads {missing}, absent from {spec.service}.{spec.method}. "
        f"Available: {sorted(members)}"
    )


@pytest.mark.parametrize("spec", RESERVATION_INVENTORY, ids=lambda s: s.key)
def test_inventory_term_fields_exist(spec, inventory_members):
    """Start exists, and expiry is readable either directly or via Duration.

    Only EC2 returns an explicit `End`. Everywhere else the end date is derived
    from start + `Duration` seconds, so a family with neither `end_field` nor
    `Duration` would silently report no expiry — the one thing this feature is
    for.
    """
    members = inventory_members[spec.key]
    assert spec.start_field in members, (
        f"{spec.key} start field {spec.start_field} absent from "
        f"{spec.service}.{spec.method}"
    )
    if spec.end_field:
        assert spec.end_field in members, (
            f"{spec.key} end field {spec.end_field} absent from "
            f"{spec.service}.{spec.method}"
        )
    else:
        assert "Duration" in members, (
            f"{spec.key} has no end_field and {spec.service}.{spec.method} "
            f"models no Duration, so its expiry cannot be derived"
        )


@pytest.mark.parametrize("spec", RESERVATION_INVENTORY, ids=lambda s: s.key)
def test_inventory_state_field_exists(spec, inventory_members):
    """`State` exists — it is what separates a live commitment from history."""
    assert "State" in inventory_members[spec.key], (
        f"{spec.service}.{spec.method} models no State, so "
        f"ACTIVE_RESERVATION_STATES cannot filter retired rows"
    )


@pytest.mark.parametrize("spec", RESERVATION_INVENTORY, ids=lambda s: s.key)
def test_inventory_attribute_and_arn_fields_exist(spec, inventory_members):
    """Renewal-matching attributes exist on the wire.

    These are the dimensions a renewal has to match — RDS `MultiAZ` and
    `ProductDescription`, EC2 `Scope`/`AvailabilityZone`. A typo drops one
    silently, and a renewal bought against an unstated deployment option or
    engine does not apply the discount.
    """
    members = inventory_members[spec.key]
    expected = [field for field, _label in spec.attribute_fields]
    if spec.arn_field:
        expected.append(spec.arn_field)
    missing = [field for field in expected if field not in members]
    assert not missing, (
        f"{spec.key} reads {missing}, absent from {spec.service}.{spec.method}. "
        f"Available: {sorted(members)}"
    )


def test_rds_multi_az_is_still_boolean(inventory_members, botocore_session):
    """`MultiAZ` is a bool, which is why it is tested against None, not truth.

    `False` means Single-AZ — a real, expensive specification — so the code
    checks `is not None` and renders through INVENTORY_ATTRIBUTE_VALUES rather
    than treating a falsy value as absent. If AWS ever changed this to a string
    that logic would need revisiting, so the type is pinned here.
    """
    service = botocore_session.get_service_model("rds")
    operation = operation_for(service, "describe_reserved_db_instances")
    members = structure_members(
        structure_members(operation.output_shape)["ReservedDBInstances"]
    )
    assert members["MultiAZ"].type_name == "boolean"


# ---------------------------------------------------------------------------
# Savings Plans: the one family denominated in dollars
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def savings_plan_model(botocore_session):
    return botocore_session.get_service_model("savingsplans")


def test_savings_plan_fields_exist(savings_plan_model):
    """Every `describe_savings_plans` field the collector reads exists.

    `ec2InstanceFamily` matters most: it is present for an EC2 Instance Savings
    Plan and absent for a Compute plan, and the collector renders the absence as
    an empty spec on purpose. That distinction only holds if the field name is
    right — a typo would make *every* plan look like a Compute plan.
    """
    members = structure_members(
        structure_members(
            savings_plan_model.operation_model("DescribeSavingsPlans").output_shape
        )["savingsPlans"]
    )
    expected = (
        "savingsPlanId",
        "savingsPlanArn",
        "savingsPlanType",
        "ec2InstanceFamily",
        "commitment",
        "region",
        "state",
        "paymentOption",
        "start",
        "end",
    )
    missing = [field for field in expected if field not in members]
    assert not missing, (
        f"describe_savings_plans no longer models {missing}. "
        f"Available: {sorted(members)}"
    )


def test_active_sp_states_are_valid_enum_values(savings_plan_model):
    """`ACTIVE_SP_STATES` are real API enum values.

    These go to the API as a server-side `states` filter, so an invalid value is
    a ValidationException at collection time — in a Lambda, against a live
    account, rather than here.
    """
    valid = savings_plan_model.shape_for("SavingsPlanState").enum
    invalid = [state for state in ACTIVE_SP_STATES if state not in valid]
    assert not invalid, f"{invalid} are not SavingsPlanState values; valid: {valid}"


def test_savings_plan_commitment_is_a_string_on_the_wire(savings_plan_model):
    """`commitment` arrives as a string, so the float() conversion is required.

    AWS returns "1.00000000", not 1.0. Summing these without converting would
    concatenate them, which is the kind of bug that produces a plausible-looking
    but wrong dollar figure in a report.
    """
    members = structure_members(
        structure_members(
            savings_plan_model.operation_model("DescribeSavingsPlans").output_shape
        )["savingsPlans"]
    )
    assert members["commitment"].type_name == "string"
