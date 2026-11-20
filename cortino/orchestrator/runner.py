import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace

from cortino.config import load
from cortino.orchestrator.run_store import Run, RunStore
from cortino.orchestrator.states import (
    END_STATES,
    TRANSITIONS,
    IllegalTransition,
    RunState,
)

S = RunState
GATES = {S.AWAITING_PLAN_APPROVAL, S.AWAITING_PREVIEW_APPROVAL}
_STOPS = GATES | END_STATES | {S.NEEDS_INPUT}

_tracer = trace.get_tracer("cortino")


class StageError(Exception):
    """A stage failed for a reason worth recording; the runner moves the run to FAILED."""


class WrongState(Exception):
    """The requested action is not allowed in the run's current state."""


@dataclass
class RunContext:
    store: RunStore
    run: Run
    path: Path
    config: dict


@dataclass
class StageResult:
    next: RunState
    note: str = ""
    owner_failures: dict[str, list] | None = None


StageFn = Callable[[RunContext], StageResult]


class Runner:
    """Drives runs through the stage sequence.

    At the verify-loop cap the run goes to NEEDS_INPUT; resuming it gives one more build cycle
    (BUILDING with the last findings in fix.json, verify_loops reset).
    A call whose run was moved on by someone else stops quietly (stale_advance event).
    """

    def __init__(self, store: RunStore, stages: dict[RunState, StageFn], config: dict | None = None):
        self.store = store
        self.stages = stages
        self.config = config if config is not None else load("thresholds")

    def advance(self, run_id: str) -> RunState:
        """Run stages until a gate, NEEDS_INPUT or an end state."""
        while (state := self.store.get(run_id).state) not in _STOPS:
            if state == S.CREATED:
                try:
                    self.store.transition(run_id, S.INTAKE)
                except IllegalTransition:
                    self._stale(run_id, state)
                    break
            elif not self._run_stage(run_id, state):
                break
        return self.store.get(run_id).state

    def approve(self, run_id: str) -> RunState:
        state = self._require(run_id, GATES, "approve")
        if state == S.AWAITING_PLAN_APPROVAL:
            self._gate_transition(run_id, S.BUILDING)
        else:
            self._run_stage(run_id, state)
        return self.advance(run_id)

    def reject(self, run_id: str) -> RunState:
        self._require(run_id, GATES, "reject")
        self._gate_transition(run_id, S.REJECTED)
        return S.REJECTED

    def request_changes(self, run_id: str, text: str, visual_id: str | None = None) -> RunState:
        self._require(run_id, {S.AWAITING_PREVIEW_APPROVAL}, "request changes")
        self._gate_transition(run_id, S.CHANGES_REQUESTED)
        record = {"ts": datetime.now(UTC).isoformat(), "text": text, "visual_id": visual_id}
        with open(self.store.path(run_id) / "change_requests.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return self.advance(run_id)

    def resume(self, run_id: str) -> RunState:
        run = self.store.get(run_id)
        if run.state in END_STATES:
            raise WrongState(f"cannot resume a run in end state {run.state.value}")
        if run.state == S.NEEDS_INPUT:
            if "resume_to" not in run.meta:
                raise WrongState("run is NEEDS_INPUT but has no resume_to recorded")
            self._gate_transition(run_id, S(run.meta["resume_to"]))
        return self.advance(run_id)

    def _gate_transition(self, run_id: str, to: RunState) -> None:
        try:
            self.store.transition(run_id, to)
        except IllegalTransition as e:
            raise WrongState(f"run state changed concurrently ({e})") from e

    def _require(self, run_id: str, allowed: set[RunState], action: str) -> RunState:
        state = self.store.get(run_id).state
        if state not in allowed:
            raise WrongState(f"cannot {action} a run in state {state.value}")
        return state

    def _run_stage(self, run_id: str, state: RunState) -> bool:
        """Run the stage for `state` and apply its result. False if the run stopped."""
        run = self.store.get(run_id)
        ctx = RunContext(self.store, run, self.store.path(run_id), self.config)
        try:
            with _tracer.start_as_current_span(
                "cortino.stage", attributes={"run_id": run_id, "state": state.value}
            ):
                result = self.stages[state](ctx)
        except Exception as e:  # noqa: BLE001  KeyboardInterrupt/SystemExit propagate; resume reruns
            return self._fail(run_id, state, f"{type(e).__name__}: {e}")
        nxt, resume_to = result.next, None
        if (
            state == S.VERIFYING
            and nxt == S.BUILDING
            and run.verify_loops >= self.config["max_verify_loops"]
        ):
            nxt, resume_to = S.NEEDS_INPUT, S.BUILDING
        if self.store.get(run_id).state != state:
            return self._stale(run_id, state)
        if nxt not in TRANSITIONS[state]:
            return self._fail(run_id, state, f"illegal next state {nxt.value} from {state.value}")
        if nxt == S.NEEDS_INPUT:
            self.store.event(run_id, "needs_input", stage=state.value, message=result.note)
        try:
            self.store.transition(run_id, nxt, note=result.note, resume_to=resume_to)
        except IllegalTransition:
            return self._stale(run_id, state)
        return True

    def _stale(self, run_id: str, state: RunState) -> bool:
        self.store.event(run_id, "stale_advance", stage=state.value)
        return False

    def _fail(self, run_id: str, state: RunState, reason: str) -> bool:
        """Move the run to FAILED unless it already moved on; always returns False."""
        if self.store.get(run_id).state != state:
            return self._stale(run_id, state)
        self.store.event(run_id, "stage_failed", stage=state.value, error=reason)
        try:
            self.store.transition(run_id, S.FAILED, note=reason)
        except IllegalTransition:
            self._stale(run_id, state)
        return False
