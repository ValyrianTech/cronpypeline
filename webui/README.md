# cronpypeline dashboard

A standalone FastAPI web UI that visualizes the live state of cronpypeline pipelines.

Read-only, except for one interaction: an **enable/disable toggle** that writes
`{"enabled": true|false}` to the pipeline's `config_file` (the same toggle the
tick loop checks).

## Features

- Config dropdown listing all `*.json` files in the configs directory
- Per-target "subway map" lanes showing every stage's state, derived live from
  filesystem markers (complete / processing / stale / gave up / rejected / pending)
- Animated progress bars, pulsing processing rings, completion glow effects
- Summary cards (targets, complete, processing, stale, gave up)
- Click any stage node for a detail panel (trigger/action type, timeouts,
  retries, processing marker JSON)
- Mode badge, retry/rejection count badges, target-lock ACTIVE indicator
- SWE plugin state badges per target (when a `.SWE/` directory exists):
  PR number + lifecycle state (open / approved / changes requested / merged /
  rejected, linked to GitHub) from `.SWE/pr_published.json`, active GitHub
  session badge from `.SWE/github_session.json`, and issue counts by status
  from `.SWE/issues/*.md` — the detail panel for PR-related stages shows the
  raw PR/session JSON
- Auto-refresh via polling every 4 seconds

## Setup

```bash
cd webui
pip install -r requirements.txt
# cronpypeline itself must be importable (e.g. installed with `pip install -e ..`)
```

## Run

```bash
# Default: serves configs from ../configs on http://127.0.0.1:8600
python app.py

# Custom configs directory / port
python app.py --configs-dir /path/to/configs --port 8600

# Or via uvicorn directly (configs dir via env var)
CRONPYPELINE_CONFIGS_DIR=/path/to/configs uvicorn app:app --port 8600
```

Then open http://127.0.0.1:8600

The module can be imported without fastapi/pydantic installed — the FastAPI app is built lazily via `_build_app()`, and the module-level `app` is `None` when those dependencies are missing (useful for importing helper functions without installing the web stack).

## Notes

- The dashboard never executes pipeline actions and never acquires the pipeline
  lock — it only derives state from markers, exactly like `--status`.
- Stages without markers (custom plugin-managed stages) are shown with a dashed
  "stateless" node.
- If a pipeline defines no `config_file`, the enable/disable toggle is hidden.
- If the workspace or registry paths in a config don't exist on this machine,
  the UI shows an error banner instead of data.
