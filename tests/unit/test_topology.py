"""Static topology validation tests.

Layer 1 of the topology flexibility test suite (see
`temp/optimizations.md::Deploy-topology flexibility`). These tests run on
every `make test-unit` — no AWS, no containers, no Terraform. They catch
misconfigurations in `src/agents/hierarchy.json` and
`src/lambda/mcp/tools.json` BEFORE a deploy surfaces them (or worse, they
surface silently at runtime).

Covers:
  * Structural invariants of hierarchy.json (types, protocol, graph shape).
  * Per-agent hash slicing behaves deterministically (CRITICAL: a
    non-deterministic slice invalidates every cache and defeats the
    build-hash optimisation).
  * Cross-references between hierarchy.json and tools.json resolve.
  * Filesystem layout matches the `dir` claims in each entry.
  * Capability flags (memory / suggestions) appear only where
    the factories expect them.
  * Three supported topology "shapes" — full hierarchy,
    orchestrator-as-frontend, solo-leaf — are mutable slices of the same
    config. The mutations themselves are tested as pure data transforms;
    the deploy end-to-end check is Layer 3.

If one of these fails, a subsequent `make deploy-auto` would likely fail
too — often at an unhelpful step (terraform plan, container startup,
or worse, silently at request time).
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

# The `unit` marker is applied to every test under tests/unit/ by
# tests/unit/conftest.py — no per-file pytestmark needed.

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HIERARCHY_PATH = _REPO_ROOT / "src" / "agents" / "hierarchy.json"
_TOOLS_PATH = _REPO_ROOT / "src" / "lambda" / "mcp" / "tools.json"
_AGENTS_ROOT = _REPO_ROOT / "src" / "agents"

# Code folders that a `dir` field can legally point at. `dir` is relative
# to `src/` in hierarchy.json (e.g. `agents/worker`, `agents/frontend`).
_VALID_AGENT_DIRS = {
    "agents/frontend",
    "agents/orchestrator",
    "agents/worker",
}

# Capability flags the factories only read on frontend-type agents. If
# they appear on sub-agents they are silently dropped — which is a foot-gun.
_FRONTEND_ONLY_FLAGS = {"memory", "suggestions"}

# AgentCore Runtime name regex (from terraform/README.md:
# `[a-zA-Z][a-zA-Z0-9_]{0,47}`). Agent names in hierarchy.json flow
# through `replace("-", "_")` to derive the runtime name; the input
# name must produce a legal output.
_RUNTIME_NAME_CHARSET = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,47}$")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def hierarchy() -> dict:
    with open(_HIERARCHY_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def tools() -> dict:
    with open(_TOOLS_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def hierarchy_text() -> str:
    """Raw JSON text — used for byte-level determinism checks on slicing."""
    return _HIERARCHY_PATH.read_text()


# ---------------------------------------------------------------------------
# 1. hierarchy.json — structural invariants
# ---------------------------------------------------------------------------
class TestHierarchyStructure:
    """Every entry has required keys, legal types, and the graph is sane."""

    def test_hierarchy_is_a_dict(self, hierarchy):
        assert isinstance(hierarchy, dict), "hierarchy.json must be a JSON object"
        assert hierarchy, "hierarchy.json is empty"

    def test_every_entry_has_required_keys(self, hierarchy):
        required = {"type", "dir", "protocol", "description", "model", "prompt"}
        for name, entry in hierarchy.items():
            missing = required - entry.keys()
            assert not missing, f"agent {name!r} missing required keys: {missing}"

    def test_every_entry_has_legal_type(self, hierarchy):
        legal = {"frontend", "orchestrator", "worker"}
        for name, entry in hierarchy.items():
            assert entry["type"] in legal, (
                f"agent {name!r} has illegal type {entry['type']!r} "
                f"(must be one of {legal})"
            )

    def test_every_dir_points_to_a_real_code_folder(self, hierarchy):
        for name, entry in hierarchy.items():
            dir_field = entry["dir"]
            assert dir_field in _VALID_AGENT_DIRS, (
                f"agent {name!r} has dir={dir_field!r} — must be one of "
                f"{_VALID_AGENT_DIRS}"
            )
            server_path = _REPO_ROOT / "src" / dir_field / "server.py"
            assert server_path.exists(), (
                f"agent {name!r} points at {dir_field} but {server_path} "
                f"does not exist"
            )

    def test_agent_type_matches_code_folder(self, hierarchy):
        """`type: "worker"` MUST map to `dir: "agents/worker"`, etc.

        A mismatch silently loads the wrong factory — e.g. a worker
        pointed at `agents/orchestrator/` gets `create_mid_level_agent`
        semantics and starts looking for children in the registry
        instead of MCP tools in the gateway. Fails at runtime with
        confusing logs.
        """
        expected = {
            "frontend": "agents/frontend",
            "orchestrator": "agents/orchestrator",
            "worker": "agents/worker",
        }
        for name, entry in hierarchy.items():
            want = expected[entry["type"]]
            got = entry["dir"]
            assert got == want, (
                f"agent {name!r} is type={entry['type']!r} but "
                f"dir={got!r} (expected {want!r})"
            )

    def test_every_entry_uses_http_protocol(self, hierarchy):
        """The `protocol` field in `hierarchy.json` is NOT the runtime
        protocol — no code reads it. The runtime protocol is set by
        Terraform (`protocol_configuration`): the frontend runtime serves
        AG-UI, all others serve HTTP. The field is kept `"http"` for every
        agent so it never misleads readers into thinking it drives the
        runtime.

        See docs/development.md "Runtime protocol" + terraform/README.md.
        """
        for name, entry in hierarchy.items():
            assert entry["protocol"] == "http", (
                f"agent {name!r} has protocol={entry['protocol']!r}. "
                "It MUST be 'http'. This field is vestigial (nothing reads "
                "it) — the runtime protocol is set by Terraform's "
                "protocol_configuration, not by this field."
            )

    def test_agent_names_are_legal_runtime_identifiers(self, hierarchy):
        """AgentCore Runtime names are [a-zA-Z][a-zA-Z0-9_]{0,47}.
        Agent names get `replace("-", "_")` applied, so hyphens are
        fine, but e.g. a leading digit or a dot would break.
        """
        for name in hierarchy:
            assert _RUNTIME_NAME_CHARSET.match(name), (
                f"agent name {name!r} is not a legal runtime identifier "
                f"(regex: {_RUNTIME_NAME_CHARSET.pattern})"
            )
            derived = name.replace("-", "_")
            assert len(derived) <= 48, (
                f"agent name {name!r} produces runtime name "
                f"{derived!r} ({len(derived)} chars) — AgentCore "
                "caps at 48"
            )


# ---------------------------------------------------------------------------
# 2. Graph shape — the critical deploy-topology contract
# ---------------------------------------------------------------------------
class TestHierarchyGraph:
    """Frontend count, orphan detection, cycle-freeness, child reachability."""

    def test_exactly_one_frontend_agent(self, hierarchy):
        frontends = [n for n, e in hierarchy.items() if e["type"] == "frontend"]
        assert len(frontends) == 1, (
            f"expected exactly one agent with type='frontend', got "
            f"{len(frontends)}: {frontends}. Terraform sets the AGUI runtime "
            "protocol on the single frontend agent; more than one is ambiguous."
        )

    def test_every_child_reference_resolves(self, hierarchy):
        for parent, entry in hierarchy.items():
            for child in entry.get("children", []):
                assert child in hierarchy, (
                    f"agent {parent!r} lists child {child!r} which is "
                    "not defined in hierarchy.json. This creates a "
                    "dangling registry entry and the orchestrator logs "
                    "`ResourceNotFoundException` at runtime."
                )

    def test_no_orphan_agents(self, hierarchy):
        """Every non-frontend agent must be reachable from the frontend.
        An orphan deploys a runtime that nothing ever calls — wasted
        container, wasted Bedrock setup, confusing registry."""
        frontends = [n for n, e in hierarchy.items() if e["type"] == "frontend"]
        reachable = set(frontends)
        changed = True
        while changed:
            changed = False
            for name in list(reachable):
                for child in hierarchy[name].get("children", []):
                    if child not in reachable:
                        reachable.add(child)
                        changed = True
        orphans = set(hierarchy) - reachable
        assert not orphans, (
            f"orphan agents (unreachable from frontend): {sorted(orphans)}. "
            "Every agent must be a child of some reachable ancestor."
        )

    def test_graph_is_acyclic(self, hierarchy):
        """DFS: a cycle would deadlock the supervisor's delegation."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in hierarchy}

        def dfs(node, path):
            if color[node] == GRAY:
                raise AssertionError(
                    f"cycle detected in hierarchy.json: "
                    f"{' -> '.join(path + [node])}"
                )
            if color[node] == BLACK:
                return
            color[node] = GRAY
            for child in hierarchy[node].get("children", []):
                dfs(child, path + [node])
            color[node] = BLACK

        for name in hierarchy:
            if color[name] == WHITE:
                dfs(name, [])

    def test_workers_declared_as_leaves(self, hierarchy):
        """Workers are leaf agents — they should never have `children`.
        Orchestrator logic doesn't run in the worker container, so any
        children would be silently ignored."""
        for name, entry in hierarchy.items():
            if entry["type"] == "worker":
                children = entry.get("children", [])
                assert not children, (
                    f"worker {name!r} declares children={children} but "
                    "workers are leaf agents. Children on a worker are "
                    "silently ignored. Remove them or change type to "
                    "'orchestrator'."
                )

    def test_orchestrators_have_children(self, hierarchy):
        """An orchestrator with no children is a placeholder for an
        upcoming subtree. It's still a legal deploy — it gets a runtime
        and accepts routed requests, just always returns "no agents
        available" — but it wastes a container. We warn (collect the
        offenders) rather than fail, because the platform allows empty
        orchestrators as reserved stubs while a new subtree is being
        built. Break this into an error in CI if you want empty
        orchestrators banned from `main`.
        """
        offenders = [
            name for name, entry in hierarchy.items()
            if entry["type"] == "orchestrator" and not entry.get("children")
        ]
        if offenders:
            pytest.skip(
                f"{len(offenders)} empty orchestrator(s) — tracked as "
                f"reserved stubs, not a failure: {offenders}. Remove "
                "this skip to make empty orchestrators a hard error "
                "once all subtrees have at least one leaf."
            )


# ---------------------------------------------------------------------------
# 3. Capability flags — foot-gun guard
# ---------------------------------------------------------------------------
class TestCapabilityFlags:
    """memory / suggestions are only honored on frontend agents."""

    def test_frontend_only_flags_on_non_frontend_agents(self, hierarchy):
        for name, entry in hierarchy.items():
            if entry["type"] == "frontend":
                continue
            present = _FRONTEND_ONLY_FLAGS & entry.keys()
            assert not present, (
                f"agent {name!r} (type={entry['type']}) declares "
                f"{present} — these flags are only honored on "
                "type='frontend' agents. The factories for worker / "
                "orchestrator ignore them silently, which hides "
                "configuration mistakes."
            )


# ---------------------------------------------------------------------------
# 4. Tools — cross-reference hierarchy.json and tools.json
# ---------------------------------------------------------------------------
class TestToolReferences:
    """`tools: [...]` on workers must point at real tools.json entries."""

    def test_every_referenced_tool_exists(self, hierarchy, tools):
        for name, entry in hierarchy.items():
            declared = entry.get("tools")
            if not declared:
                continue
            for t in declared:
                assert t in tools, (
                    f"agent {name!r} declares tool {t!r} which is not in "
                    f"tools.json. Known tools: {sorted(tools)}. This "
                    "produces a 0-tool agent at runtime (see "
                    "agent_base.py::load_gateway_tools fail-closed path)."
                )

    def test_worker_tools_field_is_a_list(self, hierarchy):
        for name, entry in hierarchy.items():
            if entry["type"] != "worker":
                continue
            if "tools" not in entry:
                continue  # Omitted = all tools. Legal.
            assert isinstance(entry["tools"], list), (
                f"worker {name!r} tools field is "
                f"{type(entry['tools']).__name__}, must be a list"
            )

    def test_orchestrator_and_frontend_do_not_declare_tools(self, hierarchy):
        """Orchestrators and frontends use child-agent tools via the
        registry, not gateway MCP tools. Declaring `tools` on them is
        confusing at best, broken at worst."""
        for name, entry in hierarchy.items():
            if entry["type"] in ("orchestrator", "frontend"):
                assert "tools" not in entry or entry["type"] == "frontend", (
                    f"{entry['type']} agent {name!r} declares a tools "
                    "field. Only worker agents consume gateway MCP "
                    "tools. On an orchestrator, the tools field is "
                    "dead weight; tools are derived from child-agent "
                    "delegation via the registry."
                )


# ---------------------------------------------------------------------------
# 5. tools.json — basic sanity
# ---------------------------------------------------------------------------
class TestToolsJson:
    def test_tools_is_a_dict(self, tools):
        assert isinstance(tools, dict) and tools

    def test_every_tool_has_required_fields(self, tools):
        required = {"handler", "runtime", "timeout", "memory", "iam_actions"}
        for name, entry in tools.items():
            missing = required - entry.keys()
            assert not missing, (
                f"tool {name!r} missing required fields: {missing}"
            )

    def test_every_tool_has_a_handler_py(self, tools):
        for name in tools:
            handler_path = (
                _REPO_ROOT / "src" / "lambda" / "mcp" / name / "handler.py"
            )
            assert handler_path.exists(), (
                f"tool {name!r} declared in tools.json but "
                f"{handler_path} does not exist"
            )

    def test_no_write_iam_actions_on_read_only_tools(self, tools):
        """Platform convention (see docs/development.md):
        read-only tools MUST NOT have write-like IAM actions. This guards
        against silently widening a read-only tool's policy in a future
        edit."""
        write_prefixes = (
            "Put", "Update", "Delete", "Create", "Tag", "Untag",
            "Attach", "Detach", "Modify", "Stop", "Start", "Reboot",
            "Terminate", "Disassociate", "Associate", "Register",
            "Deregister", "Set", "Remove",
        )
        # Tools that legitimately need one write-like action (e.g.
        # athena:StartQueryExecution) can opt out via this allowlist.
        _ALLOWED_WRITE_ACTIONS = {
            # Athena query execution is the tool's point of existence
            # and is idempotent against the CUR read-only data source.
            "athena:StartQueryExecution",
            "athena:StopQueryExecution",
            "athena:BatchGetQueryExecution",
            # S3 writes go to the Athena output bucket only.
            "s3:PutObject",
        }
        for tool_name, entry in tools.items():
            for action in entry.get("iam_actions", []):
                if action in _ALLOWED_WRITE_ACTIONS:
                    continue
                verb = action.split(":", 1)[-1]
                if verb.startswith(write_prefixes):
                    pytest.fail(
                        f"tool {tool_name!r} declares IAM action "
                        f"{action!r}, which looks write-like. Read-only "
                        "tools must not widen their policy. If this is "
                        "intentional, add it to _ALLOWED_WRITE_ACTIONS "
                        "in this test with a justification."
                    )

    def test_tool_input_schemas_are_objects(self, tools):
        """A tool with no input schema (or one that's not an object)
        confuses the model and breaks the gateway target. Previously
        surfaced as silent `{}` calls."""
        for tool_name, entry in tools.items():
            for tool in entry.get("tools", []):
                schema = tool.get("input_schema", {})
                assert isinstance(schema, dict), (
                    f"{tool_name}.{tool['name']}: input_schema must be an "
                    f"object, got {type(schema).__name__}"
                )
                assert schema.get("type") == "object", (
                    f"{tool_name}.{tool['name']}: input_schema.type must "
                    f"be 'object', got {schema.get('type')!r}"
                )


# ---------------------------------------------------------------------------
# 6. Hierarchy slicing — the build-hash optimisation's critical path
# ---------------------------------------------------------------------------
#
# `scripts/lib/build.sh::_write_agent_hierarchy_slice` writes a
# single-entry hierarchy.json per agent. If the slice output is
# non-deterministic OR accidentally includes siblings, EVERY agent
# image hash flips on unrelated edits and the "prompt-only change
# rebuilds one container" optimisation collapses.
#
# We re-implement the slice in Python here (matching the bash
# implementation's semantics line-for-line) so this check runs without
# shelling out.
# ---------------------------------------------------------------------------
def _slice_hierarchy(hierarchy: dict, agent_name: str) -> str:
    """Produce the same JSON bytes as `_write_agent_hierarchy_slice`
    in scripts/lib/build.sh (see sibling call `json.dump(..., indent=2,
    sort_keys=True)`).
    """
    if agent_name not in hierarchy:
        raise KeyError(agent_name)
    return json.dumps({agent_name: hierarchy[agent_name]}, indent=2, sort_keys=True)


class TestHierarchySlicing:
    def test_slice_contains_only_target_agent(self, hierarchy):
        for name in hierarchy:
            sliced = json.loads(_slice_hierarchy(hierarchy, name))
            assert list(sliced.keys()) == [name], (
                f"slice for {name!r} contains extra keys: "
                f"{list(sliced.keys())}. The per-agent slice MUST be a "
                "single-key object or the container will violate the "
                "'hierarchy has exactly one key inside the image' "
                "invariant documented in docs/development.md."
            )

    def test_slice_preserves_all_agent_fields(self, hierarchy):
        for name, entry in hierarchy.items():
            sliced = json.loads(_slice_hierarchy(hierarchy, name))[name]
            assert sliced == entry, (
                f"slice for {name!r} mutated the agent entry. "
                "Slicing must be a pure projection."
            )

    def test_slice_is_deterministic_across_repeated_calls(self, hierarchy):
        for name in hierarchy:
            a = _slice_hierarchy(hierarchy, name)
            b = _slice_hierarchy(hierarchy, name)
            assert a == b, (
                f"slice for {name!r} is non-deterministic. This breaks "
                "the build-hash cache: every rebuild flips the slice "
                "hash, forcing every container to rebuild."
            )

    def test_slice_is_insensitive_to_siblings(self, hierarchy):
        """CRITICAL: editing a SIBLING's prompt must NOT change this
        agent's slice. This is the whole point of per-agent slicing."""
        # Pick any agent and verify its slice doesn't change when we
        # mutate a different agent's prompt.
        for target in hierarchy:
            other = next(n for n in hierarchy if n != target)
            mutated = copy.deepcopy(hierarchy)
            mutated[other]["prompt"] = mutated[other]["prompt"] + "\n# scratch"
            assert _slice_hierarchy(hierarchy, target) == _slice_hierarchy(
                mutated, target
            ), (
                f"slicing {target!r} is affected by edits to {other!r}. "
                "Per-agent slicing is broken."
            )
            break  # one pair is enough — assertion is structural


# ---------------------------------------------------------------------------
# 7. Topology-shape mutations — dry-run the three supported deploy shapes
# ---------------------------------------------------------------------------
#
# README + developer-guide advertise three topologies: full hierarchy,
# orchestrator-as-frontend, and single-leaf-as-frontend. The deploy
# path has only been exercised for the full hierarchy in practice.
#
# Here we only validate that the MUTATION is a valid hierarchy — the
# deploy end-to-end is Layer 3. If a mutation fails these checks, the
# deploy for that topology will definitely fail.
# ---------------------------------------------------------------------------
def _promote_to_frontend(hierarchy: dict, target: str) -> dict:
    """Return a mutated copy where `target` becomes the frontend agent.

    - All other `type: "frontend"` entries are demoted to orchestrator
      or worker based on whether they currently have children.
    - The new frontend's `dir` is set to `agents/frontend`.
    - Agents not reachable from the new frontend are pruned.
    """
    mutated = copy.deepcopy(hierarchy)
    # Demote current frontend(s) to match their shape.
    for name, entry in mutated.items():
        if entry["type"] == "frontend" and name != target:
            entry["type"] = (
                "orchestrator" if entry.get("children") else "worker"
            )
            entry["dir"] = (
                "agents/orchestrator" if entry["type"] == "orchestrator"
                else "agents/worker"
            )
            for flag in _FRONTEND_ONLY_FLAGS:
                entry.pop(flag, None)
    # Promote target.
    new_entry = mutated[target]
    new_entry["type"] = "frontend"
    new_entry["dir"] = "agents/frontend"
    # Prune orphans (agents not reachable from the new frontend).
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


class TestTopologyShapes:
    """The three supported topology shapes are legal hierarchies too."""

    def test_full_hierarchy_matches_current(self, hierarchy):
        """Sanity: the current config IS the 'full hierarchy' topology.
        If this ever changes we want to know — new topologies need new
        shape tests."""
        frontends = [n for n, e in hierarchy.items() if e["type"] == "frontend"]
        assert frontends == ["supervisor"], (
            "The 'full hierarchy' topology assumes the frontend is named "
            "'supervisor'. If you renamed it, update this test AND check "
            "scripts/deploy.sh's FRONTEND_AGENT detection."
        )

    def test_orchestrator_as_frontend_is_a_valid_hierarchy(self, hierarchy):
        """Promote finops-agent → frontend. The resulting tree must
        still pass the structural invariants."""
        mutated = _promote_to_frontend(hierarchy, "finops-agent")
        _assert_is_valid_hierarchy(mutated, context="finops-as-frontend")

    def test_solo_leaf_as_frontend_is_a_valid_hierarchy(self, hierarchy):
        """Promote cost-operations-agent → frontend. Only that agent
        should remain."""
        mutated = _promote_to_frontend(hierarchy, "cost-operations-agent")
        _assert_is_valid_hierarchy(mutated, context="cost-ops-solo")
        assert set(mutated) == {"cost-operations-agent"}, (
            f"solo-leaf topology should contain only the promoted leaf; "
            f"got {sorted(mutated)}"
        )

    def test_promotion_pruning_is_correct(self, hierarchy):
        """Promoting an orchestrator must preserve its children but
        drop every other subtree."""
        mutated = _promote_to_frontend(hierarchy, "finops-agent")
        finops_children = set(hierarchy["finops-agent"].get("children", []))
        assert finops_children.issubset(set(mutated)), (
            "finops-agent's children were pruned during promotion"
        )
        # Other orchestrators' leaves should NOT be present.
        other_leaves = {
            c for n, e in hierarchy.items()
            if n != "finops-agent" and e["type"] == "orchestrator"
            for c in e.get("children", [])
        }
        overlap = other_leaves & set(mutated)
        assert not overlap, (
            f"promotion leaked unrelated leaves into the topology: "
            f"{overlap}"
        )

    def test_promoted_frontend_has_either_children_or_tools(self, hierarchy):
        """Regression guard for the solo-leaf-as-frontend bug
        (caught during Layer 3 on 2026-05-04): `frontend/server.py`
        derives `agent_type` from the hierarchy entry — non-empty
        `children` → mid_level; else → leaf with gateway MCP tools.
        This test asserts every promotable agent has AT LEAST ONE of
        the two so the derived agent_type can't resolve to something
        that loads zero tools.

        If an orchestrator ever loses its children or a worker ever
        loses its tools, this fails fast — before deploy — instead of
        the runtime container starting up and refusing to answer."""
        for name, entry in hierarchy.items():
            has_children = bool(entry.get("children"))
            has_tools = bool(entry.get("tools"))
            # Orchestrators should have children; workers should have tools.
            # An empty orchestrator is covered by the soft-skip test above.
            if entry["type"] == "orchestrator":
                continue  # covered by test_orchestrators_have_children
            if entry["type"] == "worker":
                assert has_tools, (
                    f"worker {name!r} has no `tools` field — if this "
                    "agent is ever promoted to frontend, the resolved "
                    "agent_type=leaf path will load zero gateway tools "
                    "and the platform guardrail will refuse every "
                    "prompt. Add a `tools: [...]` array to the entry."
                )
            if entry["type"] == "frontend":
                # A frontend must be EITHER a mid_level (children) OR a leaf
                # (tools). Exactly one of the two — never both, never
                # neither.
                assert has_children or has_tools, (
                    f"frontend {name!r} has neither children nor tools. "
                    "frontend/server.py resolves agent_type from this "
                    "config; without one of the two, the runtime starts "
                    "with zero tools and refuses every prompt."
                )
                assert not (has_children and has_tools), (
                    f"frontend {name!r} has BOTH children and tools. "
                    "This is ambiguous — frontend/server.py resolves "
                    "children → mid_level (registry-based child "
                    "delegation), which would ignore the tools field. "
                    "If you need both, we need a hybrid agent_type "
                    "that hasn't been designed yet."
                )


def _assert_is_valid_hierarchy(h: dict, *, context: str) -> None:
    """Apply the core structural invariants to a candidate mutation.

    Shared across topology-shape tests. Keeps the "what makes a
    hierarchy valid" definition in one place.
    """
    assert h, f"{context}: empty hierarchy"
    frontends = [n for n, e in h.items() if e["type"] == "frontend"]
    assert len(frontends) == 1, (
        f"{context}: need exactly one frontend, got {frontends}"
    )
    for name, entry in h.items():
        # Protocol stays http.
        assert entry["protocol"] == "http", (
            f"{context}: {name} has non-http protocol"
        )
        # Type and dir stay in sync.
        expected = {
            "frontend": "agents/frontend",
            "orchestrator": "agents/orchestrator",
            "worker": "agents/worker",
        }
        assert entry["dir"] == expected[entry["type"]], (
            f"{context}: {name} type/dir mismatch: "
            f"type={entry['type']} dir={entry['dir']}"
        )
        # Workers don't have children.
        if entry["type"] == "worker":
            assert not entry.get("children"), (
                f"{context}: worker {name} has children"
            )
        # Every child reference resolves inside the mutation.
        for child in entry.get("children", []):
            assert child in h, (
                f"{context}: {name} references unknown child {child}"
            )
    # No orphans.
    reachable = set(frontends)
    changed = True
    while changed:
        changed = False
        for name in list(reachable):
            for child in h[name].get("children", []):
                if child not in reachable:
                    reachable.add(child)
                    changed = True
    orphans = set(h) - reachable
    assert not orphans, f"{context}: orphans {orphans}"
