"""Shared setup for the unit-test package.

Two things are centralized here so individual test modules don't each
re-implement them (which is how conventions drift — see the sys.modules
pollution that once lived in test_lambda_runtime_tool.py):

1. Deterministic AWS env for import time. Several Lambda handlers read
   AWS_REGION (and clients like MemoryClient default to us-west-2 if it is
   unset — see CLAUDE.md), and they are imported at test-collection time via
   importlib. conftest.py is imported BEFORE the test modules in its
   directory, so setting these at module scope here guarantees they are in
   place before any handler import. Dummy credentials ensure a stray boto3
   call can never reach a real account from the unit suite.

2. Automatic `unit` marker. Every test under tests/unit/ is a unit test, so
   rather than repeat `pytestmark = pytest.mark.unit` in 15 files (only one
   did), we tag them all here. This makes `pytest -m unit` actually select
   the whole unit suite.
"""

import os

import pytest

# --- 1. Deterministic AWS env (module scope → runs at conftest import,
#        before test modules import their handlers) --------------------------
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
# Dummy creds so no unit test can accidentally hit a real account. setdefault
# means a developer who has real creds exported still gets them overridden
# only if unset — but the unit suite never makes live calls, so this is a
# backstop, not a dependency.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")


# --- 2. Auto-apply the `unit` marker to everything in this package ----------
def pytest_collection_modifyitems(config, items):
    """Tag every test collected under tests/unit/ with the `unit` marker,
    so `pytest -m unit` selects the full suite without each file having to
    declare `pytestmark = pytest.mark.unit`.
    """
    unit_root = os.path.dirname(__file__)
    for item in items:
        if str(item.fspath).startswith(unit_root):
            item.add_marker(pytest.mark.unit)
