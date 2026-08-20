# cronpypeline

> A Python library for building cron-friendly, stateful, multi-stage agentic pipelines driven by JSON configuration.

## 1. Problem Statement

Two existing pipelines — the **SWE Pipeline** (`run_swe_pipeline.py`, ~5,700 lines) and the **VNN Article Pipeline** (`run_article_pipeline.py`, ~1,900 lines) — share the same fundamental architecture but share zero code. Every new pipeline is built from scratch, duplicating:

- Cron-tick orchestration (one action per run, re-derive state from filesystem)
- Single-instance locking (fcntl-based, non-blocking)
- Stage detector chains (priority-ordered, first-match-wins)
- File-based state markers (`.processing`, `latest.md` symlinks, `published.json`, etc.)
- Timeout / stale-task detection and cleanup
- Retry / max-attempts / give-up logic
- Agent queueing (conversation queue integration)
- Enable/disable toggles
- Dry-run / verbose CLI flags
- Status reporting

**cronpypeline** extracts these shared patterns into a reusable, configuration-driven library so new pipelines can be created in minutes instead of weeks.

## 2. Goals

- **Configuration over code**: Define a pipeline entirely via a JSON config file — stages, commands, timeouts, retries, markers, agent queueing — without writing orchestrator code.
- **Cron-native**: Each invocation is a single "tick" that takes one action and exits. State is derived from the filesystem, not held in memory. Safe to run every minute via crontab.
- **Step management**: Easy to insert, reorder, rename, or remove steps by editing the JSON config — no code changes, no renumbering.
- **Resilience**: Built-in handling for timeouts, retries, stale tasks, and rollback/revert actions.
- **Agent-agnostic**: Works with any async agent dispatch mechanism (conversation queue, subprocess, HTTP API call) via pluggable action handlers.
- **Minimal dependencies**: Pure Python stdlib where possible. No heavy framework.

## 3. Non-Goals

- Not a workflow engine for long-running daemons (no persistent process, no event loop).
- Not tied to any specific LLM platform (Serendipity integration is a plugin, not a dependency).
- Not a CI/CD runner (no DAG fan-out, no parallel job execution).
- Not a replacement for cron itself — it runs *under* cron.

## 4. Extracted Patterns (from SWE + VNN pipelines)

### 4.1 Tick-based orchestration

Both pipelines follow the same model:

```
cron fires → script starts → acquire lock → derive state from filesystem →
  walk detector chain → first match executes one action → release lock → exit
```

- **SWE**: `main()` → `acquire_pipeline_lock()` → for each repo → `plan_next_action()` walks `STAGE_DETECTORS` list → first non-None detector returns an action dict `{stage, agent, reason, execute}` → `action['execute'](dry_run, verbose)` runs → exit.
- **VNN**: `main()` → check pipeline enabled → check conversation queue empty → walk priority stages (revision > publishing > writing > research > compilation) → first match queues an agent → exit.

**cronpypeline** unifies this into a single `Pipeline.tick()` method.

### 4.2 Single-instance lock

Both pipelines need to prevent overlapping cron invocations:

- **SWE**: `fcntl.flock(LOCK_EX | LOCK_NB)` on a lock file. Auto-releases on process exit. PID + timestamp written for debugging stale locks. Dry-run skips the lock.
- **VNN**: Checks if conversation queue has pending files (indirect lock via queue state).

**cronpypeline** provides `FileLock` with non-blocking acquisition, PID/timestamp recording, and dry-run bypass.

### 4.3 Stage detector chain

- **SWE**: `STAGE_DETECTORS` — a Python list of `Callable[[repo, verbose], Optional[Action]]`. `plan_next_action()` iterates and returns the first non-None result. Each detector returns `{stage, agent, reason, execute}` or `None` (nothing to do).
- **VNN**: Hardcoded if/elif priority chain in `main()` (revision > publishing > writing > research > compilation).

**cronpypeline** replaces both with a config-driven detector chain. Each stage in the JSON config declares its trigger condition and action. The library evaluates stages in order and executes the first match.

### 4.4 File-based state markers

Both pipelines use filesystem markers as the source of truth (no database, no in-memory state):

| Marker pattern | SWE example | VNN example |
|---|---|---|
| Completion marker | `coding_complete.marker` | `published.json` |
| Processing marker | (task.json exists = active) | `.processing` (JSON with agent, retry_count) |
| Latest report symlink | `reports/<stage>/latest.md` | N/A |
| Give-up marker | (issue `status: discarded`) | `.gave_up` |
| Rejection marker | (issue `status: open` + attempts) | `rejected-article.md` + `revision-notes.md` |
| Lock file | `pipeline.lock` (fcntl) | `.active_story` (story-level lock) |
| Config toggle | `swe_pipeline_config.json` | `pipeline_config.json` |

**cronpypeline** generalizes these into a `Marker` system with configurable names, formats (file existence / JSON content / symlink), and semantics (completion / processing / give-up / lock).

### 4.5 Timeout and stale-task handling

- **SWE**: `TASK_TIMEOUT_MINUTES` (30 min). If a task's `task.json` timestamp is older than the threshold and no `coding_complete.marker` exists, the task is stale → cleanup (force-checkout, delete branch, reset issue). Also: "agent forgot marker" recovery (queue empty + git commits = proceed to gate).
- **VNN**: Stale `.processing` markers older than 30 min are cleaned up. Stale compilation markers older than 60 min. Retry count tracked in marker JSON. Max 3 retries / 5 rejections before `.gave_up`.

**cronpypeline** provides configurable per-stage timeouts, stale-marker cleanup, retry counting, and max-attempts give-up logic.

### 4.6 Agent queueing

- **SWE**: `queue_agent()` writes a JSON file to `conversation_queue/` with agent name, prompt, model, temperature, etc. The `conversation_queue_monitor` picks it up asynchronously.
- **VNN**: Same pattern — `queue_agent()` drops JSON in `conversation_queue/`.

**cronpypeline** abstracts this into a pluggable `ActionHandler` interface. The default handler writes to a conversation queue directory. Custom handlers can use HTTP APIs, subprocess calls, or any other mechanism.

### 4.7 Stage chaining (mechanical stages)

- **SWE**: After a mechanical (non-agent) stage passes, the orchestrator immediately chains to the next stage in the same tick instead of waiting for the next cron cycle. Stops chaining when an LLM agent is queued (needs async time) or when leaving Phase A.
- **VNN**: No chaining — always one action per tick.

**cronpypeline** supports configurable chaining: stages can declare `chain: true` to allow same-tick continuation when the action is synchronous/mechanical.

### 4.8 Enable/disable and multi-target

- **SWE**: Global toggle (`swe_pipeline_config.json`) + per-repo `enabled` flag in `repos.json`. `--repo` flag to limit to one repo. `--all` to process all repos in one tick.
- **VNN**: Global toggle (`pipeline_config.json`). Per-country scheduling via scheduled tasks.

**cronpypeline** supports a global enable/disable config and optional multi-target iteration (e.g., multiple repos, multiple countries).

## 5. Architecture

### 5.1 Core classes

```
Pipeline
├── config: PipelineConfig          # Loaded from JSON
├── lock: FileLock                  # Single-instance guard
├── state: PipelineState            # Filesystem-derived state
├── stages: list[Stage]             # Ordered stage definitions
├── action_handler: ActionHandler   # Pluggable agent dispatch
└── tick() -> TickResult            # One cron invocation
    ├── acquire lock
    ├── check enabled
    ├── for each target (if multi-target):
    │   ├── derive state from filesystem
    │   ├── walk stages in order:
    │   │   ├── evaluate trigger condition
    │   │   ├── first match → execute action
    │   │   └── chain if configured + mechanical
    │   └── record result
    └── release lock → exit

Stage
├── id: str                         # Unique stage identifier (e.g. "A1", "research")
├── name: str                       # Human-readable name
├── trigger: TriggerCondition       # When this stage should fire
├── action: ActionSpec              # What to do when triggered
├── timeout_minutes: int            # Per-stage timeout
├── max_retries: int                # Max attempts before give-up
├── chain: bool                     # Allow same-tick chaining (mechanical only)
├── markers: dict                   # Completion/processing/give-up marker specs
└── on_fail: Optional[ActionSpec]   # Revert/rollback action on failure

TriggerCondition (discriminator-based)
├── type: "file_missing"            # Fire if a file doesn't exist
├── type: "file_exists"             # Fire if a file exists
├── type: "file_older_than"         # Fire if file is older than N minutes
├── type: "marker_state"            # Fire based on marker JSON field value
├── type: "queue_empty"             # Fire if action queue is empty
├── type: "custom"                  # User-provided Python callable path
└── combinators: "and", "or"        # Compose conditions

ActionSpec
├── type: "command"                 # Run a shell command (mechanical)
├── type: "queue_agent"             # Drop a file in conversation queue
├── type: "subprocess"              # Run a Python script as subprocess
├── type: "http_request"            # Call an HTTP endpoint
├── type: "custom"                  # User-provided Python callable path
├── params: dict                    # Type-specific parameters
├── timeout_seconds: int            # Execution timeout
└── produces: list[MarkerSpec]      # Markers created on success

MarkerSpec
├── name: str                       # Filename (e.g. "latest.md", ".processing")
├── type: "file"                    # Empty file = marker present
├── type: "json"                    # JSON file with fields
├── type: "symlink"                 # Symlink to latest report
├── content: dict                   # For JSON markers (field values)
└── directory: str                  # Relative to workspace/target dir

ActionHandler (plugin interface)
├── execute(action: ActionSpec, context: TickContext) -> bool
└── check_complete(action: ActionSpec, context: TickContext) -> bool

TickContext
├── pipeline: Pipeline
├── target: str                     # Current target (repo name, country, etc.)
├── workspace_dir: Path
├── state: PipelineState
├── dry_run: bool
├── verbose: bool
└── env: dict                       # Environment variables
```

### 5.2 Configuration format (JSON)

```json
{
  "name": "swe-pipeline",
  "workspace_dir": "/spellbook_data/Serendipity/swe/workspace",
  "lock_file": "pipeline.lock",
  "config_file": "swe_pipeline_config.json",
  "targets": {
    "type": "registry",
    "file": "repos.json",
    "key": "repos",
    "filter": {"enabled": true}
  },
  "action_handler": {
    "type": "conversation_queue",
    "queue_dir": "/spellbook_data/Serendipity/conversation_queue",
    "agent_settings_dir": "/spellbook_data/Serendipity/agents"
  },
  "stages": [
    {
      "id": "A0",
      "name": "Repo Onboarding",
      "trigger": {
        "type": "file_missing",
        "path": ".SWE/repo_briefing.md"
      },
      "action": {
        "type": "queue_agent",
        "params": {
          "agent": "RepoResearchAgent",
          "prompt": "Produce a comprehensive repository briefing..."
        }
      },
      "markers": {
        "completion": {"type": "file", "name": ".SWE/repo_briefing.md"}
      },
      "chain": false,
      "timeout_minutes": 30,
      "max_retries": 1
    },
    {
      "id": "A1",
      "name": "Test Infrastructure Check",
      "trigger": {
        "type": "file_missing",
        "path": ".SWE/reports/test-infra/latest.md"
      },
      "action": {
        "type": "command",
        "params": {
          "command": ".venv/bin/pytest -q",
          "cwd": "{target_dir}",
          "timeout_seconds": 900
        },
        "produces": [
          {"type": "symlink", "name": ".SWE/reports/test-infra/latest.md",
           "target": "{timestamp}.md"}
        ]
      },
      "chain": true,
      "timeout_minutes": 15,
      "max_retries": 1
    },
    {
      "id": "C-select",
      "name": "Select and Fix Open Issue",
      "trigger": {
        "type": "custom",
        "callable": "swe_plugin.detect_open_issue"
      },
      "action": {
        "type": "queue_agent",
        "params": {
          "agent": "CoderAgent",
          "prompt_template": "Fix issue {issue_id}..."
        }
      },
      "markers": {
        "completion": {"type": "file", "name": "coding_complete.marker"},
        "processing": {"type": "json", "name": "task.json",
                       "fields": {"issue_type": "{issue_type}"}}
      },
      "chain": false,
      "timeout_minutes": 30,
      "max_retries": 3,
      "on_fail": {
        "type": "command",
        "params": {
          "command": "git checkout integration && git branch -D {task_branch}"
        }
      }
    }
  ]
}
```

### 5.3 Runner script (user-facing)

The user writes a minimal Python script that cron invokes:

```python
from cronpypeline import Pipeline

pipeline = Pipeline.from_config("/path/to/pipeline.json")
exit_code = pipeline.tick(
    target="my-repo",       # optional: limit to one target
    dry_run=False,
    verbose=True,
)
```

Or via CLI:

```bash
python -m cronpypeline --config /path/to/pipeline.json --verbose
python -m cronpypeline --config /path/to/pipeline.json --dry-run
python -m cronpypeline --config /path/to/pipeline.json --target my-repo --all
```

### 5.4 Crontab usage

```cron
# Run pipeline every 5 minutes
*/5 * * * * cd /home/wouter/Repos/myproject && .venv/bin/python -m cronpypeline --config pipeline.json
```

## 6. Step Management

### 6.1 Inserting a step

Add a new entry to the `stages` array in the JSON config. The stage `id` is a string (not a sequential integer), so inserting between "A1" and "A2" doesn't require renumbering — use "A1b" or any unique label.

### 6.2 Reordering steps

Reorder the `stages` array. The detector chain evaluates stages in array order — first match wins.

### 6.3 Renaming a step

Change the `id` and `name` fields. Marker paths that reference the old id must be updated. No code changes.

### 6.4 Removing a step

Delete the entry from the `stages` array. Existing markers from the removed stage remain on disk but are never checked again.

### 6.5 Disabling a step

Set `"enabled": false` on the stage. It's skipped in the detector chain but remains in the config for easy re-enablement.

## 7. Failure Handling

### 7.1 Timeouts

Each stage has a `timeout_minutes` config. If a task's processing marker is older than this threshold, the pipeline:
1. Cleans up the stale marker
2. Increments the retry counter
3. Either re-queues the action (if retries remain) or writes a give-up marker

### 7.2 Retries and give-up

- `max_retries` (default 3): after this many failed attempts, the stage writes a give-up marker and the target is skipped on future ticks.
- Give-up markers are configurable (file name, JSON content).
- Manual recovery: delete the give-up marker to re-enable the stage.

### 7.3 Rollback / revert

A stage can declare an `on_fail` action that runs when the stage fails (gate fails, agent reports unfixable, etc.). This is used for:
- Cleaning up git branches (SWE: `git checkout integration && git branch -D task_branch`)
- Resetting state files
- Notifying external systems

### 7.4 Stale marker cleanup

The pipeline automatically cleans up stale processing markers (configurable age threshold). This handles the case where an agent crashed without writing a completion marker.

## 8. Plugin System

### 8.1 Action handlers

Built-in:
- **conversation_queue**: Writes JSON to a queue directory (Serendipity-compatible)
- **command**: Runs a shell command, captures stdout/stderr/exit code
- **subprocess**: Runs a Python script as a subprocess
- **http_request**: Sends an HTTP POST/GET to an endpoint

Custom: Register a Python callable via entry point or config path.

### 8.2 Trigger conditions

Built-in: `file_missing`, `file_exists`, `file_older_than`, `marker_state`, `queue_empty`, `and`, `or`.

Custom: Register a Python callable that receives `TickContext` and returns `bool`.

### 8.3 Report writers

Built-in: Markdown report with configurable template, symlink update.

Custom: Register a callable that receives the action result and writes a report in any format.

## 9. State Derivation

The pipeline does not hold state in memory. On each tick, it derives the current state from the filesystem:

- **Stage completion**: Check if the stage's completion marker exists (e.g., `latest.md` symlink, `published.json`).
- **Active task**: Check if a processing marker exists and is not stale.
- **Retry count**: Read from the processing marker's JSON content.
- **Give-up**: Check if a give-up marker exists.
- **Target enabled**: Check the target's config in the registry.

This makes the pipeline fully crash-safe: if the process is killed mid-tick, the next tick re-derives state from whatever markers were already written.

## 10. CLI interface

```
Usage: python -m cronpypeline [OPTIONS]

Options:
  --config PATH         Path to pipeline JSON config [required]
  --target NAME         Limit to a single target (repo, country, etc.)
  --all                 Process one action per target (default: first target with work)
  --dry-run             Show planned action without executing
  --verbose, -v         Verbose output
  --status              Print pipeline state and exit (no actions)
  --reset-stage ID      Delete a stage's completion marker to force re-run
  --reset-target NAME   Clear all markers for a target (nuclear reset)
```

## 11. Logging and Observability

- Each tick prints a one-line summary: `target | stage -> agent (reason) | result`
- `--verbose` adds: state derivation details, marker checks, command output
- `--status` prints a full state snapshot (all stages, all targets, marker status)
- Optional structured logging to a file (configurable path, JSON or text format)

## 12. Package Structure

```
cronpypeline/
├── __init__.py
├── pipeline.py              # Pipeline class, tick() orchestration
├── config.py                # PipelineConfig, Stage, TriggerCondition, ActionSpec
├── state.py                 # PipelineState, marker resolution
├── lock.py                  # FileLock (fcntl-based, non-blocking)
├── markers.py               # MarkerSpec, marker creation/reading
├── triggers.py              # Built-in trigger condition evaluators
├── actions.py               # Built-in action handlers
├── targets.py               # Target registry loading
├── cli.py                   # argparse CLI entry point
├── plugins/
│   ├── __init__.py
│   ├── conversation_queue.py  # Serendipity conversation queue handler
│   └── swe_plugin.py          # SWE pipeline custom triggers/actions
└── reporting.py             # Report writing, symlink management
```

## 13. Migration Path

Existing pipelines can be migrated incrementally:

1. **Phase 1**: Install cronpypeline alongside existing scripts. Create JSON configs that replicate the current detector chains. Run via `--dry-run` to verify parity.
2. **Phase 2**: Switch crontab entries from the old scripts to `python -m cronpypeline --config ...`.
3. **Phase 3**: Remove old orchestrator scripts. Custom logic (e.g., SWE's Targaryen Council ranking) moves to plugin callables.

## 14. Testing Strategy

- Unit tests for each core class (config parsing, marker resolution, trigger evaluation, lock acquisition).
- Integration tests using a temp directory as workspace, simulating multi-tick execution.
- Config validation: schema check on load (required fields, trigger/action type validity, marker path conflicts).
- Regression: port SWE and VNN configs and compare tick behavior against the original scripts.

## 15. Dependencies

- Python 3.10+
- No required third-party packages (stdlib only: `json`, `os`, `fcntl`, `subprocess`, `argparse`, `pathlib`, `datetime`, `typing`)
- Optional: `pydantic` for config validation (if installed, used for schema; otherwise stdlib dataclasses)

## 16. License

TBD — likely MIT or Apache 2.0.
