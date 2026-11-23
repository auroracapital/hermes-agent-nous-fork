"""Trigger set of the Canary Release workflow (CI cleanup, 2026-11-19).

The schedule survives only so the prune job keeps cleaning up old canaries;
the tag job must stay gated to manual dispatch, or a scheduled run would
start tagging canaries again. That constraint is the core behaviour of the
cleanup, so it is pinned here directly against the workflow file.
"""
from pathlib import Path

from ruamel.yaml import YAML

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"


def test_canary_schedule_never_tags_a_new_canary():
    workflow = YAML(typ="safe").load(
        (WORKFLOWS / "canary-release.yml").read_text(encoding="utf-8"))
    on = workflow.get("on", workflow.get(True))
    triggers = set(on) if isinstance(on, dict) else {on}
    assert "schedule" in triggers
    assert "workflow_dispatch" in triggers
    tag_if = workflow["jobs"]["tag"]["if"].replace(chr(39), "")
    assert "github.event_name == workflow_dispatch" in tag_if
    prune_if = workflow["jobs"]["prune"]["if"]
    assert "always()" in prune_if
