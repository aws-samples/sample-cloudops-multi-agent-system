"""Unit + property-based tests for ``cloudwatch.arn.parse_arn_to_dimensions``.

Coverage matrix:

* One unit case per supported service in the dispatch table — verifies the
  exact ``(namespace, dimensions)`` triple expected by the CloudWatch APIs.
* Edge cases: malformed ARNs, bare ``arn:aws:``, unknown service prefixes,
  empty strings, non-strings.
* Property-based test (Hypothesis) for Property 1 in design.md — totality.
  Any string starting with ``arn:aws:`` either parses to a valid
  ``(namespace: str, dimensions: list[dict], info: dict)`` triple or returns
  the canonical ``unknown_resource_type`` triple. The parser never raises.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

# Load the module from its file path; the cloudwatch Lambda source dir
# isn't on sys.path by default and we don't want to monkey with conftest
# for a single module under test.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ARN_PATH = _REPO_ROOT / "src" / "lambda" / "mcp" / "cloudwatch" / "arn.py"
_spec = importlib.util.spec_from_file_location("cloudwatch_arn", _ARN_PATH)
arn_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arn_module)

parse_arn_to_dimensions = arn_module.parse_arn_to_dimensions
UNKNOWN = arn_module.UNKNOWN


# ---------------------------------------------------------------------------
# Per-service unit cases
# ---------------------------------------------------------------------------

# (test_id, arn, expected_namespace, expected_dimensions)
SERVICE_CASES = [
    (
        "lambda_function",
        "arn:aws:lambda:us-east-1:123456789012:function:my-fn",
        "AWS/Lambda",
        [{"Name": "FunctionName", "Value": "my-fn"}],
    ),
    (
        "lambda_function_with_alias",
        "arn:aws:lambda:us-east-1:123456789012:function:my-fn:PROD",
        "AWS/Lambda",
        [{"Name": "FunctionName", "Value": "my-fn"}],
    ),
    (
        "rds_db_instance",
        "arn:aws:rds:us-east-1:123456789012:db:my-db",
        "AWS/RDS",
        [{"Name": "DBInstanceIdentifier", "Value": "my-db"}],
    ),
    (
        "rds_db_cluster",
        "arn:aws:rds:us-east-1:123456789012:cluster:my-cluster",
        "AWS/RDS",
        [{"Name": "DBClusterIdentifier", "Value": "my-cluster"}],
    ),
    (
        "dynamodb_table",
        "arn:aws:dynamodb:us-east-1:123456789012:table/my-table",
        "AWS/DynamoDB",
        [{"Name": "TableName", "Value": "my-table"}],
    ),
    (
        "dynamodb_table_index",
        "arn:aws:dynamodb:us-east-1:123456789012:table/my-table/index/MyIndex",
        "AWS/DynamoDB",
        [{"Name": "TableName", "Value": "my-table"}],
    ),
    (
        "ec2_instance",
        "arn:aws:ec2:us-east-1:123456789012:instance/i-1234567890abcdef0",
        "AWS/EC2",
        [{"Name": "InstanceId", "Value": "i-1234567890abcdef0"}],
    ),
    (
        "ebs_volume",
        "arn:aws:ec2:us-east-1:123456789012:volume/vol-1234567890abcdef0",
        "AWS/EBS",
        [{"Name": "VolumeId", "Value": "vol-1234567890abcdef0"}],
    ),
    (
        "nat_gateway",
        "arn:aws:ec2:us-east-1:123456789012:natgateway/nat-0abc123",
        "AWS/NATGateway",
        [{"Name": "NatGatewayId", "Value": "nat-0abc123"}],
    ),
    (
        "vpn_connection",
        "arn:aws:ec2:us-east-1:123456789012:vpn-connection/vpn-0abc123",
        "AWS/VPN",
        [{"Name": "VpnId", "Value": "vpn-0abc123"}],
    ),
    (
        "transit_gateway",
        "arn:aws:ec2:us-east-1:123456789012:transit-gateway/tgw-0abc123",
        "AWS/TransitGateway",
        [{"Name": "TransitGateway", "Value": "tgw-0abc123"}],
    ),
    (
        "ecs_cluster",
        "arn:aws:ecs:us-east-1:123456789012:cluster/my-cluster",
        "AWS/ECS",
        [{"Name": "ClusterName", "Value": "my-cluster"}],
    ),
    (
        "ecs_service",
        "arn:aws:ecs:us-east-1:123456789012:service/my-cluster/my-svc",
        "AWS/ECS",
        [
            {"Name": "ClusterName", "Value": "my-cluster"},
            {"Name": "ServiceName", "Value": "my-svc"},
        ],
    ),
    (
        "eks_cluster",
        "arn:aws:eks:us-east-1:123456789012:cluster/prod-cluster",
        "AWS/EKS",
        [{"Name": "ClusterName", "Value": "prod-cluster"}],
    ),
    (
        "elasticache_cluster",
        "arn:aws:elasticache:us-east-1:123456789012:cluster:my-cache",
        "AWS/ElastiCache",
        [{"Name": "CacheClusterId", "Value": "my-cache"}],
    ),
    (
        "kinesis_stream",
        "arn:aws:kinesis:us-east-1:123456789012:stream/my-stream",
        "AWS/Kinesis",
        [{"Name": "StreamName", "Value": "my-stream"}],
    ),
    (
        "sqs_queue",
        "arn:aws:sqs:us-east-1:123456789012:my-queue",
        "AWS/SQS",
        [{"Name": "QueueName", "Value": "my-queue"}],
    ),
    (
        "sns_topic",
        "arn:aws:sns:us-east-1:123456789012:my-topic",
        "AWS/SNS",
        [{"Name": "TopicName", "Value": "my-topic"}],
    ),
    (
        "s3_bucket",
        "arn:aws:s3:::my-bucket",
        "AWS/S3",
        [{"Name": "BucketName", "Value": "my-bucket"}],
    ),
    (
        "s3_bucket_with_prefix",
        "arn:aws:s3:::my-bucket/some/prefix",
        "AWS/S3",
        [{"Name": "BucketName", "Value": "my-bucket"}],
    ),
    (
        "cloudfront_distribution",
        "arn:aws:cloudfront::123456789012:distribution/E1ABCDEF12345",
        "AWS/CloudFront",
        [
            {"Name": "DistributionId", "Value": "E1ABCDEF12345"},
            {"Name": "Region", "Value": "Global"},
        ],
    ),
    (
        "apigateway_rest_api",
        "arn:aws:apigateway:us-east-1::/restapis/abc123",
        "AWS/ApiGateway",
        [{"Name": "ApiName", "Value": "abc123"}],
    ),
    (
        "apigateway_http_api",
        "arn:aws:apigateway:us-east-1::/apis/xyz789",
        "AWS/ApiGateway",
        [{"Name": "ApiName", "Value": "xyz789"}],
    ),
    (
        "autoscaling_group",
        "arn:aws:autoscaling:us-east-1:123456789012:autoScalingGroup:abc-uuid:autoScalingGroupName/my-asg",
        "AWS/AutoScaling",
        [{"Name": "AutoScalingGroupName", "Value": "my-asg"}],
    ),
    (
        "redshift_cluster",
        "arn:aws:redshift:us-east-1:123456789012:cluster:my-redshift",
        "AWS/Redshift",
        [{"Name": "ClusterIdentifier", "Value": "my-redshift"}],
    ),
    (
        "redshift_serverless_workgroup",
        "arn:aws:redshift-serverless:us-east-1:123456789012:workgroup/abc-uuid",
        "AWS/Redshift-Serverless",
        [{"Name": "Workgroup", "Value": "abc-uuid"}],
    ),
    (
        "acm_certificate",
        "arn:aws:acm:us-east-1:123456789012:certificate/abc-uuid",
        "AWS/CertificateManager",
        [
            {
                "Name": "CertificateArn",
                "Value": "arn:aws:acm:us-east-1:123456789012:certificate/abc-uuid",
            }
        ],
    ),
    (
        "efs_file_system",
        "arn:aws:elasticfilesystem:us-east-1:123456789012:file-system/fs-12345",
        "AWS/EFS",
        [{"Name": "FileSystemId", "Value": "fs-12345"}],
    ),
    (
        "cognito_userpool",
        "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_abc123",
        "AWS/Cognito",
        [{"Name": "UserPool", "Value": "us-east-1_abc123"}],
    ),
    (
        "route53_healthcheck",
        "arn:aws:route53:::healthcheck/abc-uuid",
        "AWS/Route53",
        [{"Name": "HealthCheckId", "Value": "abc-uuid"}],
    ),
    (
        "alb",
        "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/my-alb/abc123",
        "AWS/ApplicationELB",
        [{"Name": "LoadBalancer", "Value": "app/my-alb/abc123"}],
    ),
    (
        "nlb",
        "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/net/my-nlb/abc123",
        "AWS/NetworkELB",
        [{"Name": "LoadBalancer", "Value": "net/my-nlb/abc123"}],
    ),
    (
        "classic_elb",
        "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/my-classic-elb",
        "AWS/ELB",
        [{"Name": "LoadBalancerName", "Value": "my-classic-elb"}],
    ),
    (
        "msk_cluster",
        "arn:aws:kafka:us-east-1:123456789012:cluster/my-msk/abc-uuid",
        "AWS/Kafka",
        # Dimension name has a literal space — quirk of the AWS/Kafka namespace.
        [{"Name": "Cluster Name", "Value": "my-msk"}],
    ),
    (
        "step_functions_state_machine",
        "arn:aws:states:us-east-1:123456789012:stateMachine:my-sm",
        "AWS/States",
        [
            {
                "Name": "StateMachineArn",
                "Value": "arn:aws:states:us-east-1:123456789012:stateMachine:my-sm",
            }
        ],
    ),
    (
        "opensearch_domain",
        "arn:aws:es:us-east-1:123456789012:domain/my-domain",
        "AWS/ES",
        [
            {"Name": "DomainName", "Value": "my-domain"},
            {"Name": "ClientId", "Value": "123456789012"},
        ],
    ),
]


@pytest.mark.parametrize(
    "test_id,arn,expected_namespace,expected_dims",
    SERVICE_CASES,
    ids=[c[0] for c in SERVICE_CASES],
)
def test_supported_services(test_id, arn, expected_namespace, expected_dims):
    namespace, dimensions, info = parse_arn_to_dimensions(arn)
    assert namespace == expected_namespace, f"{test_id}: wrong namespace"
    assert dimensions == expected_dims, f"{test_id}: wrong dimensions"
    assert isinstance(info, dict), f"{test_id}: info should be a dict"


# ---------------------------------------------------------------------------
# Negative / edge cases — totality (never raises)
# ---------------------------------------------------------------------------

UNKNOWN_CASES = [
    "",
    "not-an-arn",
    "arn:aws:",
    "arn:aws:lambda",
    "arn:aws:lambda:us-east-1:123456789012:",
    "arn:aws:unknown-service:us-east-1:123456789012:resource",
    # Wrong resource type for the service:
    "arn:aws:lambda:us-east-1:123456789012:layer:my-layer:1",
    "arn:aws:rds:us-east-1:123456789012:snapshot:snap-1",
    "arn:aws:ec2:us-east-1:123456789012:image/ami-12345",
    "arn:aws:ecs:us-east-1:123456789012:service/just-cluster-no-svc/",
    "arn:aws:dynamodb:us-east-1:123456789012:nottable/my-table",
    # Empty resource part:
    "arn:aws:s3:::",
    # AWS partition variant we don't support yet:
    "arn:aws-cn:lambda:cn-north-1:123456789012:function:my-fn",
]


@pytest.mark.parametrize("arn", UNKNOWN_CASES)
def test_unknown_arns_return_canonical_triple(arn):
    result = parse_arn_to_dimensions(arn)
    assert result == UNKNOWN, f"expected UNKNOWN for {arn!r}, got {result!r}"


def test_non_string_inputs_never_raise():
    for bad in [None, 42, 3.14, [], {}, b"arn:aws:lambda"]:
        result = parse_arn_to_dimensions(bad)  # type: ignore[arg-type]
        assert result == UNKNOWN


# ---------------------------------------------------------------------------
# Property-based: totality (Property 1)
# ---------------------------------------------------------------------------

# Build ARN-shaped fuzz inputs: anything starting with "arn:aws:" plus
# random colons, slashes, and printable junk. The property: parse_arn either
# returns a valid (namespace, dimensions, info) triple or the UNKNOWN triple.
# Never raises. Never returns a partially-populated triple.

_ARN_BODY = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        whitelist_characters=":/-_.,*[]{}",
    ),
    max_size=200,
)

_ARN_STRATEGY = st.builds(lambda body: f"arn:aws:{body}", _ARN_BODY)


def _is_valid_namespaced_triple(result):
    """A returned triple is well-shaped iff:
      * namespace is a non-empty string starting with 'AWS/' or a known
        non-AWS-prefixed namespace (none today, but be permissive),
      * dimensions is a list of {Name, Value} dicts,
      * info is a dict.
    """
    namespace, dimensions, info = result
    if not isinstance(namespace, str) or not namespace:
        return False
    if not isinstance(dimensions, list):
        return False
    for d in dimensions:
        if not (isinstance(d, dict) and "Name" in d and "Value" in d):
            return False
        if not isinstance(d["Name"], str) or not isinstance(d["Value"], str):
            return False
    if not isinstance(info, dict):
        return False
    return True


@given(_ARN_STRATEGY)
@settings(
    max_examples=300,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_property_arn_totality(arn):
    """Property 1: parse_arn_to_dimensions is total over arn:aws:* inputs.

    Validates: Requirements 1.2
    """
    result = parse_arn_to_dimensions(arn)

    # Always a 3-tuple.
    assert isinstance(result, tuple) and len(result) == 3

    namespace, dimensions, info = result

    # Either the canonical UNKNOWN triple, or a fully-shaped namespaced triple.
    if namespace is None:
        assert dimensions == []
        assert info == {"note": "unknown_resource_type"}
    else:
        assert _is_valid_namespaced_triple(result)


@given(st.text(max_size=400))
@settings(
    max_examples=300,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_property_arbitrary_inputs_never_raise(s):
    """Inputs that don't even start with arn:aws: must still not raise."""
    result = parse_arn_to_dimensions(s)
    assert isinstance(result, tuple) and len(result) == 3
    namespace, dimensions, info = result
    if namespace is None:
        assert dimensions == []
        assert info == {"note": "unknown_resource_type"}
    else:
        assert _is_valid_namespaced_triple(result)
