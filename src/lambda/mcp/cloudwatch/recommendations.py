"""Compatibility export for the shared recommendation catalogue."""

from shared.cloudwatch_domain import recommendations as _module

globals().update(
    {name: value for name, value in vars(_module).items() if not name.startswith("__")}
)
