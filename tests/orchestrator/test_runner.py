import json

import pytest

from cortino.orchestrator.run_store import RunStore
from cortino.orchestrator.runner import Runner, StageError, StageResult, WrongState
from cortino.orchestrator.states import RunState as S


def go(next_state):
    return lambda ctx: StageResult(next_state)


def happy(calls=None):
    def wrap(name, nxt):
        def fn(ctx):
            if calls is not None:
                calls[name] = calls.get(name, 0) + 1
            return StageResult(nxt)

        return fn

    return {
        S.INTAKE: wrap("intake", S.PARSED),
        S.PARSED: wrap("parsed", S.PLANNED),
        S.PLANNED: wrap("planned", S.AWAITING_PLAN_APPROVAL),
        S.BUILDING: wrap("building", S.INTEGRATING),
        S.INTEGRATING: wrap("integrating", S.VERIFYING),
        S.VERIFYING: wrap("verifying", S.AWAITING_PREVIEW_APPROVAL),
        S.AWAITING_PREVIEW_APPROVAL: wrap("pr", S.PR_OPEN),
        S.CHANGES_REQUESTED: wrap("changes", S.PLANNED),
    }


@pytest.fixture
def store(tmp_path):
    s = RunStore(tmp_path / "runs")
    yield s
    s.close()


def new_run(store):
    return store.create("x.zip", {}).id


def events(store, run_id):
    lines = (store.path(run_id) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_stops_at_plan_gate(store):
    runner = Runner(store, happy())
    rid = new_run(store)
    assert runner.advance(rid) == S.AWAITING_PLAN_APPROVAL
    assert store.get(rid).state == S.AWAITING_PLAN_APPROVAL


def test_approve_continues_to_preview_gate(store):
    runner = Runner(store, happy())
    rid = new_run(store)
    runner.advance(rid)
    assert runner.approve(rid) == S.AWAITING_PREVIEW_APPROVAL
    assert runner.approve(rid) == S.PR_OPEN


def test_approve_in_other_state_raises(store):
    runner = Runner(store, happy())
    rid = new_run(store)
    with pytest.raises(WrongState):
        runner.approve(rid)
    with pytest.raises(WrongState):
        runner.reject(rid)
    with pytest.raises(WrongState):
        runner.request_changes(rid, "x")
    runner.advance(rid)
    with pytest.raises(WrongState):
        runner.request_changes(rid, "x")


def test_reject_at_gate(store):
    runner = Runner(store, happy())
    rid = new_run(store)
    runner.advance(rid)
    assert runner.reject(rid) == S.REJECTED


def test_verify_loop_caps_at_3_then_needs_input(store):
    stages = happy()
    verifying = []

    def verify(ctx):
        verifying.append(ctx.run.verify_loops)
        return StageResult(S.BUILDING)

    stages[S.VERIFYING] = verify
    runner = Runner(store, stages)
    rid = new_run(store)
    runner.advance(rid)
    assert runner.approve(rid) == S.NEEDS_INPUT
    run = store.get(rid)
    assert run.verify_loops == 3
    assert verifying == [0, 1, 2, 3]
    assert run.meta["resume_to"] == "BUILDING"


def test_resume_from_needs_input_reruns_origin_stage(store):
    stages = happy()
    outcomes = iter([S.BUILDING] * 4 + [S.AWAITING_PREVIEW_APPROVAL])
    stages[S.VERIFYING] = lambda ctx: StageResult(next(outcomes))
    runner = Runner(store, stages)
    rid = new_run(store)
    runner.advance(rid)
    assert runner.approve(rid) == S.NEEDS_INPUT
    assert runner.resume(rid) == S.AWAITING_PREVIEW_APPROVAL


def test_change_request_goes_to_planned_then_building(store):
    runner = Runner(store, happy())
    rid = new_run(store)
    runner.advance(rid)
    runner.approve(rid)
    assert runner.request_changes(rid, "bigger chart", visual_id="v1") == S.AWAITING_PLAN_APPROVAL
    lines = (store.path(rid) / "change_requests.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    assert (rec["text"], rec["visual_id"]) == ("bigger chart", "v1")
    assert "ts" in rec
    assert runner.approve(rid) == S.AWAITING_PREVIEW_APPROVAL
    runner.request_changes(rid, "again")
    assert len((store.path(rid) / "change_requests.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_resume_after_crash_reruns_only_current_stage(store):
    calls = {}
    stages = happy(calls)
    crash = {"on": True}

    def building(ctx):
        calls["building"] = calls.get("building", 0) + 1
        if crash["on"]:
            raise KeyboardInterrupt
        return StageResult(S.INTEGRATING)

    stages[S.BUILDING] = building
    runner = Runner(store, stages)
    rid = new_run(store)
    runner.advance(rid)
    with pytest.raises(KeyboardInterrupt):
        runner.approve(rid)
    assert store.get(rid).state == S.BUILDING
    crash["on"] = False
    assert runner.resume(rid) == S.AWAITING_PREVIEW_APPROVAL
    assert calls == {
        "intake": 1,
        "parsed": 1,
        "planned": 1,
        "building": 2,
        "integrating": 1,
        "verifying": 1,
    }


@pytest.mark.parametrize("exc", [StageError("boom"), ValueError("boom"), NotImplementedError("boom")])
def test_stage_error_marks_failed_with_reason_in_events(store, exc):
    stages = happy()

    def parsed(ctx):
        raise exc

    stages[S.PARSED] = parsed
    runner = Runner(store, stages)
    rid = new_run(store)
    assert runner.advance(rid) == S.FAILED
    failed = [e for e in events(store, rid) if e["kind"] == "stage_failed"]
    assert failed[0]["stage"] == "PARSED"
    assert "boom" in failed[0]["error"]


def test_needs_input_from_intake_records_message(store):
    stages = happy()
    stages[S.INTAKE] = lambda ctx: StageResult(S.NEEDS_INPUT, note="send a pbit")
    runner = Runner(store, stages)
    rid = new_run(store)
    assert runner.advance(rid) == S.NEEDS_INPUT
    ev = [e for e in events(store, rid) if e["kind"] == "needs_input"]
    assert ev[0]["message"] == "send a pbit"


def test_stale_advance_does_not_fail_or_raise(store):
    stages = happy()

    def parsed(ctx):
        ctx.store.transition(ctx.run.id, S.PLANNED)  # another worker won the race
        raise ValueError("loser blew up")

    stages[S.PARSED] = parsed
    runner = Runner(store, stages)
    rid = new_run(store)
    runner.advance(rid)
    assert store.get(rid).state != S.FAILED
    assert any(e["kind"] == "stale_advance" for e in events(store, rid))


def test_stale_result_is_dropped_quietly(store):
    stages = happy()

    def parsed(ctx):
        ctx.store.transition(ctx.run.id, S.PLANNED)
        return StageResult(S.PLANNED)

    stages[S.PARSED] = parsed
    runner = Runner(store, stages)
    rid = new_run(store)
    assert runner.advance(rid) == S.PLANNED
    assert any(e["kind"] == "stale_advance" for e in events(store, rid))


def test_double_approve_raises_wrong_state(store):
    runner = Runner(store, happy())
    rid = new_run(store)
    runner.advance(rid)
    store.transition(rid, S.BUILDING)
    with pytest.raises(WrongState):
        runner.approve(rid)


@pytest.mark.parametrize("bad", [S.PARSED, S.PR_OPEN])
def test_illegal_next_marks_failed_with_event(store, bad):
    stages = happy()
    stages[S.PARSED] = go(bad)
    runner = Runner(store, stages)
    rid = new_run(store)
    assert runner.advance(rid) == S.FAILED
    failed = [e for e in events(store, rid) if e["kind"] == "stage_failed"]
    assert bad.value in failed[0]["error"]


def test_resume_errors(store):
    runner = Runner(store, happy())
    rid = new_run(store)
    runner.advance(rid)
    runner.reject(rid)
    with pytest.raises(WrongState):
        runner.resume(rid)
    rid2 = new_run(store)
    store._db.execute("UPDATE runs SET state='NEEDS_INPUT' WHERE id=?", (rid2,))
    with pytest.raises(WrongState):
        runner.resume(rid2)


def test_change_request_resets_verify_loops(store):
    """The cap counts per build cycle: a change request after 3 loops still gets a rebuild."""
    stages = happy()
    outcomes = iter([S.BUILDING] * 3 + [S.AWAITING_PREVIEW_APPROVAL, S.BUILDING, S.AWAITING_PREVIEW_APPROVAL])
    stages[S.VERIFYING] = lambda ctx: StageResult(next(outcomes))
    stages[S.PLANNED] = lambda ctx: StageResult(
        S.BUILDING if ctx.store.get(ctx.run.id).meta.get("cr") else S.AWAITING_PLAN_APPROVAL
    )
    runner = Runner(store, stages)
    rid = new_run(store)
    runner.advance(rid)
    assert runner.approve(rid) == S.AWAITING_PREVIEW_APPROVAL
    assert store.get(rid).verify_loops == 3
    store.set_meta(rid, cr=True)
    assert runner.request_changes(rid, "tweak") == S.AWAITING_PREVIEW_APPROVAL
    assert store.get(rid).verify_loops == 1
    builds = [e for e in events(store, rid) if e["kind"] == "transition" and e["to"] == "BUILDING"]
    assert len(builds) == 6  # plan approval, 3 loops, the change request's plan, its failed verify


def test_resume_from_verify_cap_rebuilds_with_reset_counter(store):
    stages = happy()
    seen = []
    outcomes = iter([S.BUILDING] * 4 + [S.AWAITING_PREVIEW_APPROVAL])
    stages[S.VERIFYING] = lambda ctx: StageResult(next(outcomes))

    def building(ctx):
        seen.append(ctx.run.verify_loops)
        return StageResult(S.INTEGRATING)

    stages[S.BUILDING] = building
    runner = Runner(store, stages)
    rid = new_run(store)
    runner.advance(rid)
    assert runner.approve(rid) == S.NEEDS_INPUT
    assert store.get(rid).meta["resume_to"] == "BUILDING"
    assert runner.resume(rid) == S.AWAITING_PREVIEW_APPROVAL
    assert seen == [0, 1, 2, 3, 0]
    moves = [(e["from"], e["to"]) for e in events(store, rid) if e["kind"] == "transition"]
    assert ("NEEDS_INPUT", "BUILDING") in moves


def test_needs_input_from_verify_stage_resumes_to_verifying(store):
    """NEEDS_INPUT the verify stage chose itself (nothing a builder can fix) re-verifies."""
    stages = happy()
    outcomes = iter([S.NEEDS_INPUT, S.AWAITING_PREVIEW_APPROVAL])
    stages[S.VERIFYING] = lambda ctx: StageResult(next(outcomes))
    runner = Runner(store, stages)
    rid = new_run(store)
    runner.advance(rid)
    assert runner.approve(rid) == S.NEEDS_INPUT
    assert store.get(rid).meta["resume_to"] == "VERIFYING"
    assert runner.resume(rid) == S.AWAITING_PREVIEW_APPROVAL
