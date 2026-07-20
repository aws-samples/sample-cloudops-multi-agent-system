"""Layer 2 — slice + build-script + terraform-validate for each topology.

Opt-in: `pytest -m topology` or `pytest tests/topology/`.

What's new vs Layer 1:
  * Exercises the ACTUAL bash slicer (`scripts/lib/build.sh::
    _write_agent_hierarchy_slice`), not a Python re-implementation.
  * Exercises the ACTUAL frontend-detection bash helper
    (`scripts/lib/hierarchy.sh::_load_hierarchy`). Confirms that if
    `type: "frontend"` moves to a different agent, `FRONTEND_AGENT`
    follows. A regression here silently deploys the wrong AGUI agent.
  * Runs `terraform validate` — catches module/variable/reference
    errors without needing AWS creds.
  * Does all of the above against three topology shapes: full
    hierarchy, orchestrator-as-frontend, single-leaf-as-frontend.

Does NOT deploy, plan, or build containers. Runtime: ~5–15s.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.topology

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HIERARCHY_PATH = _REPO_ROOT / "src" / "agents" / "hierarchy.json"
_TERRAFORM_DIR = _REPO_ROOT / "terraform"

# ---------------------------------------------------------------------------
# Test-support helpers — stage a mutated hierarchy.json into the repo,
# run the script, then restore the original. Done via a context manager
# so a test failure mid-mutation still restores the file.
# ---------------------------------------------------------------------------


class _HierarchyOverride:
    """Context manager that temporarily replaces hierarchy.json with a
    mutated copy, then restores the original on exit.

    Safe against test-failure abort: __exit__ runs via finally.
    """

    def __init__(self, mutated: dict):
        self.mutated = mutated
        self._backup_path = _HIERARCHY_PATH.with_suffix(".json.test-backup")

    def __enter__(self):
        shutil.copy2(_HIERARCHY_PATH, self._backup_path)
        with open(_HIERARCHY_PATH, "w") as f:
            json.dump(self.mutated, f, indent=4, sort_keys=True)
        return self

    def __exit__(self, exc_type, exc, tb):
        shutil.copy2(self._backup_path, _HIERARCHY_PATH)
        self._backup_path.unlink()


@pytest.fixture(scope="module")
def hierarchy() -> dict:
    with open(_HIERARCHY_PATH) as f:
        return json.load(f)


def _promote_to_frontend(hierarchy: dict, target: str) -> dict:
    """Return a mutated copy where `target` is promoted to frontend.

    Mirrors the helper in tests/unit/test_topology.py — kept separate
    to avoid cross-file imports that break pytest discovery. If this
    diverges, the Layer 1 test `_promote_to_frontend` is the reference.
    """
    mutated = copy.deepcopy(hierarchy)
    for name, entry in mutated.items():
        if entry["type"] == "frontend" and name != target:
            entry["type"] = (
                "orchestrator" if entry.get("children") else "worker"
            )
            entry["dir"] = (
                "agents/orchestrator" if entry["type"] == "orchestrator"
                else "agents/worker"
            )
            for flag in ("memory", "suggestions", "reports"):
                entry.pop(flag, None)
    mutated[target]["type"] = "frontend"
    mutated[target]["dir"] = "agents/frontend"
    reachable = {target}
    changed = True
    while changed:
        changed = False
        for name in list(reachable):
            for child in mutated[name].get("children", []):
                if child in mutated and child not in reachable:
                    reachable.add(child)
                    changed = True
    return {k: v for k, v in mutated.items() if k in reachable}


# Topology shapes under test. Name → (promotion target, expected
# frontend-agent name after promotion).
_TOPOLOGIES: dict[str, str] = {
    "full": "supervisor",
    "orchestrator_as_frontend": "finops-agent",
    "solo_leaf_as_frontend": "cost-operations-agent",
}


def _build_topology(hierarchy: dict, shape: str) -> dict:
    """Return a mutated hierarchy for one of the three supported shapes.

    For the `full` shape we return the original unchanged — the test
    still runs against it, which guards against false positives in the
    other two shapes (if the full shape fails we know the test itself
    is broken, not the mutation logic).
    """
    if shape == "full":
        return hierarchy
    return _promote_to_frontend(hierarchy, _TOPOLOGIES[shape])


# ---------------------------------------------------------------------------
# 1. Bash slicer — the deploy script that actually writes the per-agent slice
# ---------------------------------------------------------------------------


def _run_bash_slicer(agent_name: str) -> Path:
    """Invoke `_write_agent_hierarchy_slice` from scripts/lib/build.sh
    and return the path to the resulting slice file.
    """
    slice_path = _REPO_ROOT / "src" / "agents" / f".hierarchy-{agent_name}.json"
    # Source build.sh and call the function. `.venv/bin/python` is the
    # interpreter the slicer invokes internally; make sure we run from
    # the repo root so relative paths in the script resolve.
    cmd = (
        "source scripts/lib/build.sh && "
        f"_write_agent_hierarchy_slice {agent_name}"
    )
    result = subprocess.run(
        ["bash", "-c", cmd],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"bash slicer failed for {agent_name}:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return slice_path


@pytest.mark.parametrize("shape", list(_TOPOLOGIES.keys()))
def test_bash_slicer_produces_valid_slice_for_every_agent(hierarchy, shape):
    """For each topology shape, invoke the BASH slicer on every agent
    in that shape's hierarchy and assert the output is valid."""
    mutated = _build_topology(hierarchy, shape)
    slice_files: list[Path] = []
    try:
        with _HierarchyOverride(mutated):
            for agent_name in mutated:
                slice_path = _run_bash_slicer(agent_name)
                slice_files.append(slice_path)
                assert slice_path.exists(), (
                    f"slicer did not write {slice_path}"
                )
                with open(slice_path) as f:
                    slice_data = json.load(f)
                assert list(slice_data.keys()) == [agent_name], (
                    f"bash slicer output for {agent_name} in shape "
                    f"{shape!r} contains wrong keys: "
                    f"{list(slice_data.keys())}"
                )
                assert slice_data[agent_name] == mutated[agent_name], (
                    f"bash slicer mutated {agent_name}'s entry "
                    f"(shape: {shape!r})"
                )
    finally:
        for sf in slice_files:
            sf.unlink(missing_ok=True)


def test_bash_slicer_rejects_unknown_agent():
    """`_write_agent_hierarchy_slice some-nonexistent-agent` must
    exit non-zero. Otherwise a typo in `SELECTED_AGENTS` silently
    builds an empty slice and the container starts with nothing."""
    cmd = (
        "source scripts/lib/build.sh && "
        "_write_agent_hierarchy_slice agent-that-does-not-exist"
    )
    result = subprocess.run(
        ["bash", "-c", cmd],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0, (
        "bash slicer accepted an unknown agent name — it should fail. "
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "not found" in (result.stdout + result.stderr).lower()


# ---------------------------------------------------------------------------
# 2. Frontend-agent detection — the function deploy.sh depends on
# ---------------------------------------------------------------------------


def _detect_frontend_agent() -> str:
    """Invoke `_load_hierarchy` from scripts/lib/hierarchy.sh and
    return the computed `FRONTEND_AGENT` value.
    """
    cmd = (
        "source scripts/lib/hierarchy.sh && "
        "_load_hierarchy && "
        'echo "RESULT:$FRONTEND_AGENT"'
    )
    result = subprocess.run(
        ["bash", "-c", cmd],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"frontend detection failed:\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    for line in result.stdout.splitlines():
        if line.startswith("RESULT:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(
        f"Did not find RESULT line in _load_hierarchy output:\n{result.stdout}"
    )


@pytest.mark.parametrize("shape", list(_TOPOLOGIES.keys()))
def test_frontend_detection_follows_the_frontend_type(hierarchy, shape):
    """After mutating hierarchy.json to promote a different agent to
    frontend, `_load_hierarchy` must report the new agent — NOT the
    hardcoded default `supervisor`.

    A regression here mis-identifies the frontend agent on any non-full
    topology — the wrong agent would get the AGUI runtime protocol and
    frontend-only env vars — which is the exact bug class the topology
    optimisation entry warns about.
    """
    mutated = _build_topology(hierarchy, shape)
    expected_frontend = _TOPOLOGIES[shape]
    with _HierarchyOverride(mutated):
        got = _detect_frontend_agent()
    assert got == expected_frontend, (
        f"shape={shape!r}: _load_hierarchy reported FRONTEND_AGENT="
        f"{got!r}, expected {expected_frontend!r}. The wrong agent would "
        "be treated as the frontend (AGUI protocol + frontend env vars)."
    )


# ---------------------------------------------------------------------------
# 3. Terraform validate — catches syntax + module-reference errors
# ---------------------------------------------------------------------------


def _run_terraform_validate() -> tuple[int, str]:
    """Run `terraform validate -no-color` in `terraform/`. No creds
    needed; doesn't call AWS. Returns (exit_code, combined_output).
    """
    result = subprocess.run(
        ["terraform", "-chdir=terraform", "validate", "-no-color"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            # Suppress any AWS credential chain noise — validate
            # doesn't need them but the provider will emit warnings.
            "AWS_ACCESS_KEY_ID": os.environ.get(
                "AWS_ACCESS_KEY_ID", "validate-only"
            ),
            "AWS_SECRET_ACCESS_KEY": os.environ.get(
                "AWS_SECRET_ACCESS_KEY", "validate-only"
            ),
            "AWS_REGION": os.environ.get("AWS_REGION", "us-east-1"),
        },
    )
    return result.returncode, result.stdout + result.stderr


@pytest.fixture(scope="session")
def terraform_initialised():
    """terraform validate requires `.terraform/` to exist. On a fresh
    clone this won't be there. We skip the module if we can't find it
    — real CI should have run `make deploy-auto` once first, which
    primes it.
    """
    if not (_TERRAFORM_DIR / ".terraform").exists():
        pytest.skip(
            "terraform/.terraform not found. Run `make deploy-auto` once "
            "(or `terraform -chdir=terraform init`) before running "
            "tests/topology/."
        )


def test_terraform_validate_passes_on_current_hierarchy(
    terraform_initialised,
):
    """Baseline: unmutated terraform/ must validate. If this fails the
    other validate tests would also fail, but for unrelated reasons."""
    rc, output = _run_terraform_validate()
    assert rc == 0, f"terraform validate failed on baseline:\n{output}"


@pytest.mark.parametrize("shape", list(_TOPOLOGIES.keys()))
def test_terraform_validate_passes_for_each_topology(
    terraform_initialised, hierarchy, shape
):
    """Mutate hierarchy.json → run `terraform validate` → assert success.

    This catches Terraform-level breakage introduced by unusual topology
    shapes: a missing module input, a reference to the (possibly-pruned)
    supervisor runtime from an orchestrator topology, etc.
    """
    mutated = _build_topology(hierarchy, shape)
    with _HierarchyOverride(mutated):
        rc, output = _run_terraform_validate()
    assert rc == 0, (
        f"terraform validate failed for topology shape {shape!r}:\n"
        f"{output}\n"
        "This means the Terraform code makes an assumption that breaks "
        "when the hierarchy is reshaped — e.g. a hardcoded reference "
        "to `supervisor` that won't resolve in a promoted-leaf topology."
    )


# ---------------------------------------------------------------------------
# 4. Dockerfile + Terraform consistency — every agent `dir` has a Dockerfile
# ---------------------------------------------------------------------------


def test_every_agent_dir_has_a_dockerfile(hierarchy):
    """If `dir` points at `agents/worker`, there must be a Dockerfile
    there. Layer 1 already checks server.py exists — this adds the
    Dockerfile guard because the build step will fail without it."""
    for name, entry in hierarchy.items():
        dockerfile = _REPO_ROOT / "src" / entry["dir"] / "Dockerfile"
        assert dockerfile.exists(), (
            f"agent {name!r} dir={entry['dir']} has no Dockerfile at "
            f"{dockerfile}. `make build-agents` will fail."
        )


def test_dockerfile_references_agent_hierarchy_path(hierarchy):
    """Every agent Dockerfile must accept an `AGENT_HIERARCHY_PATH`
    build-arg (see docs/development.md — per-agent slicing depends on it).

    Catches a regression where someone edits a Dockerfile and removes
    the build-arg; the build would then fall back to copying the full
    hierarchy.json, invalidating the per-agent hash optimisation
    silently."""
    agent_dirs_checked = set()
    for entry in hierarchy.values():
        dir_ = entry["dir"]
        if dir_ in agent_dirs_checked:
            continue
        agent_dirs_checked.add(dir_)
        dockerfile = _REPO_ROOT / "src" / dir_ / "Dockerfile"
        content = dockerfile.read_text()
        assert "AGENT_HIERARCHY_PATH" in content, (
            f"{dockerfile} does not reference AGENT_HIERARCHY_PATH. "
            "Per-agent slicing is broken for any agent using this "
            "Dockerfile — the container will get the full "
            "hierarchy.json, which flips the hash on every unrelated "
            "edit and defeats the build cache."
        )
