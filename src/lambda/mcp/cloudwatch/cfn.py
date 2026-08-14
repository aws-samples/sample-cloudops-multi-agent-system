"""Compatibility export for the shared CloudWatch CFN assembler."""

from shared.cloudwatch_domain import cfn as _module

globals().update(
    {name: value for name, value in vars(_module).items() if not name.startswith("__")}
)
