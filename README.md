# cronpypeline

> A Python library for building cron-friendly, stateful, multi-stage agentic pipelines driven by JSON configuration.

## Overview

**cronpypeline** extracts the shared patterns from cron-based pipeline orchestration into a reusable, configuration-driven library. Instead of writing thousands of lines of custom orchestrator code for each new pipeline, you define your stages, triggers, actions, and markers in a JSON config file — and the library handles the rest.

### Key features

- **Cron-native**: Each invocation is a single "tick" that takes one action and exits. State is derived from the filesystem, not held in memory. Safe to run every minute via crontab.
- **Configuration over code**: Define stages, commands, timeouts, retries, markers, and agent queueing entirely via JSON — no orchestrator code needed.
- **Step management**: Insert, reorder, rename, or remove steps by editing the JSON config. No code changes, no renumbering.
- **Resilience**: Built-in handling for timeouts, retries, stale tasks, rejection tracking, and rollback/revert actions.
- **Agent-agnostic**: Works with any async agent dispatch mechanism (conversation queue, subprocess, HTTP API call) via pluggable action handlers.
- **Per-target config**: Registry targets carry per-target configuration (custom commands, thresholds, GitHub slugs) that flows into triggers, actions, and templates.
- **Mode switching**: Pipeline-wide mode file enables/disables groups of stages at runtime (e.g. "default" vs "github session" mode).
- **Target locking**: Optional cross-stage lock ensures one target flows through the entire pipeline before the next target starts.
- **Pre/post-tick hooks**: Run custom callables before state derivation (can skip tick) or after tick completion.
- **Marker invalidation**: Stages can declaratively delete other stages' markers on success — no manual cleanup code.
- **Rejection tracking**: Separate rejection counter (distinct from retries) with `max_rejections` give-up — supports revision/review loops.
- **Rejection audit trail**: Optional `post_tick` hook for append-only `rejection_log.json` with detailed entries (reasons, timestamps, rejection metadata).
- **Queue-file staleness**: Processing markers track queue file paths — immediate stale detection when agent finishes without producing completion.
- **Conversation ID continuation**: On retry, the previous `entry_id` is reused as `conversation_id` so agents continue the same conversation instead of starting fresh.
- **Serendipity-compatible queue format**: Configurable `prompt_field` (e.g. `content` instead of `prompt`), `default_fields` for static metadata (`sender`, `folder_name`, `model_name`, `runs_left`), and `flatten_agent_settings` for flat agent config merging.
- **Dynamic marker naming**: Marker names and directories support `{target}`, `{slug}`, and any target config key via template substitution.
- **Shell-safe command execution**: Template variables (`target`, `target_dir`, `workspace_dir`, and target config values) substituted into commands are shell-quoted with `shlex.quote()`, and commands are executed via an argument list (`shell=False`) rather than a shell, preventing command injection.
- **HTTP requests**: Built-in `http_request` action handler with auth token resolution from config, env vars, or context.
- **SWE pipeline plugins**: Issue store (YAML frontmatter), diagnostic report handlers with output parsers, prompt builders for fix/coder/review agents, GitHub session adapter.
- **VNN pipeline plugins**: Story state sync, inconsistent state cleanup, global queue-empty gate, completed compilation checks, story discovery, rejection audit trail.
- **Crash-safe**: If a process is killed mid-tick, the next tick re-derives state from whatever markers were already written.
- **Minimal dependencies**: Pure Python stdlib. No heavy framework. Python 3.10+.

## Installation

```bash
# From source
git clone https://github.com/ValyrianTech/cronpypeline.git
cd cronpypeline
python -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Or just install
pip install -e .
```

The build system requires `setuptools>=83.0.0` (bumped from `>=68.0` to address security vulnerability PYSEC-2026-3447).

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
  "mode_file": "mode.json",
  "target_lock": true,
  "action_handler": {
    "type": "conversation_queue",
    "params": {
      "queue_dir": "/path/to/conversation_queue",
      "agent_settings_dir": "/path/to/agent_settings",
      "prompt_field": "content",
      "default_fields": {
        "sender": "MY_PIPELINE",
        "conversation_id": "",
        "folder_name": "MY_PIPELINE",
        "model_name": "default_model",
        "runs_left": 3
      },
      "flatten_agent_settings": true
    }
  },
  "pre_tick": {"callable": "my_plugin.pre_tick_sync"},
  "post_tick": {"callable": "my_plugin.post_tick_log"},
  "targets": {
    "type": "registry",
    "file": "repos.json",
    "key": "repos",
    "filter": {"enabled": true}
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
          "prompt": "Research the codebase and produce a report.",
          "reminder_prompt": "Your previous research was incomplete. Please finish."
        }
      },
      "markers": {
        "completion": {"type": "file", "name": "research.md"},
        "processing": {"type": "json", "name": ".processing", "content": {}},
        "give_up": {"type": "file", "name": ".gave_up"}
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
          "command": "{test_cmd}"
        }
      },
      "markers": {
        "completion": {"type": "file", "name": "test-results.md"}
      },
      "invalidates": [
        {"type": "file", "name": "research.md"}
      ],
      "chain": true,
      "timeout_minutes": 15
    },
    {
      "id": "A2",
      "name": "Review",
      "trigger": {
        "type": "file_exists",
        "path": "test-results.md"
      },
      "action": {
        "type": "queue_agent",
        "params": {
          "agent": "ReviewAgent",
          "prompt": "Review the test results in {target_dir}/test-results.md."
        }
      },
      "markers": {
        "completion": {"type": "file", "name": "reviewed.marker"},
        "processing": {"type": "json", "name": ".review_processing", "content": {}},
        "rejection": {"type": "json", "name": ".rejection", "content": {}},
        "give_up": {"type": "file", "name": ".review_gave_up"}
      },
      "max_rejections": 3,
      "modes": ["default", "review"]
    },
    {
      "id": "A3",
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

Stages with `"chain": true` allow same-tick continuation when the action is synchronous (command, subprocess, custom). The pipeline chains through consecutive mechanical stages until it hits a non-chain stage, an async action, or a failure. This lets multi-step mechanical workflows complete in a single tick instead of waiting for multiple cron cycles.

Async actions include `queue_agent` actions and custom actions that return `data: {"async": true}`. For chaining purposes, async custom actions are treated like `queue_agent` actions: they stop the chain and create a processing marker instead of a completion marker. The chaining logic skips chaining when either the action type is `queue_agent` or the action returns `data: {"async": true}`. The processing marker is written with `retry_count=0` and the action's result data merged in, preventing duplicate agent queueing on subsequent ticks.

When a chained stage's action fails, the chain stops and the tick returns a `TickResult` with `ACTION_FAILED` status for the failed stage (instead of silently stopping the chain). The failed stage's ID is reported in `result.stage_id` and listed in `result.failed_chained_stages`. If the failed stage declares an `on_fail` action, it is executed before the failure is reported. When the failed action produces no output, the failure message is `"Chained stage X failed"` (no trailing colon).

### Mode switching

A pipeline can define a `mode_file` — a JSON file with `{"mode": "some_mode"}`. Stages with a `modes` list are only active when the current mode is in that list. Stages without `modes` are always active. This enables runtime behavior switching (e.g. "default" vs "github session" mode) without changing the config.

```json
{"mode": "github"}
```

### Target locking

When `target_lock: true` is set on the pipeline, no stage for a target is actionable while any stage for that target has a processing marker. This ensures one target flows through the entire pipeline before the next target starts — useful when stages have side effects that conflict across targets.

### Pre-tick / post-tick hooks

A pipeline can declare `pre_tick` and `post_tick` hooks — custom Python callables that run before state derivation and after tick completion respectively.

- **Pre-tick hooks** receive a context dict (`target`, `target_dir`, `workspace_dir`, `target_config`). Returning `False` skips the tick entirely.
- **Post-tick hooks** receive the context dict and the `TickResult`. Useful for logging, notifications, or cleanup.

```json
"pre_tick": {"callable": "my_plugin.sync_state"},
"post_tick": {"callable": "my_plugin.log_result"}
```

### Marker invalidation

Stages can declare an `invalidates` list — markers from other stages to delete when this stage's action succeeds. This enables cross-stage state cleanup without custom code (e.g. a fix stage deleting upstream report markers to force re-evaluation).

### Rejection tracking

Stages can define a `rejection` marker (JSON type) and `max_rejections` count. Rejections are tracked separately from retries — when `rejection_count >= max_rejections`, the stage writes a give-up marker. Below the max, the rejection count is written back into the JSON rejection marker (incremented only when the stage's trigger actually fires, so the stage will be re-processed this tick) so it accumulates across ticks. The rejection marker is cleared only when the stage's work actually completes (the completion marker is created), not when work is re-queued. This supports revision/review loops where an agent's work is rejected and must be redone.

A rejection marker only blocks a stage from being actionable when rejection tracking is enabled (`max_rejections > 0`). When `max_rejections=0` (rejection tracking disabled), a stage with a rejection marker remains actionable.

### Queue-file staleness

Processing markers can include a `queue_file` field (written automatically by `ConversationQueueHandler`). When the queue file no longer exists, the stage is immediately marked stale — no waiting for `timeout_minutes` to elapse. This detects the case where an agent finished but didn't produce a completion marker. Falls back to time-based staleness when no `queue_file` field is present.

### Dynamic marker naming

Marker names and directories support template substitution with context variables: `{target}`, `{target_dir}`, `{workspace_dir}`, and all flattened target config keys (e.g. `{slug}`). This enables per-target marker names like `queued_for_{slug}.marker`.

### File-based state markers

The filesystem is the source of truth — no database, no in-memory state:

| Marker role | Purpose | Example |
|---|---|---|
| **Completion** | Stage is done | `research.md`, `coding_complete.marker` |
| **Processing** | Task is in progress | `.processing` (JSON with retry_count, queue_file, timestamp) |
| **Give-up** | Stage exhausted retries or rejections | `.gave_up` |
| **Rejection** | Stage output was rejected (separate from retries) | `.rejection` (JSON with rejection_count) |
| **Symlink** | Latest report pointer | `latest.md → 20240101_120000.md` |

## Configuration reference

### Top-level config

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | string | yes | — | Pipeline name |
| `workspace_dir` | string | yes | — | Root workspace directory |
| `stages` | array | yes | `[]` | Ordered list of stage definitions |
| `lock_file` | string | no | `"pipeline.lock"` | Lock file path (relative to workspace) |
| `config_file` | string | no | `null` | Optional pipeline config toggle file (`{"enabled": false}` disables). Relative paths are resolved relative to `workspace_dir` |
| `targets` | object | no | `null` | Target specification (see below) |
| `action_handler` | object | no | `null` | Action handler plugin config (wired automatically) |
| `log_file` | string | no | `null` | Optional log file path |
| `mode_file` | string | no | `null` | Path to JSON file with `{"mode": "..."}` for mode switching. Relative paths are resolved relative to `workspace_dir` |
| `target_lock` | bool | no | `false` | Cross-stage lock — blocks all stages for a target while any stage is processing |
| `pre_tick` | object | no | `null` | Pre-tick hook config (`{"callable": "module.func"}`) |
| `post_tick` | object | no | `null` | Post-tick hook config (`{"callable": "module.func"}`) |

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
| `markers` | object | no | `{}` | Completion/processing/give_up/rejection marker specs |
| `on_fail` | object | no | `null` | Revert/rollback action on failure |
| `invalidates` | array | no | `[]` | Markers from other stages to delete on success |
| `modes` | array | no | `[]` | Active modes for this stage (empty = always active) |
| `max_rejections` | int | no | `0` | Max rejections before give-up (0 = disabled) |

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
| `queue_agent` | Drop a file in conversation queue | `agent`, `prompt` or `prompt_template`, `reminder_prompt`, `reminder_prompt_template` |
| `subprocess` | Run a Python script as subprocess | `script`, `args` |
| `http_request` | Call an HTTP endpoint | `url`, `method`, `headers`, `body`, `auth_token`, `auth_token_env` |
| `custom` | User-provided Python callable | `callable` |

**Common fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `params` | object | `{}` | Type-specific parameters |
| `timeout_seconds` | int | `null` | Execution timeout |
| `produces` | array | `[]` | Markers created on success |

**Template variables** in `command`, `cwd`, `prompt_template`, marker names, and marker directories:
- `{target}` — current target name
- `{target_dir}` — full path to target directory
- `{workspace_dir}` — full path to workspace
- `{target_config}` — full per-target config dict
- Any flattened target config key (e.g. `{slug}`, `{test_cmd}`, `{coverage_threshold}`) — available when using a registry target spec

Template variables substituted into commands (`command`-type actions) are shell-quoted with `shlex.quote()` before substitution, and commands are executed without a shell (via an argument list built with `shlex.split()`, i.e. `shell=False`), preventing command injection when a value (e.g. a target name or path) contains shell metacharacters. If template substitution fails (missing key, bad format, etc.), an error is raised rather than silently falling back to the unformatted template.

### Marker specs

| Type | Description | Fields |
|---|---|---|
| `file` | Empty file = marker present | `name`, `directory` |
| `json` | JSON file with fields | `name`, `directory`, `content` |
| `symlink` | Symlink to latest report | `name`, `directory`, `target` |

Marker paths must be relative and cannot contain `..` segments or absolute paths; `MarkerSpec.resolve_path` rejects path traversal (`..` segments), absolute paths, and any path that resolves outside the workspace/base directory (raising a `ValueError`).

### Target specs

| Type | Description | Fields |
|---|---|---|
| `registry` | Load from JSON file with filter | `file`, `key`, `filter` |
| `static` | Fixed list of target names | `items` |
| `single` | One target | `name` |

**Registry targets carry per-target config** — all fields except `name` are passed through as `target_config` to triggers, actions, and templates. Keys are flattened into the context dict for direct template access (e.g. `{test_cmd}`, `{slug}`).

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
    {"name": "repo1", "enabled": true, "slug": "org/repo1", "test_cmd": "pytest -q", "coverage_threshold": 90},
    {"name": "repo2", "enabled": false, "slug": "org/repo2", "test_cmd": "tox", "coverage_threshold": 80},
    {"name": "repo3", "enabled": true, "slug": "org/repo3", "test_cmd": ".venv/bin/pytest", "coverage_threshold": 100}
  ]
}
```

## Failure handling

Unhandled exceptions during a tick are caught and reported as an `ACTION_FAILED` `TickResult` with the correct target name, `stage_id=None`, and the traceback captured in `stderr`.

### Timeouts

Each stage has a `timeout_minutes` config. If a task's processing marker is older than this threshold, the pipeline:
1. Cleans up the stale marker
2. Increments the retry counter
3. Either re-queues the action (if retries remain) or writes a give-up marker

In dry-run mode, the pipeline reports what it would do — "Would re-queue stale stage X" or "Would give up on stale stage X (retry N >= max M)" — without actually deleting the processing marker or re-queueing. When the re-executed (re-queued) action fails, the pipeline returns `ACTION_FAILED` (instead of `ACTION_EXECUTED`), runs the stage's `on_fail` action if configured, and reports the failure message.

**Queue-file-based staleness**: If the processing marker contains a `queue_file` field, staleness is detected immediately when the queue file is gone (agent finished without producing completion) — no waiting for the timeout.

### Retries and give-up

- `max_retries` (default 3): after this many failed attempts, the stage writes a give-up marker and the target is skipped on future ticks.
- Give-up markers are configurable (file name, JSON content).
- **Manual recovery**: delete the give-up marker to re-enable the stage.

### Rejections and give-up

- `max_rejections` (default 0 = disabled): a separate counter from retries, tracked via a `rejection` marker (JSON type with `rejection_count`).
- A rejection marker only blocks a stage from being actionable when `max_rejections > 0` (rejection tracking enabled). When `max_rejections=0`, a rejected stage remains actionable.
- When `rejection_count >= max_rejections`, the stage writes a give-up marker.
- Below the max, the rejection count is written back into the JSON rejection marker (accumulating across ticks) so the count is not lost. The rejection marker is cleared only when the stage's work actually completes (the completion marker is created), not when work is re-queued.
- FILE-type rejection markers cannot store a count, so when a FILE-type rejection marker is used with `max_rejections`, the marker is simply deleted.
- Supports revision/review loops where an agent's output is rejected and must be redone.

```json
"markers": {
  "rejection": {"type": "json", "name": ".rejection", "content": {}},
  "give_up": {"type": "file", "name": ".gave_up"}
},
"max_rejections": 5
```

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
- **conversation_queue**: Writes JSON to a queue directory (Serendipity-compatible), wired from config via `action_handler`
- **command**: Runs a shell command, captures stdout/stderr/exit code
- **subprocess**: Runs a Python script as a subprocess
- **http_request**: Makes HTTP requests via `urllib` with auth token resolution
- **custom**: Calls a user-provided Python callable

A `custom` action callable (referenced via `params.callable`) receives `(action, context)` and may return a full `ActionResult` object (including a `data` dict), which is passed through unchanged. Returning `data: {"async": true}` signals that the action is asynchronous: the pipeline defers completion marker creation to the external agent and creates a processing marker instead.

```python
from cronpypeline import ActionResult

def my_async_action(action, context):
    # ... queue work with an external agent ...
    return ActionResult(success=True, data={"async": True, "queue_file": "/path/to/queue/entry.json"})
```

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

Custom: Register a Python callable that receives an enriched context dict and returns `bool`. The context includes `target`, `target_dir`, `workspace_dir`, `target_config`, and all flattened target config keys:

```json
{
  "type": "custom",
  "callable": "my_module.my_trigger_function"
}
```

```python
# my_module.py
def my_trigger_function(context):
    # context contains: target, target_dir, workspace_dir, target_config,
    #   plus all flattened target_config keys (e.g. context["slug"], context["test_cmd"])
    return True  # or False
```

### Built-in plugins

#### Conversation queue (`cronpypeline.plugins.conversation_queue`)

Writes JSON files to a conversation queue directory. Compatible with Serendipity's `conversation_queue_monitor`.

**Wired from config** — no manual registration needed:

```json
"action_handler": {
  "type": "conversation_queue",
  "params": {
    "queue_dir": "/path/to/conversation_queue",
    "agent_settings_dir": "/path/to/agent_settings",
    "prompt_field": "content",
    "default_fields": {
      "sender": "SWE_PIPELINE",
      "conversation_id": "",
      "folder_name": "SWE",
      "model_name": "default_model",
      "runs_left": 3
    },
    "flatten_agent_settings": true
  }
}
```

Or manually:

```python
from cronpypeline.plugins.conversation_queue import ConversationQueueHandler
from cronpypeline import register_handler, ActionType

handler = ConversationQueueHandler(
    queue_dir="/path/to/conversation_queue",
    agent_settings_dir="/path/to/agent_settings",
    prompt_field="content",
    default_fields={"sender": "SWE_PIPELINE", "folder_name": "SWE", "runs_left": 3},
    flatten_agent_settings=True,
)
register_handler(ActionType.QUEUE_AGENT, handler)
```

**Features**:
- Template variables in prompts: `{target}`, `{target_dir}`, `{workspace_dir}`, plus all flattened target config keys
- Reminder prompts: `reminder_prompt` or `reminder_prompt_template` used when `retry_count > 0`
- **Configurable prompt field**: `prompt_field` (default `"prompt"`) — set to `"content"` for Serendipity compatibility
- **Default fields**: Static fields injected into every queue entry (e.g. `sender`, `conversation_id`, `folder_name`, `model_name`, `runs_left`)
- **Flatten agent settings**: When `flatten_agent_settings=True`, agent settings are merged flat into the entry instead of nested under `agent_config`
- **Runs left decrement**: On retry, `runs_left` is decremented by `retry_count` (clamped to 0)
- **Conversation ID continuation**: On retry, the previous `entry_id` from the processing marker is reused as `conversation_id` and `id` — agents continue the same conversation
- Agent settings loading: if `agent_settings_dir` is set, loads `{agent}.json` config into the queue entry
- Queue file tracking: returns `queue_file` and `entry_id` in `result.data`, which the pipeline writes into the processing marker for stale detection
- Optional params: `model`, `temperature`, `max_tokens`

**Default queue entry format** (backward compatible):
```json
{
  "id": "uuid",
  "agent": "CoderAgent",
  "prompt": "Fix issue 42...",
  "target": "my-repo",
  "timestamp": 1234567890.0,
  "model": "gpt-4",
  "temperature": 0.7,
  "max_tokens": 4096
}
```

**Serendipity-compatible format** (with `prompt_field`, `default_fields`, `flatten_agent_settings`):
```json
{
  "id": "uuid",
  "agent": "CoderAgent",
  "content": "Fix issue 42...",
  "sender": "SWE_PIPELINE",
  "conversation_id": "",
  "folder_name": "SWE",
  "model_name": "gpt-4",
  "temperature": 0.5,
  "runs_left": 3,
  "user_name": "coder",
  "target": "my-repo",
  "timestamp": 1234567890.0
}
```

#### HTTP request handler

Makes HTTP requests using `urllib` from the stdlib. Supports `GET`, `POST`, `PATCH`, `PUT`, `DELETE` methods, custom headers, request body, and auth token resolution. Only `http` and `https` URL schemes are accepted — requests to other schemes (e.g. `file://`) are rejected with an error.

```json
{
  "type": "http_request",
  "params": {
    "url": "https://api.github.com/repos/{slug}/issues",
    "method": "GET",
    "headers": {"Accept": "application/vnd.github+json"},
    "auth_token_env": "GITHUB_TOKEN"
  }
}
```

The URL reported in the result data (`result.data['url']`) is redacted — userinfo (credentials) and query parameters are removed — to avoid leaking sensitive information.

**Auth token resolution** (in order):
1. `auth_token` — direct value (supports template variables)
2. `auth_token_env` — environment variable name (checked in `context.env` then `os.environ`)

When an auth token is resolved, it's sent as `Authorization: token <value>` header.

#### Report writing utilities (`cronpypeline.reporting`)

Utility functions for writing timestamped reports and managing "latest" symlinks. Used by custom action handlers that need to produce structured output:

```python
from cronpypeline.reporting import write_report, update_latest_symlink, format_report, ReportConfig

# Write a timestamped report
path = write_report(directory=target_dir / "reports", filename="report_{timestamp}.md", content="# Results\n...")

# Update latest.md symlink to point to the new report
update_latest_symlink(directory=target_dir / "reports", symlink_name="latest.md", target_name=path.name)
```

#### SWE pipeline plugins

**SWE plugin** (`cronpypeline.plugins.swe_plugin`):

Custom triggers and actions for the SWE pipeline:

- `detect_open_issue` — trigger: fires if there's an open issue in `.SWE/issues/*.md` (YAML frontmatter)
- `detect_agent_forgot_marker` — trigger: fires when queue is empty + git commits exist but no completion marker
- `cleanup_git_branch` — action: cleans up git branches after failure
- `reset_issue_status` — action: resets issue status to "open" after failure (updates YAML frontmatter)
- `sync_session_mode` — pre_tick hook: syncs `.SWE/github_session.json` to the pipeline `mode_file`

Git is invoked via `shutil.which("git")` (resolved to an absolute path, falling back to `"git"`), so the binary location is detected at runtime rather than hardcoded.

**SWE issue store** (`cronpypeline.plugins.issue_store`):

Issue store with YAML frontmatter in `.SWE/issues/*.md` files. Provides `Issue` dataclass, `load_issues()`, `get_issue()`, `set_issue_status()`, `create_issue()`, `finalize_issue_outcome()`, and a built-in YAML frontmatter parser/serializer (no external YAML dependency).

**SWE diagnostics** (`cronpypeline.plugins.swe_diagnostics`):

Diagnostic report action handler and output parsers:

- `run_diagnostic` — custom action: runs a command, parses output, writes a timestamped markdown report, creates `latest.md` symlink
- Output parsers: `parse_pytest_output`, `parse_ruff_output`, `parse_mypy_output`, `parse_pydocstyle_output`, `parse_vulture_output`, `parse_coverage_output`, `parse_bandit_output`, `parse_pip_audit_output`, `parse_radon_output`

`run_diagnostic` shell-quotes template variables (`target`, `target_dir`, `workspace_dir`, and flattened target config values) with `shlex.quote()` before substituting them into the diagnostic command. Command config values (e.g. `test_cmd`, `lint_cmd`, `typecheck_cmd`, `security_cmd`, `deadcode_cmd`, `build_cmd`, `dep_audit_cmd`, `coverage_cmd`) are validated to reject shell metacharacters, and commands are executed via an argument list (`shell=False`). If template substitution fails (missing key, bad format, etc.), an error result is returned instead of silently running the unformatted command.

> **⚠️ Breaking change:** `run_diagnostic` commands now run with `shell=False`, so any existing diagnostic command that relies on shell features — pipes, redirections, `&&`, `;`, command substitution (`$(...)`), globbing, or env-var expansion — must be wrapped in `sh -c '...'` (e.g. `"command": "pytest -q | tee out.txt"` must become `"command": "sh -c 'pytest -q | tee out.txt'"`).

```json
{
  "type": "custom",
  "params": {
    "callable": "cronpypeline.plugins.swe_diagnostics.run_diagnostic",
    "command": "{test_cmd}",
    "report_dir": ".SWE/reports/test-infra",
    "parser": "cronpypeline.plugins.swe_diagnostics.parse_pytest_output",
    "report_name": "test-infra_{timestamp}.md"
  }
}
```

**SWE prompt builders** (`cronpypeline.plugins.swe_prompts`):

Custom action callables that build prompts programmatically and queue agents:

- `queue_fix_agent` — reads a diagnostic report, builds a fix prompt, queues via `ConversationQueueHandler`
- `queue_coder_agent` — reads an issue from the issue store, builds a coder prompt with git state, queues
- `queue_review_agent` — builds a review prompt with cycle numbers, diff stats, PR state, queues
- Prompt builders: `build_fix_prompt`, `build_coder_prompt`, `build_review_prompt`

`queue_fix_agent`, `queue_coder_agent`, and `queue_review_agent` mark their results as async (`data: {"async": true}`), so they do not create a completion marker immediately — the pipeline creates a processing marker instead and defers completion to the external agent.

**SWE pipeline config**: A full example config is available at `configs/swe_pipeline.json` with all SWE stages (A1–A9 diagnostics, fix agents, B1, C-select/gate/code/publish/pr-review/pr-status/session-terminal/stale).

#### VNN pipeline plugin (`cronpypeline.plugins.vnn_plugin`)

Custom hooks for the VNN pipeline:

- `log_rejection` — post_tick hook: appends to `.VNN/rejection_log.json` (append-only audit trail with timestamps, reasons, rejection metadata)
- `queue_empty_global` — pre_tick hook: returns `False` (skip tick) when conversation queue is not empty (global queue-empty gate)
- `sync_story_states` — pre_tick hook: syncs `.VNN/ranking.json` with filesystem state (article, published, rejected markers)
- `cleanup_inconsistent_state` — pre_tick hook: removes stale processing markers when completion marker also exists
- `check_completed_compilations` — pre_tick hook: checks for `.compilation_complete` markers and updates compilation state
- `cleanup_stale_compilation_markers` — pre_tick hook: removes compilation markers older than the configured timeout
- `discover_stories` — pre_tick hook: scans workspace for story directories and writes a registry file
- `vnn_pre_tick` — composite pre_tick hook: runs all VNN pre_tick hooks in sequence (queue empty gate, story discovery, state sync, cleanup, compilation checks). Returns `False` if any hook returns `False`.
- `vnn_post_tick` — composite post_tick hook: runs all VNN post_tick hooks in sequence (rejection audit trail)

```json
"pre_tick": {"callable": "cronpypeline.plugins.vnn_plugin.vnn_pre_tick"},
"post_tick": {"callable": "cronpypeline.plugins.vnn_plugin.vnn_post_tick"}
```

**VNN pipeline config**: A full example config is available at `configs/vnn_pipeline.json` with all VNN story-level stages (research, writing, publishing, revision) in a detector chain that prioritizes revision before publishing, with composite hooks, target locking, registry-based targets, and Serendipity-compatible queue entries.

## Package structure

```
cronpypeline/
├── __init__.py              # Public API exports
├── __main__.py              # python -m cronpypeline entry point
├── pipeline.py              # Pipeline class, tick() orchestration, chaining, stale handling
├── config.py                # PipelineConfig, Stage, TriggerCondition, ActionSpec, HookConfig
├── state.py                 # PipelineState, StageState, TargetState — marker resolution
├── lock.py                  # FileLock (fcntl-based, non-blocking)
├── markers.py               # MarkerSpec, marker creation/reading/deletion, template substitution
├── triggers.py              # Built-in trigger condition evaluators, custom callable resolution
├── actions.py               # ActionHandler, TickContext, ActionResult, built-in handlers
├── targets.py               # Target, load_targets, load_targets_with_config
├── cli.py                   # argparse CLI entry point
├── reporting.py             # Report writing, symlink management
└── plugins/
    ├── __init__.py
    ├── conversation_queue.py  # Serendipity conversation queue handler
    ├── swe_plugin.py          # SWE pipeline triggers, actions, session adapter
    ├── issue_store.py         # SWE issue store with YAML frontmatter
    ├── swe_diagnostics.py     # SWE diagnostic report handler + output parsers
    ├── swe_prompts.py         # SWE prompt builders for fix/coder/review agents
    └── vnn_plugin.py          # VNN pipeline hooks (rejection log, story sync, etc.)
configs/
├── swe_pipeline.json         # Full SWE pipeline example config
└── vnn_pipeline.json         # Full VNN pipeline example config
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

The test suite includes **1144 tests** covering:
- Unit tests for each core class (config parsing, marker resolution, trigger evaluation, lock acquisition, action execution)
- Integration tests using temp directories as workspaces, simulating multi-tick execution
- Crash safety tests verifying state recovery from partial filesystem state
- Plugin tests for conversation queue (Serendipity format, conversation ID continuation, runs_left decrement)
- SWE plugin tests (issue store, diagnostic parsers, prompt builders, session adapter, pipeline config)
- VNN plugin tests (rejection audit trail, queue-empty gate, story sync, state cleanup)
- VNN config tests (stage ordering, markers, hooks, action handler, target lock)
- E2E migration tests (multi-tick VNN flow, rejection/revision loop, give-up, queue entry format validation, SWE + VNN dry-run validation)

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

`tick_all()` continues processing remaining targets even if one raises an exception. Exceptions are captured as `ACTION_FAILED` `TickResult`s (one per failing target) with the traceback in the `stderr` field, so a single failure does not stop the rest of the batch.

### TickResult

```python
from cronpypeline import TickResult, TickResultStatus

# Status values:
# - ACTION_EXECUTED  — action ran successfully
# - ACTION_FAILED    — action failed
# - NO_WORK          — nothing to do
# - DRY_RUN          — would have executed (dry-run mode)
# - GAVE_UP          — stage exhausted retries or rejections
# - LOCK_FAILED      — could not acquire lock (FileLock.__enter__ raises RuntimeError)
# - DISABLED         — pipeline disabled

result.target       # "my-repo"
result.stage_id     # "A0"
result.status       # TickResultStatus.ACTION_EXECUTED
result.message      # "Executed Step 1"
result.stdout       # command output
result.stderr       # command error output
result.chained_stages  # ["A1", "A2"] if chaining occurred
result.failed_chained_stages  # ["A2"] chained stage IDs whose actions failed
```

The string representation of a `TickResult` (`str(result)`) includes the `stderr` output when present, so captured tracebacks are shown to the user.

### Full public API

```python
from cronpypeline import (
    # Core
    Pipeline, TickResult, TickResultStatus,
    # Config
    PipelineConfig, Stage, TriggerCondition, TriggerType,
    ActionSpec, ActionType, MarkerSpec, TargetSpec, TargetType,
    ActionHandlerConfig,
    # State
    PipelineState, StageState, TargetState,
    # Markers
    MarkerType, create_marker, read_marker, marker_exists, delete_marker,
    # Actions
    ActionHandler, TickContext, ActionResult, execute_action, register_handler,
    # Triggers
    evaluate_trigger,
    # Targets
    load_targets, load_targets_with_config, Target,
    # Lock
    FileLock,
)
```

## Migration path

Existing pipelines can be migrated incrementally:

1. **Phase 1**: Install cronpypeline alongside existing scripts. Create JSON configs that replicate the current detector chains. Run via `--dry-run` to verify parity.
2. **Phase 2**: Switch crontab entries from the old scripts to `python -m cronpypeline --config ...`.
3. **Phase 3**: Remove old orchestrator scripts. Custom logic moves to plugin callables.

### Migrating commands that use shell features

Commands are now executed with `shell=False` (via `shlex.split()`), a **breaking change** for configs that rely on shell features. Before migrating an existing config, audit every `command`-type action and every `run_diagnostic` command for shell syntax — pipes (`|`), redirections (`>`, `>>`, `<`, `2>&1`), command chaining (`&&`, `;`), command substitution (`$(...)`), globbing, or environment variable expansion.

If a command uses any of these, wrap it in `sh -c '...'`:

Before (worked with `shell=True`, **no longer works**):
```json
{"type": "command", "params": {"command": "echo failed > cleanup.txt"}}
{"type": "command", "params": {"command": "echo on_fail_failed >&2 && false"}}
```

After (wrap in `sh -c '...'`):
```json
{"type": "command", "params": {"command": "sh -c 'echo failed > cleanup.txt'"}}
{"type": "command", "params": {"command": "sh -c 'echo on_fail_failed >&2 && false'"}}
```

Commands without shell features do not need changes — they continue to work as-is.

## Requirements

- Python 3.10+
- No required third-party packages (stdlib only: `json`, `os`, `fcntl`, `subprocess`, `argparse`, `pathlib`, `datetime`, `typing`)
- Optional: `pydantic` for config validation

## License

MIT
