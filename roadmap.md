# cronpypeline Roadmap: VNN + SWE Compatibility

> Feature analysis derived from comparing the VNN and SWE pipeline implementations against cronpypeline's current capabilities. Sources: `VNN_compatibility.md`, `SWE_compatibility.md`, `VNN_PIPELINE.md`, `SWE_PIPELINE.md`, `PRD.md`, and the cronpypeline source code.
>
> **Implementation status**: Tiers 1–4 are fully implemented (316 tests passing). Tier 5 features require no new core code — they are modelable with existing features plus custom callables.

---

## Tier 1: Foundation Fixes (trivial, unblock everything)

Small fixes to existing code that's either stubbed, broken, or missing a wiring step.

### 1.1 `config_file` enabled check ✅

**Status**: ✅ **Implemented.** `_tick_inner()` reads the JSON config file at the top of each tick. If `{"enabled": false}`, returns `TickResult(status=DISABLED)`.

**Relevant**: `cronpypeline/pipeline.py` (`_tick_inner` — config_file check)

### 1.2 Wire up `ConversationQueueHandler` from config ✅

**Status**: ✅ **Implemented.** `Pipeline.__init__()` instantiates handlers from `ActionHandlerConfig` via a factory mapping. `"conversation_queue"` → `ConversationQueueHandler(**params)`. Custom handler types can also be registered.

**Relevant**: `cronpypeline/pipeline.py` (`Pipeline.__init__` — action handler wiring)

### 1.3 Implement `http_request` action handler ✅

**Status**: ✅ **Implemented.** `HttpRequestActionHandler` in `actions.py` supports `url`, `method` (GET/POST/PATCH), `headers`, `body`, `auth_token` (resolved from env). Uses `urllib.request` from stdlib. Registered in `_HANDLERS`.

**Relevant**: `cronpypeline/actions.py` (`HttpRequestActionHandler`)

---

## Tier 2: Rich Context & Dynamic Capabilities

These unlock the majority of both pipelines' functionality. Currently everything is too static.

### 2.1 Enriched `TickContext` ✅

**Status**: ✅ **Implemented.** `load_targets_with_config()` returns `Target` objects with `name` + `config`. `TickContext` has `target_config: dict` field. Custom triggers receive enriched context including `target_config`. Target config keys are flattened into the trigger context dict for direct access.

**Relevant**: `cronpypeline/targets.py` (`load_targets_with_config`, `Target`), `cronpypeline/actions.py` (`TickContext.target_config`), `cronpypeline/triggers.py` (`_eval_custom` — enriched context)

### 2.2 Dynamic marker naming ✅

**Status**: ✅ **Implemented.** `MarkerSpec.name` and `MarkerSpec.directory` support template variables (`{target}`, `{target_dir}`, `{workspace_dir}`, plus flattened `target_config` keys). `_format_template()` helper resolves templates at marker creation/read time. All marker functions (`create_marker`, `read_marker`, `marker_exists`, `delete_marker`, `marker_age_seconds`) accept an optional `context` dict. `_build_marker_context()` in `pipeline.py` constructs the context with flattened target config keys.

**Relevant**: `cronpypeline/markers.py` (`_format_template`, `resolve_path`, `resolve_target`), `cronpypeline/pipeline.py` (`_build_marker_context`)

### 2.3 Dynamic symlink targets ✅

**Status**: ✅ **Implemented.** `MarkerSpec.resolve_target()` accepts a context dict and performs template substitution on the symlink target path. Covered by the same `_format_template()` mechanism as dynamic marker naming (2.2).

**Relevant**: `cronpypeline/markers.py` (`resolve_target` — context-aware template substitution)

### 2.4 Dynamic prompt templates ✅

**Status**: ✅ **Implemented.** `ConversationQueueHandler` now includes `target_config` and all flattened `target_config` keys in the template variable dict. Prompts and prompt templates can reference any target config field directly (e.g., `{test_cmd}`, `{slug}`, `{issue_id}`).

**Note**: Advanced template features like `{file:path}` or `{state:stage_id.field}` are not implemented — custom action handlers can build prompts programmatically for those use cases.

**Relevant**: `cronpypeline/plugins/conversation_queue.py` (template variables with flattened target_config)

---

## Tier 3: Cross-Stage & Pipeline-Level Mechanisms

These add concepts that don't exist at all in cronpypeline currently.

### 3.1 Cross-stage marker invalidation ✅

**Status**: ✅ **Implemented.** `Stage` has an `invalidates` field — a list of `MarkerSpec`s to delete after the stage's action succeeds. Markers are deleted after completion marker creation, with context-aware template substitution. Supports all marker types (FILE, JSON, SYMLINK).

```json
"invalidates": [
  {"type": "file", "name": ".SWE/reports/lint/latest.md"},
  {"type": "file", "name": "{target}_a.md"}
]
```

**Relevant**: `cronpypeline/config.py` (`Stage.invalidates`), `cronpypeline/pipeline.py` (invalidation in `_tick_single_inner` and `_try_chain`)

### 3.2 Pre-tick / post-tick hooks ✅

**Status**: ✅ **Implemented.** `PipelineConfig` has `pre_tick` and `post_tick` fields, each a `HookConfig` with a `callable` string. Pre-tick hooks run before state derivation; returning `False` skips the tick. Post-tick hooks run after the tick completes with the `TickResult`. Hooks are resolved via `resolve_custom_callable()`.

```json
"pre_tick": {"callable": "vnn_plugin.sync_story_states"},
"post_tick": {"callable": "vnn_plugin.log_result"}
```

**Relevant**: `cronpypeline/config.py` (`HookConfig`, `PipelineConfig.pre_tick/post_tick`), `cronpypeline/pipeline.py` (`_tick_single` wraps `_tick_single_inner` with hooks)

### 3.3 Pipeline-wide mode switching ✅

**Status**: ✅ **Implemented.** `PipelineConfig` has a `mode_file` field pointing to a JSON file with `{"mode": "production"}`. `Stage` has a `modes` field listing active modes. Each tick, `_get_current_mode()` reads the mode file. Stages with `modes` set are only active if the current mode matches. Stages without `modes` are always active. Missing or unreadable mode files are treated as no mode restriction.

```json
"mode_file": ".SWE/github_session.json",
"stages": [
  {"id": "C-select", "modes": ["github"], ...},
  {"id": "A0", ...}
]
```

**Relevant**: `cronpypeline/config.py` (`PipelineConfig.mode_file`, `Stage.modes`), `cronpypeline/pipeline.py` (`_get_current_mode`, mode filtering in `_tick_inner` and `_tick_single_inner`)

### 3.4 Cross-stage target lock (active story lock) ✅

**Status**: ✅ **Implemented.** `PipelineConfig` has a `target_lock: bool` field. When enabled, `TargetState` blocks all stages for a target if any stage has a processing marker. This ensures one target flows through the entire pipeline before the next target starts. `TargetState.has_processing` checks all stage states.

```json
"target_lock": true
```

**Relevant**: `cronpypeline/config.py` (`PipelineConfig.target_lock`), `cronpypeline/state.py` (`TargetState.target_lock`, `has_processing`, `first_actionable_stage`)

---

## Tier 4: Advanced State Management

These refine the existing state model to handle real-world complexity.

### 4.1 Queue-file-based stale detection ✅

**Status**: ✅ **Implemented.** `StageState.derive()` checks for a `queue_file` field in the processing marker's JSON data. If the queue file no longer exists, the stage is immediately marked stale (no waiting for timeout). If the queue file still exists, the stage is not stale (agent still working). Falls back to time-based staleness when no `queue_file` field is present. The pipeline writes `queue_file` from `result.data` into the processing marker after action execution.

**Relevant**: `cronpypeline/state.py` (`StageState.derive` — queue-file-based stale check), `cronpypeline/pipeline.py` (writes `queue_file` to processing marker)

### 4.2 Processing marker enhancement ✅

**Status**: ✅ **Implemented.** After action execution, all `result.data` fields are merged into the processing marker's JSON content. This includes `queue_file` (for stale detection), `entry_id` (for conversation continuation), and any other data the action handler returns.

**Relevant**: `cronpypeline/pipeline.py` (result.data merged into processing marker in `_tick_single_inner` and `_handle_stale`)

### 4.3 Retry prompt support ✅

**Status**: ✅ **Implemented.** `TickContext` has a `retry_count` field. `ConversationQueueHandler` checks `retry_count > 0` and uses `reminder_prompt` or `reminder_prompt_template` from action params if available, falling back to the original prompt. `_handle_stale()` passes the incremented retry count to the `TickContext`.

```json
"action": {
  "type": "queue_agent",
  "params": {
    "agent": "NewsResearchAgent",
    "prompt": "Research this story.",
    "reminder_prompt": "You previously attempted this but did not complete it. Please finish now."
  }
}
```

**Relevant**: `cronpypeline/actions.py` (`TickContext.retry_count`), `cronpypeline/plugins/conversation_queue.py` (reminder prompt logic), `cronpypeline/pipeline.py` (`_handle_stale` — passes retry_count)

### 4.4 Separate counters (rejection vs. retry) ✅

**Status**: ✅ **Implemented.** `Stage` has a `max_rejections` field. A `rejection` marker role (JSON type) stores `rejection_count` in its content. `StageState` reads `rejection_count` from the rejection marker and sets `is_rejected`. When `rejection_count >= max_rejections`, the pipeline creates a `give_up` marker and returns `GAVE_UP`. When below max, the rejection marker is cleared so the stage can be re-processed.

```json
"markers": {
  "completion": {"type": "file", "name": "done.md"},
  "rejection": {"type": "json", "name": ".rejection", "content": {}},
  "give_up": {"type": "file", "name": ".gave_up"}
},
"max_rejections": 5
```

**Relevant**: `cronpypeline/config.py` (`Stage.max_rejections`), `cronpypeline/state.py` (`StageState.rejection_count`, `is_rejected`), `cronpypeline/pipeline.py` (rejection give-up logic in `_tick_single_inner`)

### 4.5 Issue store / shared work queue

**Status**: No concept of a shared work queue. State is per-stage markers only.

SWE's entire Phase C revolves around `.SWE/issues/` — a directory of markdown files with YAML frontmatter (id, source, type, status, attempts, hivemind_score, rank, etc.). Multiple stages read from and write to this shared store.

**Fix**: This is fundamentally outside cronpypeline's per-stage marker model. Two approaches:
1. **Plugin approach**: Implement the issue store as a plugin module (`cronpypeline.plugins.issue_store`) with custom trigger callables that read/write issues. The store lives outside cronpypeline's state model. This is the pragmatic path — the issue store is SWE-specific business logic.
2. **Generalized work queue**: Add a `WorkQueue` abstraction to cronpypeline — a shared, ordered queue of work items with rich metadata that multiple stages can consume from and produce to. This is more general but significantly more complex.

**Recommendation**: Plugin approach. The issue store is too domain-specific to generalize. Enriched `TickContext` (2.1) + custom callables is sufficient.

---

## Tier 5: Complex Flow Patterns

These are the hardest gaps — they challenge cronpypeline's linear detector chain model.

### 5.1 Circular/loop stage support (revision loop)

**Status**: Stages are strictly linear (array order, forward-only). No mechanism for a stage to send work back to a previous stage.

VNN has a publish→reject→revise→publish loop (up to 5 times). The revision stage has higher priority than writing, so it naturally fires when `rejected-article.md` exists. This actually *does* work with cronpypeline's detector chain — the revision stage just needs to appear earlier in the array than the writing stage, and its trigger checks for `rejected-article.md`. The "loop" is really just the detector chain re-evaluating from the top each tick.

**Analysis**: This may not need a code change at all. The VNN revision loop is modelable as:
1. Stage "revision" (trigger: `rejected-article.md` exists, no `article.md`, no `.processing`, no `.gave_up`) — appears early in the chain
2. Stage "publishing" (trigger: `article.md` exists, no `published.json`) — appears later
3. Publishing action creates `rejected-article.md` (on rejection) → next tick, revision stage fires first

The give-up logic tied to rejection count (4.4) is the real gap, not the loop itself.

**Fix**: Likely no new feature needed beyond separate rejection counters (4.4). The detector chain already supports this pattern naturally. Document it as a pattern.

### 5.2 Multi-state stage support (C-select/C-gate/C-wait/C-stale)

**Status**: Each stage is a single trigger→action pair. No concept of sub-states within a stage.

SWE's Phase C fix loop is a 4-way state machine: C-select (no active task + open issue → create task, queue agent), C-gate (coding_complete.marker exists → verify + merge), C-wait (active task, no marker → idle), C-stale (task >30min old → cleanup).

**Fix**: Two approaches:
1. **Multiple stages**: Model each sub-state as a separate stage in the config. C-select, C-gate, C-wait, C-stale each have their own trigger and action. They share state via the task directory (which is just files on disk). This works with cronpypeline's existing model if the triggers are expressive enough (needs enriched context — 2.1).
2. **Composite stage**: Add a `states` field to `Stage` — a dict of named sub-states, each with its own trigger and action. The pipeline evaluates the sub-states in order when the stage is reached. This is more complex but keeps related logic together.

**Recommendation**: Multiple stages (approach 1). It's more declarative, works with the existing model, and the triggers are just file-existence checks on the shared task directory. The main requirement is enriched context (2.1) for the custom triggers that need to read issue state.

### 5.3 Report-writing action handler

**Status**: `CommandActionHandler` runs a command and captures stdout/stderr/exit_code. No report generation, no output parsing, no structured markdown writing.

SWE's Phase A diagnostics (A1-A9) each run a tool, parse its output, write a structured markdown report with tables/metadata, and create a `latest.md` symlink. This is ~15-30 lines of parsing logic per stage.

**Fix**: Add a `ReportActionHandler` (or extend `CommandActionHandler`) that:
1. Runs the command (existing behavior)
2. Passes stdout to a configurable parser callable
3. Writes a timestamped markdown report from a template
4. Creates/updates a `latest.md` symlink to the new report

```json
{
  "type": "custom",
  "params": {
    "callable": "swe_plugin.run_diagnostic",
    "command": ".venv/bin/pytest -q",
    "report_dir": ".SWE/reports/test-infra",
    "parser": "swe_plugin.parse_pytest_output"
  }
}
```

Alternatively, this can be a `custom` action handler that does everything. The key is that the `produces` mechanism needs to support dynamic symlink targets (2.3) so `latest.md` can point to the timestamped report.

**Recommendation**: Implement as custom action handlers in the SWE plugin. The core library needs dynamic symlink support (2.3) but shouldn't bake in report-writing logic — that's domain-specific.

---

## Summary: Priority-Ordered Feature List

| # | Feature | Tier | Impact | Effort | Pipelines | Status |
|---|---------|------|--------|--------|-----------|--------|
| 1 | `config_file` enabled check | 1 | High | Trivial | Both | ✅ Done |
| 2 | Wire `ConversationQueueHandler` from config | 1 | High | Small | Both | ✅ Done |
| 3 | Implement `http_request` handler | 1 | High | Small | SWE | ✅ Done |
| 4 | Enriched `TickContext` (per-target config) | 2 | Critical | Medium | Both | ✅ Done |
| 5 | Pass context to custom triggers | 2 | Critical | Small | Both | ✅ Done |
| 6 | Dynamic marker naming | 2 | High | Medium | Both | ✅ Done |
| 7 | Dynamic symlink targets | 2 | High | Small | SWE | ✅ Done |
| 8 | Cross-stage marker invalidation (`invalidates`) | 3 | High | Small | Both | ✅ Done |
| 9 | Pre-tick / post-tick hooks | 3 | Medium | Medium | Both | ✅ Done |
| 10 | Pipeline-wide mode switching (`mode_file`) | 3 | Medium | Small | SWE | ✅ Done |
| 11 | Cross-stage target lock | 3 | High | Medium | VNN | ✅ Done |
| 12 | Queue-file-based stale detection | 4 | High | Medium | VNN, SWE | ✅ Done |
| 13 | Processing marker enhancement (result.data) | 4 | Medium | Small | Both | ✅ Done |
| 14 | Retry prompt support | 4 | Medium | Small | VNN | ✅ Done |
| 15 | Separate rejection counter | 4 | Medium | Medium | VNN | ✅ Done |
| 16 | Dynamic prompt templates | 2 | High | Large | Both | ✅ Done |

**All 16 features are implemented** (316 tests passing). Tiers 1–4 are complete.

The revision loop (5.1) and multi-state stages (5.2) need **no new core features** — they're modelable with the existing detector chain plus enriched context and custom callables. The issue store (4.5) should remain a plugin, not a core feature.
