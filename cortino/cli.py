import argparse
import ipaddress
import shutil
import sys
from pathlib import Path

from cortino.building.worktree import remove_worktree, run_worktree, slugify
from cortino.orchestrator.run_store import RunStore
from cortino.orchestrator.runner import Runner, StageError, WrongState
from cortino.orchestrator.stages import default_stages, real_deps
from cortino.orchestrator.states import IllegalTransition, RunState


def _setup_tracing() -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
    trace.set_tracer_provider(provider)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortino")
    parser.add_argument("--runs-dir", default="runs", help="run store root (default: runs)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="create a run from a file or a Power BI / Tableau link and advance it")
    p.add_argument("file")
    p.add_argument("--name", required=True)
    p.add_argument("--target-repo", required=True)
    p.add_argument("--notes", default="")
    p.add_argument("--ref", action="append", default=[], help="reference image (repeatable)")
    p.add_argument("--expected", help="folder of ground-truth CSVs named <visual id>.csv")

    p = sub.add_parser("status", help="show one run or a table of all runs")
    p.add_argument("run_id", nargs="?")

    for name in ("approve", "reject", "resume"):
        sub.add_parser(name, help=f"{name} a run").add_argument("run_id")

    p = sub.add_parser("request-changes", help="request changes at the preview gate")
    p.add_argument("run_id")
    p.add_argument("text")
    p.add_argument("--visual", default=None)

    p = sub.add_parser("serve", help="serve the API and web UI")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--runs-dir", default=argparse.SUPPRESS, help="run store root (default: runs)")
    return parser


def _status(store: RunStore, run_id: str | None) -> None:
    runs = [store.get(run_id)] if run_id else store.list()
    for r in runs:
        print(f"{r.id}  {r.state.value:<26} {r.meta.get('name', '')}")


def _serve(args) -> int:
    import uvicorn

    from cortino.api.server import create_app

    if args.host != "localhost" and not _is_loopback(args.host):
        print(f"warning: the API has no authentication; {args.host} may expose it", file=sys.stderr)
    runs = Path(args.runs_dir)
    try:
        deps = real_deps()  # one source connection and LLM for the process; closed at shutdown
    except StageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    store = RunStore(runs)
    try:
        app = create_app(
            store,
            lambda: Runner(store, default_stages(deps)),
            runs / "uploads",
            allowed_hosts=[args.host],
            deps=deps,
        )
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        store.close()
    return 0


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def entry() -> int:
    """The `cortino` console script: loads .env from the repo root (the shell's variables win),
    then runs the CLI. Tests call main() directly so a developer's .env never leaks into them."""
    from cortino.config import load_env_file

    load_env_file()
    return main()


def main(argv: list[str] | None = None, trace: bool = True) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if trace:
        _setup_tracing()
    store = RunStore(Path(args.runs_dir))
    runner = Runner(store, default_stages())
    try:
        if args.command == "status":
            _status(store, args.run_id)
            return 0
        if args.command == "run":
            if args.expected and not Path(args.expected).is_dir():
                print(f"error: --expected {args.expected} is not a directory", file=sys.stderr)
                return 1
            is_link = args.file.lower().startswith(("http://", "https://"))
            meta = {
                "upload": args.file if is_link else str(Path(args.file).resolve()),
                "name": args.name,
                "target_repo": args.target_repo,
                "notes": args.notes,
                "references": [str(Path(r).resolve()) for r in args.ref],
            }
            run_id = store.create(args.file if is_link else Path(args.file).name, meta).id
            if args.expected:  # read by the data-values check as ground truth
                (store.path(run_id) / "expected").mkdir()
                for csv in Path(args.expected).glob("*.csv"):
                    shutil.copyfile(csv, store.path(run_id) / "expected" / csv.name)
            state = runner.advance(run_id)
        elif args.command == "approve":
            run_id, state = args.run_id, runner.approve(args.run_id)
        elif args.command == "reject":
            run_id, state = args.run_id, runner.reject(args.run_id)
            meta = store.get(run_id).meta  # as the API does: drop the worktree and the branch
            wt = run_worktree(Path(meta["target_repo"]), store.path(run_id), slugify(meta["name"]), run_id)
            remove_worktree(wt, delete_branch=True)
        elif args.command == "resume":
            run_id, state = args.run_id, runner.resume(args.run_id)
        else:
            run_id = args.run_id
            state = runner.request_changes(run_id, args.text, args.visual)
        print(f"run {run_id}: {state.value}")
        return 1 if state == RunState.FAILED else 0
    except (WrongState, IllegalTransition, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
