"""Unit tests for src/lambda/mcp/shared/cross_account.py."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# shared/ lives under src/lambda/mcp/ (not on the default sys.path), so pull
# it in directly for the tests.
_SHARED_DIR = Path(__file__).resolve().parents[2] / "src" / "lambda" / "mcp"
sys.path.insert(0, str(_SHARED_DIR))

from shared import cross_account as ca  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # Clear both LRU caches between tests so env-var changes take effect.
    ca._reset_caches_for_testing()
    for k in list(
        v for v in [
            "CROSS_ACCOUNT_ROLE_ARN",
            "CROSS_ACCOUNT_ROLE_ARN_CE",
            "CROSS_ACCOUNT_ROLE_ARN_COH",
            "CROSS_ACCOUNT_ROLE_ARN_HEALTH",
            "CROSS_ACCOUNT_EXTERNAL_ID",
            "CROSS_ACCOUNT_EXTERNAL_ID_CE",
        ]
        if v in __import__("os").environ
    ):
        monkeypatch.delenv(k, raising=False)
    yield
    ca._reset_caches_for_testing()


def _mock_sts_ok():
    sts = MagicMock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "AK",
            "SecretAccessKey": "SK",
            "SessionToken": "TK",
        }
    }
    return sts


class TestStaticTargets:
    def test_no_env_returns_none(self):
        """Default behaviour: no role ARN configured = use execution role."""
        assert ca.get_cross_account_session() is None

    def test_default_role_arn_assumes(self, monkeypatch):
        monkeypatch.setenv(
            "CROSS_ACCOUNT_ROLE_ARN", "arn:aws:iam::111111111111:role/CEReadOnly"
        )
        sts = _mock_sts_ok()
        with patch("shared.cross_account.boto3.client", return_value=sts):
            session = ca.get_cross_account_session()

        assert session is not None
        args = sts.assume_role.call_args.kwargs
        assert args["RoleArn"] == "arn:aws:iam::111111111111:role/CEReadOnly"
        assert args["RoleSessionName"] == "mcp-gateway-cross-account"
        assert args["DurationSeconds"] == 900

    def test_aliased_role_arn(self, monkeypatch):
        """Scenario 1 multi-target: CROSS_ACCOUNT_ROLE_ARN_COH is separate
        from the default role and keyed independently in the cache."""
        monkeypatch.setenv(
            "CROSS_ACCOUNT_ROLE_ARN_COH", "arn:aws:iam::222:role/COHReadOnly"
        )
        monkeypatch.setenv(
            "CROSS_ACCOUNT_ROLE_ARN_CE", "arn:aws:iam::111:role/CEReadOnly"
        )
        sts = _mock_sts_ok()
        with patch("shared.cross_account.boto3.client", return_value=sts):
            ca.get_cross_account_session(role_alias="COH")
            ca.get_cross_account_session(role_alias="CE")

        # Two distinct ARNs → two AssumeRole calls.
        arns = {c.kwargs["RoleArn"] for c in sts.assume_role.call_args_list}
        assert arns == {
            "arn:aws:iam::222:role/COHReadOnly",
            "arn:aws:iam::111:role/CEReadOnly",
        }
        session_names = {
            c.kwargs["RoleSessionName"] for c in sts.assume_role.call_args_list
        }
        assert session_names == {"mcp-coh", "mcp-ce"}

    def test_repeat_call_is_cached(self, monkeypatch):
        """lru_cache means two calls with the same ARN → one STS call."""
        monkeypatch.setenv(
            "CROSS_ACCOUNT_ROLE_ARN", "arn:aws:iam::111:role/X"
        )
        sts = _mock_sts_ok()
        with patch("shared.cross_account.boto3.client", return_value=sts):
            ca.get_cross_account_session()
            ca.get_cross_account_session()
            ca.get_cross_account_session()

        assert sts.assume_role.call_count == 1

    def test_external_id_passed_through(self, monkeypatch):
        monkeypatch.setenv(
            "CROSS_ACCOUNT_ROLE_ARN", "arn:aws:iam::111:role/X"
        )
        monkeypatch.setenv("CROSS_ACCOUNT_EXTERNAL_ID", "secret-extid")
        sts = _mock_sts_ok()
        with patch("shared.cross_account.boto3.client", return_value=sts):
            ca.get_cross_account_session()

        assert sts.assume_role.call_args.kwargs["ExternalId"] == "secret-extid"

    def test_assume_role_failure_returns_none(self, monkeypatch):
        monkeypatch.setenv(
            "CROSS_ACCOUNT_ROLE_ARN", "arn:aws:iam::111:role/X"
        )
        sts = MagicMock()
        sts.assume_role.side_effect = RuntimeError("AccessDenied")
        with patch("shared.cross_account.boto3.client", return_value=sts):
            session = ca.get_cross_account_session()
        assert session is None


class TestGetAwsClient:
    def test_no_role_returns_execution_role_client(self):
        """get_aws_client without env config falls through to boto3.client."""
        with patch("shared.cross_account.boto3.client") as mock_boto_client:
            mock_boto_client.return_value = "EXEC_CLIENT"
            client = ca.get_aws_client("ce")
        assert client == "EXEC_CLIENT"
        mock_boto_client.assert_called_once_with("ce")

    def test_region_passed_through(self):
        with patch("shared.cross_account.boto3.client") as mock_boto_client:
            ca.get_aws_client("cost-optimization-hub", region_name="us-east-1")
        mock_boto_client.assert_called_once_with(
            "cost-optimization-hub", region_name="us-east-1"
        )

    def test_returns_assumed_role_client_when_configured(self, monkeypatch):
        monkeypatch.setenv(
            "CROSS_ACCOUNT_ROLE_ARN", "arn:aws:iam::111:role/X"
        )
        sts = _mock_sts_ok()
        session_client = MagicMock(return_value="ASSUMED_CLIENT")

        class FakeSession:
            def __init__(self, **_):
                self.client = session_client

        with patch("shared.cross_account.boto3.client", return_value=sts), patch(
            "shared.cross_account.boto3.Session", FakeSession
        ):
            client = ca.get_aws_client("ce")

        assert client == "ASSUMED_CLIENT"
        session_client.assert_called_once_with("ce")


class TestDynamicPerAccount:
    def test_builds_arn_from_account_and_role_name(self):
        sts = _mock_sts_ok()
        with patch("shared.cross_account.boto3.client", return_value=sts):
            session = ca.assume_role_for_account(
                "999999999999", "NetworkReadOnlyRole"
            )

        assert session is not None
        args = sts.assume_role.call_args.kwargs
        assert args["RoleArn"] == "arn:aws:iam::999999999999:role/NetworkReadOnlyRole"
        assert args["RoleSessionName"] == "mcp-xacct-999999999999"

    def test_service_shortcut_returns_client(self):
        sts = _mock_sts_ok()
        session_client = MagicMock(return_value="EC2_CLIENT")

        class FakeSession:
            def __init__(self, **_):
                self.client = session_client

        with patch("shared.cross_account.boto3.client", return_value=sts), patch(
            "shared.cross_account.boto3.Session", FakeSession
        ):
            client = ca.assume_role_for_account(
                "999", "NetworkReadOnlyRole", service="ec2", region_name="eu-west-1"
            )

        assert client == "EC2_CLIENT"
        session_client.assert_called_once_with("ec2", region_name="eu-west-1")

    def test_different_accounts_cached_separately(self):
        sts = _mock_sts_ok()
        with patch("shared.cross_account.boto3.client", return_value=sts):
            ca.assume_role_for_account("111111111111", "Role")
            ca.assume_role_for_account("222222222222", "Role")
            ca.assume_role_for_account("111111111111", "Role")  # repeat → cached

        # Two unique accounts → exactly two AssumeRole calls.
        assert sts.assume_role.call_count == 2

    def test_assume_role_failure_returns_none(self):
        sts = MagicMock()
        sts.assume_role.side_effect = RuntimeError("no trust policy")
        with patch("shared.cross_account.boto3.client", return_value=sts):
            result = ca.assume_role_for_account("123", "Missing")
        assert result is None


class TestSessionLifetime:
    """Warm containers outlive 15-minute credentials; failures must not stick."""

    def _sts(self, expiration):
        sts = MagicMock()
        sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "TK",
                "Expiration": expiration,
            }
        }
        return sts

    def test_a_session_near_expiry_is_reassumed(self, monkeypatch):
        from datetime import datetime, timedelta, timezone

        monkeypatch.setenv("CROSS_ACCOUNT_ROLE_ARN", "arn:aws:iam::111:role/X")
        soon = datetime.now(timezone.utc) + timedelta(seconds=60)
        sts = self._sts(soon)
        with patch("shared.cross_account.boto3.client", return_value=sts):
            ca.get_cross_account_session()
            ca.get_cross_account_session()
        assert sts.assume_role.call_count == 2

    def test_a_fresh_session_is_reused(self, monkeypatch):
        from datetime import datetime, timedelta, timezone

        monkeypatch.setenv("CROSS_ACCOUNT_ROLE_ARN", "arn:aws:iam::111:role/X")
        later = datetime.now(timezone.utc) + timedelta(minutes=15)
        sts = self._sts(later)
        with patch("shared.cross_account.boto3.client", return_value=sts):
            first = ca.get_cross_account_session()
            second = ca.get_cross_account_session()
        assert first is second
        assert sts.assume_role.call_count == 1

    def test_a_failed_assume_role_is_retried_not_cached(self, monkeypatch):
        """lru_cache once pinned a transient STS failure for the container's life."""
        monkeypatch.setenv("CROSS_ACCOUNT_ROLE_ARN", "arn:aws:iam::111:role/X")
        sts = MagicMock()
        sts.assume_role.side_effect = [
            RuntimeError("Throttling"),
            {"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "TK"}},
        ]
        with patch("shared.cross_account.boto3.client", return_value=sts):
            assert ca.get_cross_account_session() is None
            assert ca.get_cross_account_session() is not None
        assert sts.assume_role.call_count == 2


class TestRequireRole:
    def test_configured_but_unassumable_role_raises_when_required(self, monkeypatch):
        monkeypatch.setenv("CROSS_ACCOUNT_ROLE_ARN", "arn:aws:iam::111:role/X")
        sts = MagicMock()
        sts.assume_role.side_effect = RuntimeError("AccessDenied")
        with patch("shared.cross_account.boto3.client", return_value=sts):
            with pytest.raises(ca.CrossAccountRoleError, match="refusing to fall back"):
                ca.get_aws_client("ce", require_role=True)

    def test_default_keeps_the_historical_fallback(self, monkeypatch):
        monkeypatch.setenv("CROSS_ACCOUNT_ROLE_ARN", "arn:aws:iam::111:role/X")
        sts = MagicMock()
        sts.assume_role.side_effect = RuntimeError("AccessDenied")
        with patch("shared.cross_account.boto3.client", return_value=sts):
            assert ca.get_aws_client("ce") is sts

    def test_no_role_configured_is_not_an_error_even_when_required(self):
        with patch("shared.cross_account.boto3.client") as mock_client:
            mock_client.return_value = "EXEC"
            assert ca.get_aws_client("ce", require_role=True) == "EXEC"
