# Cortino

Cortino rebuilds a Power BI or Tableau dashboard as a config-driven dashboard on your warehouse.
It parses the upload, plans the source mapping (you approve), builds the datasets and
`dashboard.json`, verifies the numbers and the rendered preview (you approve), and opens a PR.
It never merges and never deploys.

## Setup

```sh
uv sync
uv sync --extra tableau --extra powerbi    # optional: Hyper extracts, Tableau Server, Power BI service
uv run playwright install chromium         # the visual and functional checks drive Chromium
```

Copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY` for real runs.

## Tests (offline)

```sh
uv run pytest -q
uv run ruff check
```

Everything runs offline: a scripted fake LLM, a DuckDB sales warehouse built from
`tests/fixtures/sales`, a temp git repo and a local PR writer. `tests/e2e/test_pbip_e2e.py` takes
the sales PBIP from upload to an open PR. Tests marked `browser` use Chromium. Where it cannot launch,
`tests/checking/test_browser_checks.py` skips, the end-to-end tests still run and assert that the
visual and functional checks are reported skipped (not passed), and `test_report_written` in
`tests/checking/test_verify.py` fails; use `-m "not browser"` there. Tests marked `live` call the
Anthropic API and run only when `ANTHROPIC_API_KEY` is set (`uv run pytest -m live`).

## Running a dashboard

```sh
uv run cortino run Sales.zip --name Sales --target-repo ../platform-repo [--notes "..."] [--ref original.png]
uv run cortino status [<run-id>]                 # one run, or a table of all runs
uv run cortino approve <run-id>                  # at the plan gate, then at the preview gate (opens the PR)
uv run cortino request-changes <run-id> "make the bar chart a column chart" [--visual barSalesByRegion]
uv run cortino reject <run-id>
uv run cortino resume <run-id>                   # continue after a crash or NEEDS_INPUT
uv run cortino serve                             # the API (stub for now)
```

Accepted inputs: a zipped PBIP project, `.pbit`, Tableau `.twb`/`.twbx`, or a link in place of the
file: a Power BI report (`https://app.powerbi.com/groups/<ws>/reports/<id>`, needs `PBI_TENANT_ID`,
`PBI_CLIENT_ID`, `PBI_CLIENT_SECRET` and the `powerbi` extra) or a Tableau Server view
(`https://<server>/#/views/<workbook>/<view>`, needs `TABLEAU_PAT_NAME`, `TABLEAU_PAT_SECRET` and the
`tableau` extra). Neither endpoint has been verified live.
Everything a run produces is under `runs/<id>/` (`spec.json`, `plan.md`,
`verify/report.json`, `pr.json`, `pr.md` with the local git host, `events.jsonl`).

## Config to fill

- `config/sources.yaml`: `warehouse.path` (the DuckDB warehouse file) and `mappings` from the
  original source tables to warehouse tables.
- `config/models.yaml`: the model per role (`planner`, `builder`, `checker`, `cheap`).
- `config/thresholds.yaml`: data tolerance, loop limits, upload limits, budgets.
- `config/git.yaml`: `git_host: local` (default: writes `pr.md`, no push) or `github` (pushes and
  opens the PR with the `gh` CLI); `base_branch`.

## Not yet verified live

- The Anthropic models named in `config/models.yaml` (only fake LLMs have run the full pipeline).
- The Power BI service and Tableau Server endpoints (tested against mocked responses only).
- Real customer dashboards: only the sales fixture has been rebuilt end to end.
- The real platform's config format: `dashboard.json` follows Cortino's own reference schema.
