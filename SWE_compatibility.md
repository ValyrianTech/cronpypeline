# SWE Pipeline vs cronpypeline: Feature Compatibility Analysis

> **Re-evaluated August 2026** after implementation of Tiers 1–4 from the roadmap and a fresh independent code review. Of the 13 gaps identified, 8 are fully implemented, 2 are partially implemented, 1 is modelable with existing features, 1 remains by design (issue store as plugin), and 1 newly identified critical gap (queue entry format mismatch) requires a plugin fix before migration is viable.
>
> **Objective**: Assess how easily the existing SWE pipeline (in `spellbook/apps/Serendipity/SWE/`) could be configured using `cronpypeline`, and whether all necessary features are supported.

---

## Table of Contents

1. [Architectural Overview](#architectural-overview)
2. [What Works: Shared Patterns](#what-works-shared-patterns)
3. [Gaps: Current Status](#gaps-current-status)
4. [Stage-by-Stage Mapping](#stage-by-stage-mapping)
5. [Summary and Recommendations](#summary-and-recommendations)

---

## Architectural Overview

### SWE Pipeline

The SWE pipeline is a **code-driven** orchestrator (`run_swe_pipeline.py`, ~5,771 lines) that:

- Runs via cron every 5 minutes, takes one action per tick, and exits.
- Iterates over repos registered in `repos.json`.
- Uses a `STAGE_DETECTORS` list of Python functions — first detector that returns non-None wins.
- Each detector returns `{stage, agent, reason, execute}` where `execute` is a closure that either runs a mechanical command or queues an LLM agent.
- State is managed through filesystem markers (symlinks, `.marker` files, issue files with YAML frontmatter).
- Mechanical Phase A stages can chain (multiple run in one tick).
- Uses `fcntl.flock` for single-instance enforcement.

### cronpypeline

cronpypeline is a **config-driven** pipeline framework that:

- Loads pipeline definitions from JSON (`PipelineConfig`).
- `Pipeline.tick()` acquires a file lock, derives state from markers, evaluates stages in array order, executes the first actionable stage's `ActionSpec`, and exits.
- Stages have `TriggerCondition`, `ActionSpec`, `markers`, `chain`, `timeout_minutes`, `max_retries`, `on_fail`, `invalidates`, `modes`, `max_rejections`.
- Supports `file_missing`, `file_exists`, `file_older_than`, `marker_state`, `queue_empty`, `custom`, `and`, `or` trigger types.
- Supports `command`, `queue_agent`, `subprocess`, `http_request`, `custom` action types — all with registered handlers.
- Markers can be `file`, `json`, or `symlink` type — with dynamic template-substituted names.
- Plugin system via `register_handler()` for custom action handlers.
- `ConversationQueueHandler` wired from config via `ActionHandlerConfig`.
- Pre-tick / post-tick hooks, pipeline-wide mode switching, cross-stage target lock.
- Per-target config passthrough with flattened keys in trigger/action context.

---

## What Works: Shared Patterns

| Feature | SWE Pipeline | cronpypeline | Status |
|---------|-------------|-------------|--------|
| **Tick-based orchestration** | One action per cron tick, exit | `Pipeline.tick()` — same model | ✅ Supported |
| **Detector chain (first match wins)** | `STAGE_DETECTORS` list, first non-None wins | Stages in array order, first actionable wins | ✅ Supported |
| **File-based state** | `latest.md` symlinks, markers, issue files | `MarkerSpec` (file/json/symlink) + `StageState` | ✅ Supported |
| **fcntl non-blocking lock** | `acquire_pipeline_lock()` using `fcntl.flock` | `FileLock` class — identical pattern | ✅ Supported |
| **Chaining** | Mechanical Phase A stages chain in same tick | `chain: true` on stages, chains non-queue_agent actions | ✅ Supported |
| **Dry-run / verbose** | `--dry-run`, `--verbose` CLI flags | Same CLI flags | ✅ Supported |
| **Multiple targets** | `repos.json` registry, `--repo`, `--all` | `registry`/`static`/`single` target specs, `--target`, `--all` | ✅ Supported |
| **Retry + give-up** | `MAX_ATTEMPTS` (3), `TASK_TIMEOUT_MINUTES` (30) | `max_retries`, `timeout_minutes` → give_up marker | ✅ Supported |
| **on_fail rollback** | Git branch cleanup, issue reset | `on_fail` action spec | ✅ Supported |
| **Plugin system for actions** | `queue_agent()` writes to conversation queue | `register_handler()`, `ConversationQueueHandler` wired from config | ✅ Supported |
| **Custom triggers** | Detector functions with arbitrary logic | `custom` trigger type with Python callable + enriched context | ✅ Supported |
| **Template variables in prompts** | Repo paths in prompts | `{target}`, `{target_dir}`, `{workspace_dir}` + all flattened target_config keys | ✅ Supported (limited — see gap #8) |
| **Per-target configuration** | `repos.json` with per-repo commands, thresholds, GitHub config | `load_targets_with_config()` + `TickContext.target_config` + flattened keys | ✅ Supported |
| **Dynamic marker naming** | `queued_for_{stem}.marker`, `ranked_{N}.marker` | `MarkerSpec.resolve_path()` with context template substitution | ✅ Supported |
| **Cross-stage marker invalidation** | Fix agents delete `latest.md` of upstream stages | `Stage.invalidates` field — markers deleted after action success | ✅ Supported |
| **GitHub API integration** | `_gh_api_get/post/patch()` | `HttpRequestActionHandler` with auth token from env | ✅ Supported |
| **Pipeline-wide mode switching** | `.SWE/github_session.json` changes ~10 detectors | `PipelineConfig.mode_file` + `Stage.modes` filtering | ✅ Supported |
| **Queue-file-based stale detection** | Check if queue file gone → immediate stale | `StageState.derive()` checks `queue_file` in processing marker | ✅ Supported |
| **Processing marker enrichment** | Queue file path, entry ID in `.processing` | Pipeline merges `result.data` into processing marker | ✅ Supported |
| **Retry/reminder prompts** | Different prompt on retry | `TickContext.retry_count` + `reminder_prompt`/`reminder_prompt_template` | ✅ Supported |
| **Pre-tick / post-tick hooks** | Pre-detection cleanup/sync | `pre_tick` / `post_tick` hook configs in `PipelineConfig` | ✅ Supported |
| **Dynamic symlink targets** | `latest.md` → timestamped report | `MarkerSpec.resolve_target()` with context template substitution | ✅ Supported |
| **Report writing utilities** | `_write_a1_report()`, etc. | `reporting.py` module: `write_report()`, `update_latest_symlink()` | ✅ Supported (utilities only — see gap #1) |

---

## Gaps: Current Status

### Gap 0: Queue entry format mismatch ❌ (critical — newly identified)

**SWE Pipeline**: The `queue_agent()` function writes JSON files to the conversation queue directory with a format compatible with Serendipity's `conversation_queue_monitor`:

```json
{
  "agent": "CoderAgent",
  "content": "Fix issue...",
  "sender": "SWE_PIPELINE",
  "conversation_id": "",
  "folder_name": "SWE",
  "model_name": "default_model",
  "temperature": 0.5,
  "runs_left": 3
}
```

Key fields: `content` (the prompt text), `sender`, `conversation_id`, `folder_name`, `model_name`, `runs_left`.

**cronpypeline**: `ConversationQueueHandler` produces a different format:

```json
{
  "id": "uuid",
  "agent": "CoderAgent",
  "prompt": "Fix issue...",
  "target": "my-repo",
  "timestamp": 1234567890.0,
  "model": "gpt-4",
  "temperature": 0.7,
  "max_tokens": 4096
}
```

Key differences:
- **`prompt` vs `content`** — the prompt text field has a different name
- **Missing fields**: `sender`, `conversation_id`, `folder_name`, `model_name`, `runs_left` — all required by Serendipity's `conversation_queue_monitor`
- **Extra fields**: `id`, `target`, `timestamp` — not used by the monitor
- **Agent settings loading**: SWE loads `settings.json` per agent and merges `user_name`, `model_name`, `temperature` into the entry; cronpypeline loads the full settings as `agent_config` sub-object

**Current status**: ❌ **Not implemented — critical blocker.** Agents queued by cronpypeline would not be picked up by Serendipity's existing `conversation_queue_monitor`. This is a hard blocker for migration.

**Fix**: Extend `ConversationQueueHandler` to support custom field names and additional required fields, or create an SWE-specific subclass that produces the correct format. The handler needs:
1. `prompt` field renamed to `content` (or configurable field name)
2. `sender`, `conversation_id`, `folder_name`, `model_name`, `runs_left` fields added (with configurable defaults)
3. Agent settings loaded and merged as flat fields (not nested `agent_config`)

**Impact**: **Critical.** Without this fix, no agents queued by cronpypeline will be dispatched by Serendipity. This was identified during a fresh code review and was not in the original 12-gap analysis. The same issue affects the VNN pipeline (documented in `VNN_compatibility.md`).

**Relevant code**:
- SWE pipeline: `queue_agent()` in `run_swe_pipeline.py:132-170`
- cronpypeline: `ConversationQueueHandler.execute()` in `cronpypeline/plugins/conversation_queue.py:28-96`
- VNN compatibility: `VNN_compatibility.md` — "Queue entry format compatibility" in Partially Supported section

---

### Gap 1: Report generation + symlink management ⚠️

**SWE Pipeline**: Mechanical stages (A1–A9) don't just run commands — they:
- Parse tool output (pytest summaries, ruff counts, coverage %, vulture items, pip-audit vulnerabilities, etc.)
- Write structured markdown reports with tables, metadata, and parsed summaries
- Create `latest.md` symlinks pointing to the timestamped report file
- Reports are the state that downstream stages check (e.g., A2-fix-agent checks if A2 report says FAIL)

**cronpypeline**: The `command` action handler runs a command and captures stdout/stderr/exit_code. The `reporting.py` module provides utility functions (`write_report()`, `update_latest_symlink()`, `format_report()`) and dynamic symlink targets work via `MarkerSpec.resolve_target()` with context. However, there is **no built-in `ReportActionHandler`** that combines command → parse → write report → create symlink in one action.

**Current status**: ⚠️ **Partially implemented.** The building blocks are all present:
- `reporting.py` utilities for writing timestamped reports and managing symlinks
- Dynamic symlink markers via `MarkerSpec.resolve_target(context)`
- `custom` action type can call `reporting.py` utilities

The missing piece is a declarative config option that says "run command, parse output, write report, symlink latest.md." This needs a `custom` action handler that uses the `reporting.py` utilities — which is the recommended approach per the roadmap (Tier 5.3: "Implement as custom action handlers in the SWE plugin").

**Impact**: Every Phase A diagnostic stage (A1–A9) would use a `custom` action handler that calls `reporting.py` utilities. The config can express "run this custom callable" but not "run pytest, parse coverage %, write markdown report, symlink latest.md" declaratively.

**Relevant code**:
- SWE pipeline: `_write_a1_report()`, `_write_a2_report()`, `_write_a6_report()`, `_write_a7_report()`, etc. in `run_swe_pipeline.py`
- cronpypeline: `cronpypeline/reporting.py` (`write_report`, `update_latest_symlink`, `format_report`, `ReportConfig`)
- cronpypeline: `CommandActionHandler` in `cronpypeline/actions.py` — no report generation, but `CustomActionHandler` can call reporting utilities

---

### Gap 2: Complex trigger conditions ✅

**SWE Pipeline**: Triggers check things like:
- "Does the latest A2 report have `errors > 0` AND `fixable == 0`?" (parsing report content)
- "Are there ≥2 open review-sourced issues without `hivemind_score`?" (scanning issue store)
- "Is the conversation queue empty AND does the task branch have git commits but no completion marker?" (agent-forgot-marker)
- "Is a GitHub session active?" (reading session JSON)
- "Has the PR been published but not reviewed?" (checking multiple JSON markers)
- "Is coverage < 100% AND no open issues AND A1 is passing?" (multi-condition)

**cronpypeline**: Custom triggers now receive **enriched context** including `target`, `target_dir`, `workspace_dir`, `target_config`, and all flattened `target_config` keys. Custom callables can read files, parse reports, check git state, scan issue directories, etc. The `and`/`or` composite triggers allow combining built-in and custom conditions.

**Current status**: ✅ **Implemented.** `_eval_custom()` passes the full context dict to the callable. Target config keys are flattened into the context for direct access (e.g., `{coverage_threshold}`, `{slug}`).

**Note**: The `swe_plugin.py` stubs still have issues:
- `detect_open_issue` reads `issues.json` (JSON format) — not the YAML-frontmatter issue store the SWE pipeline actually uses
- `detect_agent_forgot_marker` has a **bug on line 70**: `iterfile()` should be `iterdir()`
- Both need to be rewritten to match the actual SWE issue store format

**Relevant code**:
- cronpypeline: `_eval_custom()` in `cronpypeline/triggers.py:91-94` — passes enriched context
- cronpypeline: `cronpypeline/state.py:147-149` — flattens target_config keys into context
- cronpypeline: `cronpypeline/plugins/swe_plugin.py` — stubs with bugs (needs rewrite)

---

### Gap 3: Issue store with YAML frontmatter ❌ (by design)

**SWE Pipeline**: The issue store (`.SWE/issues/*.md`) is the work queue for Phase C. Each issue has:
- YAML frontmatter with `id`, `source`, `type`, `status`, `attempts`, `hivemind_score`, `rank`, `repo`, `labels`, `github_number`, `github_url`, `created_at`, etc.
- Status lifecycle: `open` → `triaged` → `done` / `discarded`
- Attempt counting with `MAX_ATTEMPTS` enforcement (3 attempts before discard)
- Source discrimination (`dep-audit`, `pipeline`, `review`, `github`) that changes pipeline behavior
- Shared module `issue_store.py` for reading/writing issues

**cronpypeline**: Has **no concept of an issue store**. Its state model is per-stage markers (completion/processing/give_up) — there's no shared work queue that multiple stages read from and write to.

**Current status**: ❌ **Not implemented — by design.** The roadmap (Tier 4.5) recommends keeping the issue store as a plugin, not a core feature. The enriched `TickContext` (gap 2) + custom callables is sufficient for reading/writing issues externally. The issue store is too domain-specific to generalize into cronpypeline's core.

**Impact**: The Phase C fix loop needs a plugin module (`cronpypeline.plugins.issue_store` or similar) with custom trigger callables that read/write issues. This lives outside cronpypeline's state model but is fully accessible via custom callables.

**Relevant code**:
- SWE pipeline: `issue_store.py` module, `load_issues()`, `set_issue_status()`, `finalize_issue_outcome()` in `run_issue_fix.py`
- cronpypeline: No equivalent — needs to be implemented as a plugin

---

### Gap 4: Dynamic marker naming ✅

**SWE Pipeline**: Uses per-report deduplication markers:
- `queued_for_{a2_report_stem}.marker` — keyed to the source report filename
- `applied_for_{a2_report_stem}.marker` — same pattern
- `ranked_{N}.marker` — keyed to the count of unranked issues
- `coverage-{sha[:8]}.md` — issue ID keyed to integration HEAD SHA

**cronpypeline**: `MarkerSpec.resolve_path()` accepts a context dict and performs template substitution on `name` and `directory` using `_format_template()`. All marker functions (`create_marker`, `read_marker`, `marker_exists`, `delete_marker`, `marker_age_seconds`) accept an optional `context` parameter. The pipeline builds context with flattened target_config keys.

**Current status**: ✅ **Implemented.** Marker names can use any context variable: `{target}`, `{target_dir}`, `{workspace_dir}`, plus all flattened target_config keys.

```json
"markers": {
  "completion": {"type": "file", "name": "queued_for_{slug}.marker"}
}
```

**Relevant code**:
- cronpypeline: `_format_template()` and `MarkerSpec.resolve_path()` in `cronpypeline/markers.py:18-71`
- cronpypeline: `_build_marker_context()` in `cronpypeline/pipeline.py:62-78`

---

### Gap 5: Cross-stage side effects (marker invalidation) ✅

**SWE Pipeline**: Stages modify other stages' state:
- A2-autofix deletes A2's `latest.md` so A2 re-runs after autofix
- A2-fix-agent deletes A1 + A2 `latest.md` so both re-run after agent fixes
- A6-fix-agent deletes A1 + A2 + A6 `latest.md` so all three re-run
- C-gate deletes A1 + A7 `latest.md` so tests/coverage re-measure after code changes
- C-pr-status deletes `pr_reviewed.json` + `pr_review_queued.json` to re-trigger C-pr-review
- C-pr-review deletes `pr_reviewed.json` to re-trigger after changes requested

**cronpypeline**: `Stage.invalidates` field — a list of `MarkerSpec`s to delete after the stage's action succeeds. Markers are deleted after completion marker creation, with context-aware template substitution. Supports all marker types (FILE, JSON, SYMLINK). Also handled during chaining.

**Current status**: ✅ **Implemented.**

```json
"invalidates": [
  {"type": "file", "name": ".SWE/reports/lint/latest.md"},
  {"type": "file", "name": ".SWE/reports/test-infra/latest.md"}
]
```

**Relevant code**:
- cronpypeline: `Stage.invalidates` in `cronpypeline/config.py:126`
- cronpypeline: Invalidation in `cronpypeline/pipeline.py:453-455` (`_tick_single_inner`) and `cronpypeline/pipeline.py:539-542` (`_try_chain`)

---

### Gap 6: GitHub API integration ✅

**SWE Pipeline**: Makes GitHub API calls for:
- **B1**: Fetching open issues with a label (`GET /repos/.../issues`)
- **C-publish**: Opening PRs (`POST /repos/.../pulls`)
- **C-pr-status**: Polling PR state, fetching reviews (`GET /repos/.../pulls/{n}`, `GET /repos/.../pulls/{n}/reviews`)
- **C-pr-review**: Posting reviews via `post_pr_review.py` CLI subprocess
- **C-session-terminal**: Closing issues, posting comments (`POST /repos/.../issues/{n}/comments`, `PATCH /repos/.../issues/{n}`)
- Token resolution: per-repo config → `SWE_GITHUB_TOKEN` env → `GITHUB_TOKEN` env → `.env` file

**cronpypeline**: `HttpRequestActionHandler` in `actions.py` supports `url`, `method` (GET/POST/PATCH), `headers`, `body`, and `auth_token` (resolved from direct value or env var via `auth_token_env`). Uses `urllib.request` from stdlib. Registered in `_HANDLERS`.

**Current status**: ✅ **Implemented.** Token resolution supports `auth_token_env` parameter — set to `"GITHUB_TOKEN"` or `"SWE_GITHUB_TOKEN"` to resolve from environment. For per-repo tokens, use `auth_token` with a template variable from target_config (e.g., `{github_token}`).

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

**Relevant code**:
- cronpypeline: `HttpRequestActionHandler` in `cronpypeline/actions.py:193-257`
- cronpypeline: Registered in `_HANDLERS` at `cronpypeline/actions.py:266`

---

### Gap 7: Per-target configuration ✅

**SWE Pipeline**: `repos.json` has rich per-repo config:
- Custom commands: `test_cmd`, `lint_cmd`, `typecheck_cmd`, `security_cmd`, `deadcode_cmd`, `coverage_cmd`, `complexity_cmd`, `dep_audit_cmd`
- Thresholds: `coverage_threshold`, `max_review_generations`, `max_review_issues_per_generation`, `max_pr_review_cycles`
- GitHub: `slug`, `issue_label`, `github_token`, `default_branch`
- Flags: `skip_deadcode`, `delivery`, `enabled`

**cronpypeline**: `load_targets_with_config()` returns `Target` objects with `name` + `config` (all registry fields except `name`). `TickContext.target_config` carries the config to action handlers. Target config keys are flattened into the trigger context dict and the action handler template variables for direct access (e.g., `{test_cmd}`, `{coverage_threshold}`, `{slug}`).

**Current status**: ✅ **Implemented.** Per-repo commands, thresholds, and GitHub config from `repos.json` are available in triggers, actions, and templates.

**Relevant code**:
- cronpypeline: `load_targets_with_config()` and `Target` in `cronpypeline/targets.py:64-101`
- cronpypeline: `TickContext.target_config` in `cronpypeline/actions.py:35`
- cronpypeline: Flattened keys in `cronpypeline/state.py:147-149` and `cronpypeline/pipeline.py:74-77`

---

### Gap 8: Dynamic prompt generation ⚠️

**SWE Pipeline**: Prompts include:
- Full report contents (lint report, coverage report, dead code report, etc.)
- Repo-specific paths and commands
- Issue details (title, body, frontmatter metadata)
- Cycle numbers for PR review ("cycle 2 of 3")
- Integration branch SHA, diff stats
- Phase A commit hints (branch name, commit message template)
- Instructions to delete `latest.md` files after committing

**cronpypeline**: `ConversationQueueHandler` now includes `target_config` and all flattened `target_config` keys in the template variable dict. Prompts and prompt templates can reference any target config field directly (e.g., `{test_cmd}`, `{slug}`, `{issue_id}`).

**Current status**: ⚠️ **Partially implemented.** Target config fields are available in prompt templates. However, advanced template features like `{file:path}` (include file contents), `{state:stage_id.field}` (reference other stage state), or dynamic git SHA insertion are **not implemented**. Prompts that need to include full report contents, issue details, or git state require a `custom` action handler that builds the prompt programmatically.

**Impact**: Medium. Simple prompts with target config fields work declaratively. Complex prompts with report contents or git state need custom action handlers — but the handler can use `format_template()` with any variables it constructs.

**Relevant code**:
- cronpypeline: `ConversationQueueHandler` template variables in `cronpypeline/plugins/conversation_queue.py:50-63`
- cronpypeline: `format_template()` in `cronpypeline/actions.py:56-61`

---

### Gap 9: Multi-state task machine (C-select / C-gate / C-wait / C-stale) ✅ (modelable)

**SWE Pipeline**: The Phase C fix loop is a state machine with 4 sub-states:
- **C-select**: No active task + open issue → create `task.json`, create task branch, queue CoderAgent
- **C-gate**: `coding_complete.marker` exists → re-run verification tools, capture diff, merge into integration branch, finalize issue status
- **C-wait**: Active task, no completion marker → idle (return action but don't execute)
- **C-stale**: Task older than 30 min → cleanup task dir, reset issue to open (or discard if max attempts), select next issue

**cronpypeline**: Still single trigger→action per stage. However, this is modelable as **multiple stages** in the config, each with its own trigger and action, sharing state via files on disk (task directory). The enriched context (gap 2) enables custom triggers that read task state.

**Current status**: ✅ **Modelable with existing features — no code change needed.** Model each sub-state as a separate stage:
- C-select: `custom` trigger checking for open issues + no active task → `custom` action creating task + queuing agent
- C-gate: `file_exists` trigger on `coding_complete.marker` → `custom` action running verification + merge
- C-stale: `custom` trigger checking task age > 30min → `custom` action cleaning up task dir

C-wait is naturally handled — if no trigger fires, the tick returns `NO_WORK`.

**Relevant code**:
- cronpypeline: `custom` trigger type with enriched context in `cronpypeline/triggers.py:91-94`
- cronpypeline: `custom` action type in `cronpypeline/actions.py:166-190`

---

### Gap 10: Pipeline-wide mode switching (GitHub session) ✅

**SWE Pipeline**: The GitHub session marker (`.SWE/github_session.json`) changes behavior across ~10 detectors simultaneously:
- C-select only picks `source: github` issues (filters out dep-audit/review issues)
- C-review uses delta scope (generation 2) instead of full-tree review
- C-review-ranking skips entirely
- C-coverage/review/publish/pr-review/pr-status all check session state
- C-session-terminal handles PR merge/close/comment

**cronpypeline**: `PipelineConfig.mode_file` field points to a JSON file with `{"mode": "production"}`. `Stage.modes` field lists active modes. Each tick, `_get_current_mode()` reads the mode file. Stages with `modes` set are only active if the current mode matches. Stages without `modes` are always active.

**Current status**: ✅ **Implemented.**

```json
"mode_file": ".SWE/github_session.json",
"stages": [
  {"id": "C-select", "modes": ["github"], ...},
  {"id": "C-review-ranking", "modes": ["default"], ...},
  {"id": "A0", ...}
]
```

The mode file format `{"mode": "github"}` maps to SWE's session file (needs a `mode` field added or a custom adapter).

**Relevant code**:
- cronpypeline: `PipelineConfig.mode_file` and `Stage.modes` in `cronpypeline/config.py:232, 127`
- cronpypeline: `_get_current_mode()` and mode filtering in `cronpypeline/pipeline.py:283-294, 219-226, 308-315`

---

### Gap 11: `ConversationQueueHandler` plugin is incomplete ✅

**SWE Pipeline**: The `queue_agent()` function writes JSON files to a conversation queue directory with agent name, prompt, folder, and extra metadata (repo_name, repo_dir, stage, report paths).

**cronpypeline**: `Pipeline.__init__()` now instantiates handlers from `ActionHandlerConfig` via `_instantiate_action_handler()` factory. `"conversation_queue"` → `ConversationQueueHandler(**params)`. The handler is registered for `ActionType.QUEUE_AGENT`. The `register()` function is intentionally a no-op — the docstring explains the actual wiring happens in `Pipeline.__init__()`.

**Current status**: ✅ **Implemented.** The handler is wired from config:

```json
"action_handler": {
  "type": "conversation_queue",
  "params": {
    "queue_dir": "/path/to/conversation_queue",
    "agent_settings_dir": "/path/to/agent_settings"
  }
}
```

**Note**: The handler returns `data={"queue_file": str(queue_file), "entry_id": entry["id"]}` which the pipeline writes into the processing marker for stale detection. Extra metadata (repo_name, repo_dir, stage) can be added by extending the handler or using a subclass.

**Relevant code**:
- cronpypeline: `_instantiate_action_handler()` in `cronpypeline/pipeline.py:54-59`
- cronpypeline: `Pipeline.__init__()` wiring in `cronpypeline/pipeline.py:92-98`
- cronpypeline: `ConversationQueueHandler` in `cronpypeline/plugins/conversation_queue.py:18-113`

---

### Gap 12: `http_request` action handler not implemented ✅

**cronpypeline**: `HttpRequestActionHandler` is now fully implemented and registered in `_HANDLERS`.

**Current status**: ✅ **Implemented.** Same as gap 6 — see above for details.

**Relevant code**:
- cronpypeline: `HttpRequestActionHandler` in `cronpypeline/actions.py:193-257`
- cronpypeline: Registered in `_HANDLERS` at `cronpypeline/actions.py:266`

---

## Stage-by-Stage Mapping

### Phase A — Diagnostics & Hygiene

| Stage | SWE Pipeline Function | Trigger Logic | Action | cronpypeline Feasibility |
|-------|-----------------------|---------------|--------|-------------------------|
| **A0** (briefing) | `detect_a0_briefing` | `repo_briefing.md` missing | Queue RepoResearchAgent | ✅ `file_missing` trigger + `queue_agent` action. Prompt is static enough. |
| **A1** (test infra) | `detect_a1_test_infra` | `latest.md` missing in test-infra reports | Mechanical: run test infra check, write report + symlink | ⚠️ `file_missing` trigger works. `custom` action handler needed for report generation + symlink (using `reporting.py` utilities). |
| **A2** (lint) | `detect_a2_lint` | `latest.md` missing in lint reports | Mechanical: run ruff, write report + symlink | ⚠️ Same as A1. |
| **A2-autofix** | `detect_a2_autofix` | A2 report exists + has fixable errors + no `applied_for_*.marker` | Mechanical: run `ruff --fix`, delete A2 `latest.md` | ✅ `custom` trigger (parse report) + dynamic marker naming + `invalidates` for `latest.md` deletion. |
| **A2-fix-agent** | `detect_a2_fix_agent` | A2 report exists + FAIL + no fixable + no `queued_for_*.marker` | Queue LintFixAgent with report content in prompt | ⚠️ `custom` trigger + dynamic marker naming + `invalidates` all work. Prompt with report contents needs `custom` action handler. |
| **A3** (docstrings) | `detect_a3_docstrings` | `latest.md` missing in docstring reports | Mechanical: run pydocstyle, write report + symlink | ⚠️ Same as A1. |
| **A3-fix-agent** | `detect_a3_fix_agent` | A3 report exists + FAIL + no `queued_for_*.marker` | Queue DocstringAgent with report content | ⚠️ Same as A2-fix-agent. |
| **A4** (typecheck) | `detect_a4_typecheck` | `latest.md` missing in typecheck reports | Mechanical: run mypy, write report + symlink | ⚠️ Same as A1. |
| **A4-fix-agent** | `detect_a4_fix_agent` | A4 report exists + FAIL + no `queued_for_*.marker` | Queue TypeFixAgent with report content | ⚠️ Same as A2-fix-agent. |
| **A5** (security) | `detect_a5_security` | `latest.md` missing in security reports | Mechanical: run bandit/pip-audit, write report + symlink | ⚠️ Same as A1. |
| **A5-fix-agent** | `detect_a5_fix_agent` | A5 report exists + FAIL + no `queued_for_*.marker` | Queue SecurityFixAgent with report content | ⚠️ Same as A2-fix-agent. |
| **A6** (deadcode) | `detect_a6_deadcode` | `latest.md` missing in deadcode reports | Mechanical: run vulture, write report + symlink | ⚠️ Same as A1. |
| **A7** (coverage) | `detect_a7_coverage` | `latest.md` missing in coverage reports | Mechanical: run pytest --cov, write report + symlink | ⚠️ Same as A1. |
| **A7-fix-agent** | `detect_a7_fix_agent` | A7 report exists + coverage < threshold + no `queued_for_*.marker` | Queue CoverageAgent with report content | ⚠️ `custom` trigger with `{coverage_threshold}` from target_config + dynamic markers + `invalidates`. Prompt needs `custom` handler. |
| **A8** (complexity) | `detect_a8_complexity` | `latest.md` missing in complexity reports | Mechanical: run radon, write report + symlink | ⚠️ Same as A1. |
| **A9** (dep audit) | `detect_a9_dep_audit` | `latest.md` missing in dep-audit reports | Mechanical: run pip-audit, write report + symlink, create issues for vulnerabilities | ⚠️ `custom` action handler for report + issue creation (issue store as plugin). |

### Phase B — GitHub Issue Intake

| Stage | SWE Pipeline Function | Trigger Logic | Action | cronpypeline Feasibility |
|-------|-----------------------|---------------|--------|-------------------------|
| **B1** (issue gathering) | `detect_b1_issue_gathering` | No active/completed GitHub session + recheck interval elapsed + token available | Mechanical: GitHub API GET, write issue to store, create session JSON | ⚠️ `http_request` action works for GitHub API. Session management + issue store writes need `custom` action handler. `file_older_than` trigger for recheck interval. |
| **B2** (TaskCompiler) | `_not_implemented` | N/A | N/A | N/A — not implemented in SWE pipeline either. |
| **B3** (Targaryen Council) | `_not_implemented` | N/A | N/A | N/A — not implemented in SWE pipeline either. |

### Phase C — Code Writing

| Stage | SWE Pipeline Function | Trigger Logic | Action | cronpypeline Feasibility |
|-------|-----------------------|---------------|--------|-------------------------|
| **C-review-ranking** | `detect_c_review_ranking` | No active task + ≥2 unranked review issues + no `ranked_{N}.marker` + not in GitHub session | Mechanical: run `run_swe_issue_ranking.py` subprocess | ✅ `custom` trigger (count issues) + dynamic marker naming (`ranked_{N}`) + `modes` for session filtering + `subprocess` action with target_config args. |
| **C-pr-status** | `detect_c_pr_status` | `pr_published.json` exists + no `pr_reviewed.json` | Mechanical: GitHub API GET PR state, handle merge/close/changes | ⚠️ `http_request` for GitHub API + `file_exists`/`file_missing` triggers. PR state parsing + conditional behavior need `custom` action handler. `invalidates` for marker cleanup. |
| **C-issue-fix** | `detect_c_issue_fix` | Active task needs gating OR stale task needs cleanup OR open issue needs selection | Queue CoderAgent (select) or run verification (gate) or cleanup (stale) | ✅ Modelable as 3 separate stages with `custom` triggers sharing task dir state. See gap #9. |
| **C-session-terminal** | `detect_c_github_session_terminal` | GitHub session completed + PR merged | Mechanical: close GitHub issue, post comment, delete session | ⚠️ `http_request` for GitHub API + `custom` trigger for session state. `invalidates` for session marker cleanup. |
| **C-coverage-issue** | `detect_c_coverage_issue` | No open issues + A1 passing + coverage < target + no pending PR review | Mechanical: create coverage issue in issue store | ⚠️ `custom` trigger (multi-condition with `{coverage_threshold}`) + `custom` action (issue store writes as plugin). |
| **C-review-issue** | `detect_c_review_issue` | No open issues + coverage ≥ target + review generations < max + no pending PR | Mechanical: create review issue in issue store | ⚠️ `custom` trigger (multi-condition with `{max_review_generations}`) + `custom` action (issue store writes). |
| **C-doc-sync** | `detect_c_doc_sync` | No open issues + no `doc_sync.json` marker + integration branch has commits | Queue DocSyncAgent | ✅ `and` conditions + `file_missing` trigger. `custom` trigger for git commit check. Prompt needs `custom` handler for dynamic content. |
| **C-pr-publish** | `detect_c_pr_publish` | No open issues + coverage ≥ target + no `pr_published.json` + review generations exhausted | Mechanical: push integration branch, create PR via GitHub API, write `pr_published.json` | ⚠️ `custom` trigger (multi-condition) + `http_request` for PR creation + `custom` action for git push + marker creation. |
| **C-pr-review** | `detect_c_pr_review` | `pr_published.json` exists + no `pr_reviewed.json` + no `pr_review_queued.json` | Queue PRReviewAgent or run `post_pr_review.py` | ⚠️ `file_exists`/`file_missing` triggers + `queue_agent` or `subprocess` action. `invalidates` for re-trigger after changes requested. |

---

## Summary and Recommendations

### Bottom Line

**The SWE pipeline is largely viable in cronpypeline, with one critical blocker.** After implementation of Tiers 1–4 from the roadmap, 12 of the 13 identified gaps have been addressed: 8 fully implemented, 2 partially implemented, 1 modelable with existing features, and 1 by design (issue store as plugin). However, a fresh code review identified a **critical queue entry format mismatch** (gap 0) that must be fixed before migration is viable — agents queued by cronpypeline would not be picked up by Serendipity's `conversation_queue_monitor`.

The core orchestration patterns (tick-based, detector chain, file lock, chaining, retries) were already aligned. The implemented features — enriched custom trigger context, per-target config passthrough, dynamic marker naming, cross-stage invalidation, `http_request` handler, pipeline mode switching, `ConversationQueueHandler` wiring, queue-file-based stale detection, pre/post-tick hooks — cover the vast majority of SWE pipeline needs.

The remaining work is writing SWE-specific **plugin code** (custom triggers, action handlers, issue store module) that uses these now-implemented capabilities, plus fixing the queue entry format mismatch. The JSON config would define the stage structure, triggers, and marker wiring, while the plugin code handles SWE-specific logic like report parsing, issue store management, and GitHub session handling.

### Feature Parity Matrix

| Category | What works | What needs plugin code |
|----------|-----------|------------------------|
| **Phase A diagnostics (A1-A9)** | `file_missing` triggers, `command` action, `reporting.py` utilities, dynamic symlinks, `invalidates` for cross-stage deletion | `custom` action handler for report generation (run tool → parse → write report → symlink) |
| **Phase A fix-agents** | `custom` triggers with enriched context, dynamic markers (`queued_for_{slug}`), `invalidates`, `queue_agent` with `reminder_prompt` | `custom` action handler for prompts with report contents |
| **Phase B (GitHub intake)** | `http_request` handler with auth token, `file_older_than` for recheck interval | `custom` action for session management + issue store writes |
| **Phase C fix loop** | `custom` triggers with task dir state, `modes` for session filtering, modelable as multiple stages | Issue store plugin (YAML frontmatter read/write) |
| **Phase C review/publish** | `http_request` for GitHub API, `file_exists`/`file_missing` triggers, `invalidates`, `subprocess` for scripts | `custom` action for PR state parsing + conditional behavior |
| **Cross-cutting** | Lock, chaining, dry-run, targets with per-target config, pipeline modes, pre/post-tick hooks, queue-file stale detection | — |
| **Agent dispatch** | `ConversationQueueHandler` wiring, `queue_file` in processing marker, retry prompts | **Queue entry format fix** — `content` vs `prompt`, missing `sender`/`conversation_id`/`folder_name`/`model_name`/`runs_left` fields |

### Remaining Work for SWE Migration

The following work is needed, ordered by priority:

1. **⚠️ CRITICAL: Queue entry format fix** — Extend `ConversationQueueHandler` (or create SWE-specific subclass) to produce entries with `content` (not `prompt`), `sender`, `conversation_id`, `folder_name`, `model_name`, `runs_left` fields. Without this, no agents will be dispatched. The same issue affects VNN.
2. **SWE report action handler** — `custom` action that runs a command, parses output, writes markdown report via `reporting.py`, creates `latest.md` symlink
3. **SWE issue store plugin** — YAML frontmatter read/write, status lifecycle, attempt counting (used by custom triggers and actions)
4. **SWE prompt builder** — `custom` action handler that builds prompts with report contents, issue details, and git state
5. **Fix `swe_plugin.py` stubs** — `detect_agent_forgot_marker` has `iterfile()` bug (line 70, should be `iterdir()`), `detect_open_issue` reads `issues.json` instead of YAML-frontmatter `.SWE/issues/*.md`, `reset_issue_status` has same format issue
6. **GitHub session adapter** — Map `.SWE/github_session.json` format to `mode_file` `{"mode": "..."}` format (or custom pre_tick hook)
