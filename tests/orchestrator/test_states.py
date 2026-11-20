from itertools import pairwise

import pytest

from cortino.orchestrator.states import TRANSITIONS, IllegalTransition, RunState

MAIN_LINE = [
    RunState.CREATED, RunState.INTAKE, RunState.PARSED, RunState.PLANNED,
    RunState.AWAITING_PLAN_APPROVAL, RunState.BUILDING, RunState.INTEGRATING,
    RunState.VERIFYING, RunState.AWAITING_PREVIEW_APPROVAL, RunState.PR_OPEN,
]


def test_main_line_is_legal():
    for a, b in pairwise(MAIN_LINE):
        assert b in TRANSITIONS[a], (a, b)
    assert RunState.BUILDING in TRANSITIONS[RunState.PLANNED]


def test_illegal_jump_raises():
    assert RunState.PR_OPEN not in TRANSITIONS[RunState.CREATED]
    assert issubclass(IllegalTransition, Exception)
    with pytest.raises(IllegalTransition):
        raise IllegalTransition("x")


def test_end_states_have_no_exits():
    for s in (RunState.PR_OPEN, RunState.REJECTED, RunState.FAILED):
        assert TRANSITIONS[s] == set()


def test_gates_and_failure_edges():
    for gate in (RunState.AWAITING_PLAN_APPROVAL, RunState.AWAITING_PREVIEW_APPROVAL):
        assert RunState.REJECTED in TRANSITIONS[gate]
    ends = {RunState.PR_OPEN, RunState.REJECTED, RunState.FAILED}
    for s in RunState:
        if s not in ends:
            assert RunState.FAILED in TRANSITIONS[s]
