"""Shared test fixtures for CloudOps Multi-Agent System."""

import os

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def aws_credentials():
    """Set mock AWS credentials for moto."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    yield
    for key in [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_REGION",
    ]:
        os.environ.pop(key, None)


@pytest.fixture
def mock_aws_env(aws_credentials):
    """Provide a fully mocked AWS environment via moto."""
    with mock_aws():
        yield


@pytest.fixture
def dynamodb_resource(mock_aws_env):
    """Provide a mocked DynamoDB resource."""
    return boto3.resource("dynamodb", region_name="us-east-1")


@pytest.fixture
def agent_registry_table(dynamodb_resource):
    """Create and return the agent-registry DynamoDB table."""
    table = dynamodb_resource.create_table(
        TableName="cloudops-agent-registry",
        KeySchema=[
            {"AttributeName": "agent_name", "KeyType": "HASH"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "agent_name", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    return table
