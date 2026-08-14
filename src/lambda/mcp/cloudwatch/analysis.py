"""Compatibility export for the shared CloudWatch analysis module."""

from shared.cloudwatch_domain import analysis as _module

globals().update(
    {name: value for name, value in vars(_module).items() if not name.startswith("__")}
)
