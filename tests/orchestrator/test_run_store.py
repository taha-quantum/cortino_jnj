import json

import pytest

from cortino.orchestrator.run_store import RunStore
from cortino.orchestrator.states import IllegalTransition, RunState


@pytest.fixture
def mk(tmp_path):
    stores = []

    def make():
        stores.append(RunStore(tmp_path))
        return stores[-1]

    yield make
    for st in stores:
        st.close()


def test_run_folder_created(tmp_path, mk):
    run = mk().create("a.twbx", {"x": 1})
    assert len(run.id) == 32
    assert run.state == RunState.CREATED
    assert (tmp_path / run.id / "input").is_dir()
    assert run.meta["x"] == 1 and run.verify_loops == 0


def test_store_persists_across_instances(tmp_path, mk):
    run = mk().create("a", {})
    mk().transition(run.id, RunState.INTAKE)
    again = mk()
    assert again.get(run.id).state == RunState.INTAKE
    assert [r.id for r in again.list()] == [run.id]


def test_events_appended_in_order(tmp_path, mk):
    s = mk()
    run = s.create("a", {})
    s.transition(run.id, RunState.INTAKE, note="go")
    s.event(run.id, "custom", k=1)
    s.transition(run.id, RunState.PARSED)
    lines = [json.loads(x) for x in (s.path(run.id) / "events.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in lines] == ["transition", "custom", "transition"]
    assert lines[0]["from"] == "CREATED" and lines[0]["to"] == "INTAKE" and lines[0]["note"] == "go"
    assert all("ts" in e for e in lines)


def test_illegal_transition_raises_and_keeps_state(tmp_path, mk):
    s = mk()
    run = s.create("a", {})
    with pytest.raises(IllegalTransition):
        s.transition(run.id, RunState.PR_OPEN)
    assert s.get(run.id).state == RunState.CREATED


def test_needs_input_resumes_only_to_origin(tmp_path, mk):
    s = mk()
    run = s.create("a", {})
    s.transition(run.id, RunState.INTAKE)
    s.transition(run.id, RunState.NEEDS_INPUT)
    assert s.get(run.id).meta["resume_to"] == "INTAKE"
    with pytest.raises(IllegalTransition):
        s.transition(run.id, RunState.PARSED)
    s.transition(run.id, RunState.INTAKE)
    assert s.get(run.id).state == RunState.INTAKE


def test_set_meta_merges(tmp_path, mk):
    s = mk()
    run = s.create("a", {"x": 1})
    s.set_meta(run.id, y=2)
    assert s.get(run.id).meta == {"x": 1, "y": 2, "input_name": "a"}


def test_event_append_failure_rolls_back_transition(tmp_path, mk, monkeypatch):
    s = mk()
    run = s.create("a", {})

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(s, "_append", boom)
    with pytest.raises(OSError):
        s.transition(run.id, RunState.INTAKE)
    assert s.get(run.id).state == RunState.CREATED


def test_stale_transition_from_second_instance_rejected(tmp_path, mk):
    a, b = mk(), mk()
    run = a.create("a", {})
    a.transition(run.id, RunState.INTAKE)
    with pytest.raises(IllegalTransition):
        b.transition(run.id, RunState.INTAKE)
    assert b.get(run.id).state == RunState.INTAKE


def test_verify_loops_counts_only_verify_retries(tmp_path, mk):
    s = mk()
    run = s.create("a", {})
    for st in (RunState.INTAKE, RunState.PARSED, RunState.PLANNED, RunState.BUILDING,
               RunState.INTEGRATING, RunState.BUILDING):
        s.transition(run.id, st)
    assert s.get(run.id).verify_loops == 0
    for st in (RunState.INTEGRATING, RunState.VERIFYING, RunState.BUILDING):
        s.transition(run.id, st)
    assert s.get(run.id).verify_loops == 1


def test_event_unknown_run_raises(tmp_path, mk):
    with pytest.raises(KeyError):
        mk().event("nope", "x")
