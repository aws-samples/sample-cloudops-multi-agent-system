"""Compatibility export for the shared CloudWatch ARN parser."""

from shared.cloudwatch_domain import arn as _module

globals().update(
    {name: value for name, value in vars(_module).items() if not name.startswith("__")}
)
