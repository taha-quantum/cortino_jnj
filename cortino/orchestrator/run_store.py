import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cortino.orchestrator.states import TRANSITIONS, IllegalTransition, RunState

_NEW_CYCLE = {
    (RunState.CHANGES_REQUESTED, RunState.PLANNED),
    (RunState.NEEDS_INPUT, RunState.BUILDING),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Run:
    id: str
    state: RunState
    created_at: str
    updated_at: str
    meta: dict = field(default_factory=dict)
    verify_loops: int = 0


class RunStore:
    def __init__(self, root: Path = Path("runs")):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            self.root / "cortino.db", check_same_thread=False, timeout=30, isolation_level=None
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, state TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, meta TEXT NOT NULL, "
            "verify_loops INTEGER NOT NULL DEFAULT 0)"
        )

    def path(self, run_id: str) -> Path:
        return self.root / run_id

    def create(self, input_name: str, meta: dict) -> Run:
        run_id = uuid.uuid4().hex
        now = _now()
        meta = {**meta, "input_name": input_name}
        with self._lock:
            self._db.execute(
                "INSERT INTO runs (id, state, created_at, updated_at, meta) VALUES (?,?,?,?,?)",
                (run_id, RunState.CREATED.value, now, now, json.dumps(meta)),
            )
        (self.path(run_id) / "input").mkdir(parents=True)
        return Run(run_id, RunState.CREATED, now, now, meta)

    def get(self, run_id: str) -> Run:
        with self._lock:
            row = self._db.execute(
                "SELECT id, state, created_at, updated_at, meta, verify_loops FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return Run(row[0], RunState(row[1]), row[2], row[3], json.loads(row[4]), row[5])

    def list(self) -> list[Run]:
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT id FROM runs ORDER BY created_at, rowid")]
        return [self.get(i) for i in ids]

    @contextmanager
    def _write(self):
        """Serialize writers across threads and store instances; roll back on any error."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def set_meta(self, run_id: str, **kv) -> None:
        with self._write():
            meta = {**self.get(run_id).meta, **kv}
            self._db.execute(
                "UPDATE runs SET meta=?, updated_at=? WHERE id=?",
                (json.dumps(meta), _now(), run_id),
            )

    def transition(
        self, run_id: str, to: RunState, note: str = "", resume_to: RunState | None = None
    ) -> None:
        """Entering NEEDS_INPUT records where a resume goes (`resume_to`, default the origin).
        verify_loops counts per build cycle: it is reset when a change request re-enters PLANNED
        and when a resume re-enters BUILDING."""
        with self._write():
            run = self.get(run_id)
            frm = run.state
            legal = to in TRANSITIONS[frm]
            if frm == RunState.NEEDS_INPUT and to != RunState.FAILED:
                legal = to.value == run.meta.get("resume_to")
            if not legal:
                raise IllegalTransition(f"{frm.value} -> {to.value}")
            meta = dict(run.meta)
            if to == RunState.NEEDS_INPUT:
                meta["resume_to"] = (resume_to or frm).value
            loops = run.verify_loops + int(frm == RunState.VERIFYING and to == RunState.BUILDING)
            if (frm, to) in _NEW_CYCLE:
                loops = 0
            self._db.execute(
                "UPDATE runs SET state=?, meta=?, verify_loops=?, updated_at=? WHERE id=?",
                (to.value, json.dumps(meta), loops, _now(), run_id),
            )
            # Appended before COMMIT: a failed append rolls the transition back.
            self._append(run_id, "transition", {"from": frm.value, "to": to.value, "note": note})

    def event(self, run_id: str, kind: str, **data) -> None:
        self.get(run_id)
        self._append(run_id, kind, data)

    def _append(self, run_id: str, kind: str, data: dict) -> None:
        line = json.dumps({"ts": _now(), "kind": kind, **data})
        with self._lock, open(self.path(run_id) / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def close(self) -> None:
        self._db.close()
