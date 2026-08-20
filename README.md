# cronpypeline

> A Python library for building cron-friendly, stateful, multi-stage agentic pipelines driven by JSON configuration.

## Overview

**cronpypeline** extracts the shared patterns from cron-based pipeline orchestration into a reusable, configuration-driven library. Instead of writing thousands of lines of custom orchestrator code for each new pipeline, you define your stages, triggers, actions, and markers in a JSON config file — and the library handles the rest.

### Key features

- **Cron-native**: Each invocation is a single "tick" that takes one action and exits. State is derived from the filesystem, not held in memory. Safe to run every minute via crontab.
- **Configuration over code**: Define stages, commands, timeouts, retries, markers, and agent queueing entirely via JSON — no orchestrator code needed.
- **Step management**: Insert, reorder, rename, or remove steps by editing the JSON config. No code changes, no renumbering.
- **Resilience**: Built-in handling for timeouts, retries, stale tasks, and rollback/revert actions.
- **Agent-agnostic**: Works with any async agent dispatch mechanism (conversation queue, subprocess, HTTP API call) via pluggable action handlers.
- **Crash-safe**: If a process is killed mid-tick, the next tick re-derives state from whatever markers were already written.
- **Minimal dependencies**: Pure Python stdlib. No heavy framework. Python 3.10+.

## Installation

```bash
# From source
git clone https://github.com/yourusername/cronpypeline.git
cd cronpypeline
python -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Or just install
pip install -e .
```

### Optional dependencies

```bash
# For config validation with pydantic
pip install -e ".[pydantic]"

# For development (pytest, coverage)
pip install -e ".[dev]"
```

## Quick start

### 1. Create a pipeline config

```json
{
  "name": "my-pipeline",
  "workspace_dir": "/path/to/workspace",
  "lock_file": "pipeline.lock",
  "targets": {
    "type": "static",
    "items": ["project-a", "project-b"]
  },
  "stages": [
    {
      "id": "A0",
      "name": "Research",
      "trigger": {
        "type": "file_missing",
        "path": "research.md"
      },
      "action": {
        "type": "queue_agent",
        "params": {
          "agent": "ResearchAgent",
          "prompt": "Research the codebase and produce a report."
        }
      },
      "markers": {
        "completion": {"type": "file", "name": "research.md"},
        "processing": {"type": "json", "name": ".processing", "content": {}}
      },
      "timeout_minutes": 30,
      "max_retries": 3
    },
    {
      "id": "A1",
      "name": "Test Suite",
      "trigger": {
        "type": "file_missing",
        "path": "test-results.md"
      },
      "action": {
        "type": "command",
        "params": {
          "command": ".venv/bin/pytest -q > test-results.md"
        }
      },
      "markers": {
        "completion": {"type": "file", "name": "test-results.md"}
      },
      "chain": true,
      "timeout_minutes": 15
    },
    {
      "id": "A2",
      "name": "Deploy",
      "trigger": {
        "type": "file_missing",
        "path": "deployed.marker"
      },
      "action": {
        "type": "command",
        "params": {
          "command": "echo deployed > deployed.marker"
        }
      },
      "markers": {
        "completion": {"type": "file", "name": "deployed.marker"}
      }
    }
  ]
}
```

### 2. Run it

**As a Python script:**

```python
from cronpypeline import Pipeline

pipeline = Pipeline.from_config("/path/to/pipeline.json")
exit_code = pipeline.tick(
    target="project-a",   # optional: limit to one target
    dry_run=False,
    verbose=True,
)
```

**Via CLI:**

```bash
# Single tick (first target with work)
python -m cronpypeline --config /path/to/pipeline.json --verbose

# Dry run (show what would happen)
python -m cronpypeline --config /path/to/pipeline.json --dry-run

# Process all targets in one invocation
python -m cronpypeline --config /path/to/pipeline.json --all

# Limit to a specific target
python -m cronpypeline --config /path/to/pipeline.json --target project-a

# Check pipeline status without executing
python -m cronpypeline --config /path/to/pipeline.json --status
```

### 3. Add to crontab

```cron
# Run pipeline every 5 minutes
*/5 * * * * cd /home/user/myproject && .venv/bin/python -m cronpypeline --config pipeline.json
```

## How it works

### Tick-based orchestration

Each cron invocation is a single **tick**:

```
cron fires → script starts → acquire lock → derive state from filesystem →
  walk detector chain → first match executes one action → release lock → exit
```

The pipeline takes **one action per tick** and exits. State is derived fresh from the filesystem on every tick — there is no in-memory state between invocations. This makes the pipeline fully crash-safe.

### Stage detector chain

Stages are evaluated in array order (first-match-wins). The first stage whose trigger condition is met gets executed. If no stage triggers, the tick returns `NO_WORK`.

### Chaining

Stages with `"chain": true` allow same-tick continuation when the action is synchronous (command, subprocess, custom). The pipeline chains through consecutive mechanical stages until it hits a non-chain stage, an async action (`queue_agent`), or a failure. This lets multi-step mechanical workflows complete in a single tick instead of waiting for multiple cron cycles.

### File-based state markers

The filesystem is the source of truth — no database, no in-memory state:

| Marker type | Purpose | Example |
|---|---|---|
| **Completion** | Stage is done | `research.md`, `coding_complete.marker` |
| **Processing** | Task is in progress | `.processing` (JSON with agent, retry_count, timestamp) |
| **Give-up** | Stage exhausted retries | `.gave_up` |
| **Symlink** | Latest report pointer | `latest.md → 20240101_120000.md` |

## Configuration reference

### Top-level config

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | string | yes | — | Pipeline name |
| `workspace_dir` | string | yes | — | Root workspace directory |
| `stages` | array | yes | `[]` | Ordered list of stage definitions |
| `lock_file` | string | no | `"pipeline.lock"` | Lock file path (relative to workspace) |
| `config_file` | string | no | `null` | Optional pipeline config toggle file |
| `targets` | object | no | `null` | Target specification (see below) |
| `action_handler` | object | no | `null` | Action handler plugin config |
| `log_file` | string | no | `null` | Optional log file path |

### Stage definition

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `id` | string | yes | — | Unique stage identifier (e.g. `"A0"`, `"C-select"`) |
| `name` | string | yes | — | Human-readable name |
| `trigger` | object | yes | — | Trigger condition (see below) |
| `action` | object | yes | — | Action spec (see below) |
| `chain` | bool | no | `false` | Allow same-tick chaining (mechanical only) |
| `timeout_minutes` | int | no | `30` | Per-stage timeout for stale detection |
| `max_retries` | int | no | `3` | Max attempts before give-up |
| `enabled` | bool | no | `true` | Skip this stage if false |
| `markers` | object | no | `{}` | Completion/processing/give-up marker specs |
| `on_fail` | object | no | `null` | Revert/rollback action on failure |

### Trigger conditions

| Type | Description | Required fields |
|---|---|---|
| `file_missing` | Fire if file doesn't exist | `path` |
| `file_exists` | Fire if file exists | `path` |
| `file_older_than` | Fire if file is older than N minutes | `path`, `minutes` |
| `marker_state` | Fire based on JSON marker field value | `path`, `field`, `op`, `value` |
| `queue_empty` | Fire if action queue directory is empty | `queue_dir` |
| `custom` | User-provided Python callable | `callable` |
| `and` | All conditions must be true | `conditions` (array of triggers) |
| `or` | Any condition must be true | `conditions` (array of triggers) |

**Operators for `marker_state`:** `eq`, `ne`, `lt`, `lte`, `gt`, `gte`

**Example:**

```json
{
  "type": "and",
  "conditions": [
    {"type": "file_missing", "path": "done.md"},
    {"type": "queue_empty", "queue_dir": "/path/to/queue"}
  ]
}
```

### Action specs

| Type | Description | Key params |
|---|---|---|
| `command` | Run a shell command | `command`, `cwd` |
| `queue_agent` | Drop a file in conversation queue | `agent`, `prompt` or `prompt_template` |
| `subprocess` | Run a Python script as subprocess | `script`, `args` |
| `http_request` | Call an HTTP endpoint | `url`, `method` |
| `custom` | User-provided Python callable | `callable` |

**Common fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `params` | object | `{}` | Type-specific parameters |
| `timeout_seconds` | int | `null` | Execution timeout |
| `produces` | array | `[]` | Markers created on success |

**Template variables** in `command`, `cwd`, and `prompt_template`:
- `{target}` — current target name
- `{target_dir}` — full path to target directory
- `{workspace_dir}` — full path to workspace

### Marker specs

| Type | Description | Fields |
|---|---|---|
| `file` | Empty file = marker present | `name`, `directory` |
| `json` | JSON file with fields | `name`, `directory`, `content` |
| `symlink` | Symlink to latest report | `name`, `directory`, `target` |

### Target specs

| Type | Description | Fields |
|---|---|---|
| `registry` | Load from JSON file with filter | `file`, `key`, `filter` |
| `static` | Fixed list of target names | `items` |
| `single` | One target | `name` |

**Registry example:**

```json
{
  "type": "registry",
  "file": "repos.json",
  "key": "repos",
  "filter": {"enabled": true}
}
```

With `repos.json`:
```json
{
  "repos": [
    {"name": "repo1", "enabled": true},
    {"name": "repo2", "enabled": false},
    {"name": "repo3", "enabled": true}
  ]
}
```

## Failure handling

### Timeouts

Each stage has a `timeout_minutes` config. If a task's processing marker is older than this threshold, the pipeline:
1. Cleans up the stale marker
2. Increments the retry counter
3. Either re-queues the action (if retries remain) or writes a give-up marker

### Retries and give-up

- `max_retries` (default 3): after this many failed attempts, the stage writes a give-up marker and the target is skipped on future ticks.
- Give-up markers are configurable (file name, JSON content).
- **Manual recovery**: delete the give-up marker to re-enable the stage.

### Rollback / revert

A stage can declare an `on_fail` action that runs when the stage fails. This is used for:
- Cleaning up git branches
- Resetting state files
- Notifying external systems

```json
{
  "on_fail": {
    "type": "command",
    "params": {
      "command": "git checkout integration && git branch -D {task_branch}"
    }
  }
}
```

## Step management

### Inserting a step

Add a new entry to the `stages` array. The stage `id` is a string (not a sequential integer), so inserting between "A1" and "A2" doesn't require renumbering — use "A1b" or any unique label.

### Reordering steps

Reorder the `stages` array. The detector chain evaluates stages in array order — first match wins.

### Renaming a step

Change the `id` and `name` fields. Marker paths that reference the old id must be updated. No code changes.

### Removing a step

Delete the entry from the `stages` array. Existing markers from the removed stage remain on disk but are never checked again.

### Disabling a step

Set `"enabled": false` on the stage. It's skipped in the detector chain but remains in the config for easy re-enablement.

## CLI reference

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

## Plugin system

### Action handlers

Built-in:
- **conversation_queue**: Writes JSON to a queue directory (Serendipity-compatible)
- **command**: Runs a shell command, captures stdout/stderr/exit code
- **subprocess**: Runs a Python script as a subprocess
- **custom**: Calls a user-provided Python callable

Custom: Register a Python callable via `register_handler()`:

```python
from cronpypeline import ActionHandler, ActionResult, register_handler, ActionType

class MyHandler(ActionHandler):
    def execute(self, action, context):
        # Do something
        return ActionResult(success=True, stdout="done")

    def check_complete(self, action, context):
        return True

register_handler(ActionType.QUEUE_AGENT, MyHandler())
```

### Trigger conditions

Built-in: `file_missing`, `file_exists`, `file_older_than`, `marker_state`, `queue_empty`, `and`, `or`.

Custom: Register a Python callable that receives a context dict and returns `bool`:

```json
{
  "type": "custom",
  "callable": "my_module.my_trigger_function"
}
```

```python
# my_module.py
def my_trigger_function(context):
    # context contains: target, target_dir, workspace_dir, etc.
    return True  # or False
```

### Built-in plugins

#### Conversation queue (`cronpypeline.plugins.conversation_queue`)

Writes JSON files to a conversation queue directory. Compatible with Serendipity's `conversation_queue_monitor`.

```python
from cronpypeline.plugins.conversation_queue import ConversationQueueHandler
from cronpypeline import register_handler, ActionType

handler = ConversationQueueHandler(
    queue_dir="/path/to/conversation_queue",
    agent_settings_dir="/path/to/agent_settings",
)
register_handler(ActionType.QUEUE_AGENT, handler)
```

Queue entry format:
```json
{
  "id": "uuid",
  "agent": "CoderAgent",
  "prompt": "Fix issue 42...",
  "target": "my-repo",
  "timestamp": 1234567890.0,
  "model": "gpt-4",
  "temperature": 0.7
}
```

#### SWE pipeline plugin (`cronpypeline.plugins.swe_plugin`)

Custom triggers and actions for the SWE pipeline:

- `detect_open_issue` — trigger: fires if there's an open issue in `issues.json`
- `detect_agent_forgot_marker` — trigger: fires when queue is empty + git commits exist but no completion marker
- `cleanup_git_branch` — action: cleans up git branches after failure
- `reset_issue_status` — action: resets issue status to "open" after failure

## Package structure

```
cronpypeline/
├── __init__.py              # Public API exports
├── __main__.py              # python -m cronpypeline entry point
├── pipeline.py              # Pipeline class, tick() orchestration
├── config.py                # PipelineConfig, Stage, TriggerCondition, ActionSpec
├── state.py                 # PipelineState, marker resolution
├── lock.py                  # FileLock (fcntl-based, non-blocking)
├── markers.py               # MarkerSpec, marker creation/reading
├── triggers.py              # Built-in trigger condition evaluators
├── actions.py               # Built-in action handlers
├── targets.py               # Target registry loading
├── cli.py                   # argparse CLI entry point
├── reporting.py             # Report writing, symlink management
└── plugins/
    ├── __init__.py
    ├── conversation_queue.py  # Serendipity conversation queue handler
    └── swe_plugin.py          # SWE pipeline custom triggers/actions
```

## Testing

```bash
# Run all tests
.venv/bin/python -m pytest

# Run with coverage
.venv/bin/python -m pytest --cov=cronpypeline

# Run a specific test module
.venv/bin/python -m pytest tests/test_pipeline.py -v
```

The test suite includes **243 tests** covering:
- Unit tests for each core class (config parsing, marker resolution, trigger evaluation, lock acquisition, action execution)
- Integration tests using temp directories as workspaces, simulating multi-tick execution
- Crash safety tests verifying state recovery from partial filesystem state
- Plugin tests for conversation queue and SWE plugin

## Python API reference

### Pipeline

```python
from cronpypeline import Pipeline

# Create from config file
pipeline = Pipeline.from_config("/path/to/pipeline.json")

# Single tick (one target)
result = pipeline.tick(target="my-repo", dry_run=False, verbose=True)

# Process all targets
results = pipeline.tick_all(dry_run=False, verbose=False)

# Get status snapshot
status = pipeline.status(targets=["my-repo"])
```

### TickResult

```python
from cronpypeline import TickResult, TickResultStatus

# Status values:
# - ACTION_EXECUTED  — action ran successfully
# - ACTION_FAILED    — action failed
# - NO_WORK          — nothing to do
# - DRY_RUN          — would have executed (dry-run mode)
# - GAVE_UP          — stage exhausted retries
# - LOCK_FAILED      — could not acquire lock
# - DISABLED         — pipeline disabled

result.target       # "my-repo"
result.stage_id     # "A0"
result.status       # TickResultStatus.ACTION_EXECUTED
result.message      # "Executed Step 1"
result.stdout       # command output
result.stderr       # command error output
result.chained_stages  # ["A1", "A2"] if chaining occurred
```

## Migration path

Existing pipelines can be migrated incrementally:

1. **Phase 1**: Install cronpypeline alongside existing scripts. Create JSON configs that replicate the current detector chains. Run via `--dry-run` to verify parity.
2. **Phase 2**: Switch crontab entries from the old scripts to `python -m cronpypeline --config ...`.
3. **Phase 3**: Remove old orchestrator scripts. Custom logic moves to plugin callables.

## Requirements

- Python 3.10+
- No required third-party packages (stdlib only: `json`, `os`, `fcntl`, `subprocess`, `argparse`, `pathlib`, `datetime`, `typing`)
- Optional: `pydantic` for config validation

## License

MIT
