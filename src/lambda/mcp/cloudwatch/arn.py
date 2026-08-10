"""ARN-to-CloudWatch-dimensions parser.

Pure function: takes an AWS resource ARN, returns the CloudWatch
``(namespace, dimensions, info)`` triple needed to look up alarm
recommendations or fetch metric data for that resource.

Property guarantees (Property 1 in design.md):

* Totality. Any input string is safe to pass — the function never raises.
  Inputs that don't start with ``arn:aws:`` or whose service/resource-type
  isn't known to this module return ``(None, [], {"note": "unknown_resource_type"})``.
* Shape. When a namespace is returned, ``dimensions`` is a list of
  ``{"Name": str, "Value": str}`` dicts in the order CloudWatch expects.
* Pure. No I/O, no AWS calls, no globals modified.

# Supported services
#
# Catalogue-backed (`metric_metadata.json` ships a recommendation):
#   AWS/Lambda, AWS/RDS, AWS/DynamoDB, AWS/EC2 (incl. EBS, NAT GW, VPN,
#   Transit GW), AWS/ECS, AWS/EKS, AWS/ElastiCache, AWS/Kinesis, AWS/SQS,
#   AWS/SNS, AWS/S3, AWS/CloudFront, AWS/ApiGateway, AWS/AutoScaling,
#   AWS/Redshift, AWS/Redshift-Serverless, AWS/CertificateManager, AWS/EFS,
#   AWS/Cognito, AWS/Route53.
#
# Catalogue-not-backed but commonly requested (parser still returns the
# right namespace + dimensions; recommendation-lookup callers will get an
# empty list and surface a "no_recommendations_in_catalogue" note):
#   AWS/ApplicationELB, AWS/NetworkELB, AWS/ELB (Classic),
#   AWS/States (Step Functions), AWS/Kafka (MSK), AWS/ES (OpenSearch).
#
# Computed from the catalogue:
#   import json
#   cat = json.load(open("data/metric_metadata.json"))
#   sorted({e["metricId"]["namespace"] for e in cat})
"""

from __future__ import annotations

from typing import Callable

# --- Public API ------------------------------------------------------------

UNKNOWN: tuple[None, list[dict], dict] = (None, [], {"note": "unknown_resource_type"})


def parse_arn_to_dimensions(arn: str) -> tuple[str | None, list[dict], dict]:
    """Parse an AWS resource ARN to its CloudWatch ``(namespace, dimensions, info)``.

    Args:
        arn: AWS resource ARN, e.g. ``arn:aws:lambda:us-east-1:123456789012:function:my-fn``.

    Returns:
        A 3-tuple:
          * ``namespace``: the CloudWatch namespace (e.g. ``"AWS/Lambda"``) or
            ``None`` if the resource type isn't recognised.
          * ``dimensions``: a list of ``{"Name": str, "Value": str}`` dicts
            ready to pass to CloudWatch APIs. Empty list when ``namespace`` is None.
          * ``info``: free-form dict for caller-relevant notes (e.g. when the
            ARN doesn't carry the dimension value the API actually needs).
            Empty dict on the happy path.

    Never raises. Inputs that don't start with ``arn:aws:`` or whose service
    isn't in the dispatch table return ``(None, [], {"note": "unknown_resource_type"})``.
    """
    if not isinstance(arn, str) or not arn.startswith("arn:aws:"):
        return UNKNOWN

    # ARN format: arn:aws:<service>:<region>:<account>:<resource_part>
    # Split with maxsplit=5 so the resource_part keeps any embedded colons
    # (e.g. autoscaling, step functions) intact.
    parts = arn.split(":", 5)
    if len(parts) < 6:
        return UNKNOWN

    service = parts[2]
    resource_part = parts[5]
    if not service or not resource_part:
        return UNKNOWN

    parser = _PARSERS.get(service)
    if parser is None:
        return UNKNOWN

    try:
        return parser(parts, resource_part)
    except Exception:  # never raise — totality is the property
        return UNKNOWN


# --- Internal helpers ------------------------------------------------------


def _split_resource(resource_part: str) -> tuple[str | None, str]:
    """Split a resource part into ``(resource_type, resource_id)``.

    Handles the three common shapes:
      * ``type/id``      → ("type", "id")
      * ``type:id``      → ("type", "id")
      * ``id``           → (None, "id")

    Multi-segment ids (e.g. ``loadbalancer/app/name/uuid``) are returned
    intact in the id slot — the per-service parser splits further.
    """
    if "/" in resource_part:
        rtype, _, rid = resource_part.partition("/")
        return rtype or None, rid
    if ":" in resource_part:
        rtype, _, rid = resource_part.partition(":")
        return rtype or None, rid
    return None, resource_part


def _dim(name: str, value: str) -> dict:
    return {"Name": name, "Value": value}


# --- Per-service parsers ---------------------------------------------------


def _parse_lambda(parts, resource_part):
    # arn:aws:lambda:region:account:function:name[:qualifier]
    rtype, rid = _split_resource(resource_part)
    if rtype != "function" or not rid:
        return UNKNOWN
    # Strip optional version/alias qualifier ("name:1" or "name:PROD").
    fn_name = rid.split(":", 1)[0]
    if not fn_name:
        return UNKNOWN
    return ("AWS/Lambda", [_dim("FunctionName", fn_name)], {})


def _parse_rds(parts, resource_part):
    # arn:aws:rds:region:account:db:identifier        (instance)
    # arn:aws:rds:region:account:cluster:identifier   (Aurora cluster)
    rtype, rid = _split_resource(resource_part)
    if not rid:
        return UNKNOWN
    if rtype == "db":
        return ("AWS/RDS", [_dim("DBInstanceIdentifier", rid)], {})
    if rtype == "cluster":
        return ("AWS/RDS", [_dim("DBClusterIdentifier", rid)], {})
    return UNKNOWN


def _parse_dynamodb(parts, resource_part):
    # arn:aws:dynamodb:region:account:table/name
    rtype, rid = _split_resource(resource_part)
    if rtype != "table" or not rid:
        return UNKNOWN
    # DynamoDB ARNs may include "/index/IndexName" suffix; keep the table only.
    table = rid.split("/", 1)[0]
    return ("AWS/DynamoDB", [_dim("TableName", table)], {})


def _parse_ec2(parts, resource_part):
    # arn:aws:ec2:region:account:<resource_type>/<resource_id>
    # Several CloudWatch namespaces sit under the ec2 service prefix.
    rtype, rid = _split_resource(resource_part)
    if not rtype or not rid:
        return UNKNOWN
    if rtype == "instance":
        return ("AWS/EC2", [_dim("InstanceId", rid)], {})
    if rtype == "volume":
        return ("AWS/EBS", [_dim("VolumeId", rid)], {})
    if rtype == "natgateway":
        return ("AWS/NATGateway", [_dim("NatGatewayId", rid)], {})
    if rtype == "vpn-connection":
        return ("AWS/VPN", [_dim("VpnId", rid)], {})
    if rtype == "transit-gateway":
        return ("AWS/TransitGateway", [_dim("TransitGateway", rid)], {})
    return UNKNOWN


def _parse_ecs(parts, resource_part):
    # arn:aws:ecs:region:account:cluster/name              -> ClusterName
    # arn:aws:ecs:region:account:service/cluster/svc       -> ClusterName + ServiceName
    rtype, rid = _split_resource(resource_part)
    if not rtype or not rid:
        return UNKNOWN
    if rtype == "cluster":
        return ("AWS/ECS", [_dim("ClusterName", rid)], {})
    if rtype == "service":
        # rid is "cluster/svc"; some accounts use the long form
        # "cluster-name/service-name".
        cluster, _, svc = rid.partition("/")
        if not cluster or not svc:
            return UNKNOWN
        return (
            "AWS/ECS",
            [_dim("ClusterName", cluster), _dim("ServiceName", svc)],
            {},
        )
    return UNKNOWN


def _parse_eks(parts, resource_part):
    # arn:aws:eks:region:account:cluster/name
    rtype, rid = _split_resource(resource_part)
    if rtype != "cluster" or not rid:
        return UNKNOWN
    return ("AWS/EKS", [_dim("ClusterName", rid)], {})


def _parse_elasticache(parts, resource_part):
    # arn:aws:elasticache:region:account:cluster:my-cache
    # (also "replicationgroup", "snapshot" etc.; we only handle cluster.)
    rtype, rid = _split_resource(resource_part)
    if rtype != "cluster" or not rid:
        return UNKNOWN
    return ("AWS/ElastiCache", [_dim("CacheClusterId", rid)], {})


def _parse_kinesis(parts, resource_part):
    # arn:aws:kinesis:region:account:stream/name
    rtype, rid = _split_resource(resource_part)
    if rtype != "stream" or not rid:
        return UNKNOWN
    return ("AWS/Kinesis", [_dim("StreamName", rid)], {})


def _parse_sqs(parts, resource_part):
    # arn:aws:sqs:region:account:queue-name  (resource_part IS the queue name)
    if not resource_part or "/" in resource_part or ":" in resource_part:
        # SQS queue ARNs never carry a slash or colon in the resource segment.
        return UNKNOWN
    return ("AWS/SQS", [_dim("QueueName", resource_part)], {})


def _parse_sns(parts, resource_part):
    # arn:aws:sns:region:account:topic-name
    if not resource_part or "/" in resource_part or ":" in resource_part:
        return UNKNOWN
    return ("AWS/SNS", [_dim("TopicName", resource_part)], {})


def _parse_s3(parts, resource_part):
    # arn:aws:s3:::bucket-name (or "bucket-name/prefix"). region/account empty.
    bucket = resource_part.split("/", 1)[0]
    if not bucket:
        return UNKNOWN
    return ("AWS/S3", [_dim("BucketName", bucket)], {})


def _parse_cloudfront(parts, resource_part):
    # arn:aws:cloudfront::account:distribution/id
    rtype, rid = _split_resource(resource_part)
    if rtype != "distribution" or not rid:
        return UNKNOWN
    # CloudFront metrics are always emitted to the "Global" region dimension.
    return (
        "AWS/CloudFront",
        [_dim("DistributionId", rid), _dim("Region", "Global")],
        {},
    )


def _parse_apigateway(parts, resource_part):
    # arn:aws:apigateway:region::/restapis/abc123 (REST API v1)
    # arn:aws:apigateway:region::/apis/abc123     (HTTP / WebSocket API v2)
    # CloudWatch's AWS/ApiGateway namespace uses the ApiName dimension, not the
    # ApiId. Names aren't in the ARN — caller must resolve via boto3 if they
    # need the canonical name. We surface the id with a note.
    stripped = resource_part.lstrip("/")
    if not stripped:
        return UNKNOWN
    rtype, _, rid = stripped.partition("/")
    if rtype not in ("restapis", "apis") or not rid:
        return UNKNOWN
    api_id = rid.split("/", 1)[0]
    return (
        "AWS/ApiGateway",
        [_dim("ApiName", api_id)],
        {"note": "ApiName is not in the ARN; falling back to ApiId"},
    )


def _parse_autoscaling(parts, resource_part):
    # arn:aws:autoscaling:region:account:autoScalingGroup:<uuid>:autoScalingGroupName/<name>
    # The resource_part starts with "autoScalingGroup:" because we split the
    # outer ARN with maxsplit=5.
    if not resource_part.startswith("autoScalingGroup:"):
        return UNKNOWN
    asg_name_marker = "autoScalingGroupName/"
    idx = resource_part.find(asg_name_marker)
    if idx == -1:
        return UNKNOWN
    asg_name = resource_part[idx + len(asg_name_marker):]
    if not asg_name:
        return UNKNOWN
    return ("AWS/AutoScaling", [_dim("AutoScalingGroupName", asg_name)], {})


def _parse_redshift(parts, resource_part):
    # arn:aws:redshift:region:account:cluster:my-cluster
    rtype, rid = _split_resource(resource_part)
    if rtype != "cluster" or not rid:
        return UNKNOWN
    return ("AWS/Redshift", [_dim("ClusterIdentifier", rid)], {})


def _parse_redshift_serverless(parts, resource_part):
    # arn:aws:redshift-serverless:region:account:workgroup/<uuid>
    rtype, rid = _split_resource(resource_part)
    if rtype != "workgroup" or not rid:
        return UNKNOWN
    return ("AWS/Redshift-Serverless", [_dim("Workgroup", rid)], {})


def _parse_acm(parts, resource_part):
    # arn:aws:acm:region:account:certificate/<uuid>
    rtype, _ = _split_resource(resource_part)
    if rtype != "certificate":
        return UNKNOWN
    full_arn = ":".join(parts[:5] + [resource_part])
    return ("AWS/CertificateManager", [_dim("CertificateArn", full_arn)], {})


def _parse_efs(parts, resource_part):
    # arn:aws:elasticfilesystem:region:account:file-system/fs-12345
    rtype, rid = _split_resource(resource_part)
    if rtype != "file-system" or not rid:
        return UNKNOWN
    return ("AWS/EFS", [_dim("FileSystemId", rid)], {})


def _parse_cognito(parts, resource_part):
    # arn:aws:cognito-idp:region:account:userpool/<pool-id>
    rtype, rid = _split_resource(resource_part)
    if rtype != "userpool" or not rid:
        return UNKNOWN
    return ("AWS/Cognito", [_dim("UserPool", rid)], {})


def _parse_route53(parts, resource_part):
    # arn:aws:route53:::healthcheck/<id>
    # Other Route53 resource types (hostedzone, etc.) don't publish CW metrics.
    rtype, rid = _split_resource(resource_part)
    if rtype != "healthcheck" or not rid:
        return UNKNOWN
    return ("AWS/Route53", [_dim("HealthCheckId", rid)], {})


def _parse_elb(parts, resource_part):
    # arn:aws:elasticloadbalancing:region:account:loadbalancer/app/name/uuid  (ALB)
    # arn:aws:elasticloadbalancing:region:account:loadbalancer/net/name/uuid  (NLB)
    # arn:aws:elasticloadbalancing:region:account:loadbalancer/name           (Classic)
    rtype, rid = _split_resource(resource_part)
    if rtype != "loadbalancer" or not rid:
        return UNKNOWN
    if rid.startswith("app/"):
        return ("AWS/ApplicationELB", [_dim("LoadBalancer", rid)], {})
    if rid.startswith("net/"):
        return ("AWS/NetworkELB", [_dim("LoadBalancer", rid)], {})
    # Classic ELB: dimension is the bare name, namespace is AWS/ELB.
    return ("AWS/ELB", [_dim("LoadBalancerName", rid)], {})


def _parse_kafka(parts, resource_part):
    # arn:aws:kafka:region:account:cluster/my-cluster/<uuid>
    # CW dimension name has a literal SPACE: "Cluster Name". Verified against
    # the AWS/Kafka dimensions doc — it's a quirk of the Kafka namespace.
    rtype, rid = _split_resource(resource_part)
    if rtype != "cluster" or not rid:
        return UNKNOWN
    cluster_name = rid.split("/", 1)[0]
    if not cluster_name:
        return UNKNOWN
    return ("AWS/Kafka", [_dim("Cluster Name", cluster_name)], {})


def _parse_states(parts, resource_part):
    # arn:aws:states:region:account:stateMachine:my-sm
    rtype, rid = _split_resource(resource_part)
    if rtype != "stateMachine" or not rid:
        return UNKNOWN
    full_arn = ":".join(parts[:5] + [resource_part])
    return ("AWS/States", [_dim("StateMachineArn", full_arn)], {})


def _parse_es(parts, resource_part):
    # arn:aws:es:region:account:domain/my-domain
    # CloudWatch AWS/ES dimensions are ClientId (account) + DomainName.
    rtype, rid = _split_resource(resource_part)
    if rtype != "domain" or not rid:
        return UNKNOWN
    domain = rid.split("/", 1)[0]
    account = parts[4]
    if not domain or not account:
        return UNKNOWN
    return (
        "AWS/ES",
        [_dim("DomainName", domain), _dim("ClientId", account)],
        {},
    )


# Service prefix → parser. Keep this sorted alphabetically.
_PARSERS: dict[str, Callable[[list[str], str], tuple[str | None, list[dict], dict]]] = {
    "acm": _parse_acm,
    "apigateway": _parse_apigateway,
    "autoscaling": _parse_autoscaling,
    "cloudfront": _parse_cloudfront,
    "cognito-idp": _parse_cognito,
    "dynamodb": _parse_dynamodb,
    "ec2": _parse_ec2,
    "ecs": _parse_ecs,
    "eks": _parse_eks,
    "elasticache": _parse_elasticache,
    "elasticfilesystem": _parse_efs,
    "elasticloadbalancing": _parse_elb,
    "es": _parse_es,
    "kafka": _parse_kafka,
    "kinesis": _parse_kinesis,
    "lambda": _parse_lambda,
    "rds": _parse_rds,
    "redshift": _parse_redshift,
    "redshift-serverless": _parse_redshift_serverless,
    "route53": _parse_route53,
    "s3": _parse_s3,
    "sns": _parse_sns,
    "sqs": _parse_sqs,
    "states": _parse_states,
}
