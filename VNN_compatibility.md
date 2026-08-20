# VNN Pipeline → cronpypeline Compatibility Analysis

> **Re-evaluated August 2026** after implementation of Tiers 1–4 from the roadmap. All 9 original gaps have been addressed — 7 are fully implemented, 1 is partially implemented, 1 is modelable with existing features.
>
> Analysis of how the Valyrian News Network (VNN) article pipeline maps to cronpypeline's abstractions.

## References

- **VNN pipeline docs**: `/home/wouter/Repos/spellbook/apps/Serendipity/docs/VNN_PIPELINE.md`
- **VNN pipeline script**: `/home/wouter/Repos/spellbook/apps/Serendipity/scripts/run_article_pipeline.py` (1920 lines)
- **cronpypeline source**: `/home/wouter/Repos/cronpypeline/cronpypeline/`

---

## Fully Supported (✅)

These VNN features map directly to cronpypeline abstractions:

| VNN Feature | cronpypeline Equivalent |
|---|---|
| **Tick-based orchestration** — one action per run, exit | `pipeline.tick()` takes one action and exits |
| **State from filesystem** — check file existence (`research.md`, `article.md`, etc.) | `PipelineState` derives state from markers on every tick |
| **Stage detector chain** — priority order: revision > publishing > writing > research > compilation | Stages evaluated in array order, first match wins |
| **File-based markers** — `.processing`, `.gave_up`, `published.json` | `file`/`json` marker types with `completion`/`processing`/`give_up` roles |
| **Retry tracking in processing marker** — `retry_count` in `.processing` JSON | `StageState.derive()` reads `retry_count` from processing data |
| **Time-based stale cleanup** — remove markers >30min old | `timeout_minutes` per stage + `_handle_stale()` |
| **Retry with give-up** — `MAX_RETRY_COUNT=3` → `.gave_up` marker | `max_retries` per stage + give_up marker |
| **Conversation queue dispatch** — drop JSON in `conversation_queue/` | `ConversationQueueHandler` plugin |
| **Per-stage timeouts** — 30min stories, 60min compilation | `timeout_minutes` per stage |
| **Dry run / verbose** | Both supported in `tick()` and CLI |
| **File lock** — prevents concurrent pipeline runs | `FileLock` (fcntl-based, non-blocking) |
| **Chaining** — mechanical stages complete in one tick | `chain: true` on stages with sync actions |
| **on_fail rollback** | `on_fail` action spec on stages |
| **Custom triggers/actions** — plugin system | `callable` refs in JSON config, `register_handler()` |
| **Template variables** — `{target}`, `{target_dir}`, `{workspace_dir}` | Supported in `command`, `cwd`, `prompt_template` |
| **Pipeline enabled/disabled toggle** | `config_file` checked in `_tick_inner()` — returns `DISABLED` if `{"enabled": false}` |
| **Cross-stage target lock** (active story lock) | `target_lock: true` in `PipelineConfig` — blocks all stages for a target if any stage has a processing marker |
| **Queue-file-based stale detection** | `StageState.derive()` checks `queue_file` in processing marker — immediately stale if queue file gone |
| **Queue file path in processing marker** | Pipeline merges `result.data` (including `queue_file`) into processing marker after action execution |
| **Retry/reminder prompts** | `TickContext.retry_count` + `reminder_prompt`/`reminder_prompt_template` in `ConversationQueueHandler` |
| **Pre-tick / post-tick hooks** | `pre_tick` and `post_tick` hook configs in `PipelineConfig`, resolved via `resolve_custom_callable()` |
| **Rejection-based give-up** (separate counter) | `Stage.max_rejections` + `rejection` marker role + `rejection_count` tracking in `StageState` |
| **Cross-stage marker invalidation** | `Stage.invalidates` field — list of `MarkerSpec`s to delete after stage action succeeds |
| **Dynamic marker naming** | `MarkerSpec.resolve_path()` accepts context dict, template-substitutes `{key}` placeholders in name and directory |
| **Per-target config passthrough** | `load_targets_with_config()` returns `Target` objects with `config`, flattened into `TickContext` and trigger context |
| **Ranking as inline subprocess** | `subprocess` action type + custom trigger with enriched context (can count unranked stories) |
| **Compilation threshold detection** | Custom trigger callable with enriched context (can count files, compare metadata) |
| **Post-success cleanup** | `Stage.invalidates` for marker cleanup + `post_tick` hook for arbitrary cleanup logic |

---

## Partially Supported (⚠️)

| VNN Feature | cronpypeline Status | Workaround |
|---|---|---|
| **Queue empty as global pre-condition** | `queue_empty` exists as a per-stage trigger, not a pipeline-level gate | Add `queue_empty` condition to every stage's trigger via `and`, or use `pre_tick` hook to return `False` when queue is not empty |
| **Dynamic target discovery** | Targets are config-driven (static/registry/single). VNN discovers stories by scanning directories | Use a registry file updated externally (e.g., by a `pre_tick` hook that scans directories and writes the registry) |
| **Conversation ID continuation** | `ConversationQueueHandler` creates a new UUID per queue entry. `entry_id` is stored in processing marker but not reused on retry | Custom action handler that reads `entry_id` from processing marker and reuses it for retry prompts |

---

## Previously Not Supported — Now Resolved (✅)

All 9 original gaps from the initial analysis have been addressed:

### 1. Active Story Lock (cross-stage target locking) ✅

**Original status**: ❌ Not supported

**Current status**: ✅ **Implemented.** `PipelineConfig.target_lock: bool` field. When enabled, `TargetState.first_actionable_stage` returns `None` if any stage has a processing marker (`has_processing` check). This ensures one target flows through the entire pipeline before the next target starts.

**Relevant**: `cronpypeline/config.py` (`PipelineConfig.target_lock`), `cronpypeline/state.py` (`TargetState.target_lock`, `has_processing`, `first_actionable_stage`)

### 2. Incomplete Task Detection (queue-file-based, not time-based) ✅

**Original status**: ❌ Not supported

**Current status**: ✅ **Implemented.** `StageState.derive()` checks for a `queue_file` field in the processing marker's JSON data. If the queue file no longer exists, the stage is immediately marked stale (no waiting for timeout). Falls back to time-based staleness when no `queue_file` field is present. The pipeline writes `queue_file` from `result.data` into the processing marker after action execution.

**Relevant**: `cronpypeline/state.py` (`StageState.derive` — queue-file-based stale check), `cronpypeline/pipeline.py` (writes `result.data` into processing marker)

### 3. Reminder Prompts with Conversation ID Continuation ⚠️

**Original status**: ❌ Not supported

**Current status**: ⚠️ **Partially implemented.** Reminder prompts are fully supported — `TickContext.retry_count` is passed to action handlers, and `ConversationQueueHandler` uses `reminder_prompt` or `reminder_prompt_template` when `retry_count > 0`. `_handle_stale()` passes the incremented retry count.

However, **conversation ID continuation is not implemented.** The handler creates a new UUID per queue entry. The `entry_id` from `result.data` is written to the processing marker, but it's not reused on retry. A custom action handler could read `entry_id` from the processing marker and reuse it.

**Relevant**: `cronpypeline/actions.py` (`TickContext.retry_count`), `cronpypeline/plugins/conversation_queue.py` (reminder prompt logic), `cronpypeline/pipeline.py` (`_handle_stale` — passes retry_count)

### 4. State Synchronization with External File (ranking.json) ✅

**Original status**: ❌ Not supported

**Current status**: ✅ **Implemented.** `PipelineConfig.pre_tick` hook runs before state derivation. A `sync_story_states()` callable can be registered as `pre_tick` to sync `ranking.json` with filesystem state. Returning `False` from the hook skips the tick entirely.

**Relevant**: `cronpypeline/config.py` (`HookConfig`, `PipelineConfig.pre_tick`), `cronpypeline/pipeline.py` (`_tick_single` — pre-tick hook execution)

### 5. Inconsistent State Cleanup (marker conflict resolution) ✅

**Original status**: ❌ Not supported

**Current status**: ✅ **Implemented.** `PipelineConfig.pre_tick` hook can run `cleanup_inconsistent_state()` before state derivation. The hook receives a context dict with `target`, `target_dir`, `workspace_dir`, and `target_config`.

**Relevant**: `cronpypeline/config.py` (`PipelineConfig.pre_tick`), `cronpypeline/pipeline.py` (`_tick_single` — pre-tick hook)

### 6. Rejection-Based Give-Up (separate counter from retries) ✅

**Original status**: ❌ Not supported

**Current status**: ✅ **Implemented.** `Stage.max_rejections` field (default 0 = disabled). A `rejection` marker role (JSON type) stores `rejection_count` in its content. `StageState` reads `rejection_count` from the rejection marker and sets `is_rejected`. When `rejection_count >= max_rejections`, the pipeline creates a `give_up` marker and returns `GAVE_UP`. When below max, the rejection marker is cleared so the stage can be re-processed.

```json
"markers": {
  "completion": {"type": "file", "name": "done.md"},
  "rejection": {"type": "json", "name": ".rejection", "content": {}},
  "give_up": {"type": "file", "name": ".gave_up"}
},
"max_rejections": 5
```

**Relevant**: `cronpypeline/config.py` (`Stage.max_rejections`), `cronpypeline/state.py` (`StageState.rejection_count`, `is_rejected`), `cronpypeline/pipeline.py` (rejection give-up logic in `_tick_single_inner`)

### 7. Rejection/Revision Loop (circular stage flow) ✅

**Original status**: ❌ Not supported

**Current status**: ✅ **Modelable with existing features — no code change needed.** The VNN revision loop (publish → reject → revise → publish) works naturally with cronpypeline's detector chain:

1. Stage "revision" (trigger: `rejected-article.md` exists, no `article.md`, no `.processing`, no `.gave_up`) — appears **early** in the stage array
2. Stage "publishing" (trigger: `article.md` exists, no `published.json`) — appears **later** in the array
3. Publishing action creates `rejected-article.md` (on rejection) → next tick, revision stage fires first because it's earlier in the chain

The "loop" is just the detector chain re-evaluating from the top each tick. Combined with separate rejection counters (gap 6), the give-up logic after N rejections is fully supported.

### 8. Queue File Path Tracking in Processing Marker ✅

**Original status**: ❌ Not supported

**Current status**: ✅ **Implemented.** After action execution, all `result.data` fields are merged into the processing marker's JSON content. `ConversationQueueHandler.execute()` returns `data={"queue_file": str(queue_file), "entry_id": entry["id"]}`, which the pipeline writes into the processing marker.

**Relevant**: `cronpypeline/pipeline.py` (result.data merged into processing marker), `cronpypeline/plugins/conversation_queue.py` (returns `queue_file` and `entry_id` in `result.data`)

### 9. Pre-Tick / Post-Tick Hooks ✅

**Original status**: ❌ Not supported

**Current status**: ✅ **Implemented.** `PipelineConfig` has `pre_tick` and `post_tick` fields, each a `HookConfig` with a `callable` string. Pre-tick hooks run before state derivation; returning `False` skips the tick. Post-tick hooks run after the tick completes with the `TickResult`. Hooks are resolved via `resolve_custom_callable()`.

```json
"pre_tick": {"callable": "vnn_plugin.sync_story_states"},
"post_tick": {"callable": "vnn_plugin.log_result"}
```

All four VNN pre-tick operations (`check_completed_compilations`, `cleanup_stale_compilation_markers`, `sync_story_states`, `cleanup_inconsistent_state`) can be registered as pre-tick hooks.

**Relevant**: `cronpypeline/config.py` (`HookConfig`, `PipelineConfig.pre_tick/post_tick`), `cronpypeline/pipeline.py` (`_tick_single` wraps `_tick_single_inner` with hooks)

---

## Summary

| Category | Count | Verdict |
|---|---|---|
| Fully supported | 27 | Direct mapping or implemented feature |
| Partially supported | 3 | Workable with minor workarounds |
| Not supported | 0 | All original gaps resolved |

### Remaining partial items (low impact)

1. **Queue empty as global pre-condition** — use `and` conditions on each stage or a `pre_tick` hook
2. **Dynamic target discovery** — use externally-updated registry file + `pre_tick` hook
3. **Conversation ID continuation** — `entry_id` stored in processing marker but not reused; custom handler needed

### Migration readiness

**VNN is ready for migration to cronpypeline.** All high-impact gaps are resolved. The three remaining partial items have clear workarounds and are low complexity. The revision loop, active story lock, queue-file-based stale detection, rejection counters, and pre-tick hooks — the five most critical VNN-specific features — are all fully implemented.
