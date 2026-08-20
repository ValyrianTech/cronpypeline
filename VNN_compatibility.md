# VNN Pipeline → cronpypeline Compatibility Analysis

> Analysis of how the Valyrian News Network (VNN) article pipeline maps to cronpypeline's abstractions, including supported features, partial matches, and gaps.

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

---

## Partially Supported (⚠️)

| VNN Feature | cronpypeline Status | Workaround |
|---|---|---|
| **Pipeline enabled/disabled toggle** | `config_file` field exists in `PipelineConfig` but is never checked in `pipeline.py`'s tick logic | Add a check in `_tick_inner()` — trivial fix |
| **Queue empty as global pre-condition** | `queue_empty` exists as a per-stage trigger, not a pipeline-level gate | Add `queue_empty` condition to every stage's trigger via `and`, or add pipeline-level pre-condition |
| **Ranking as inline subprocess** | `subprocess` action type exists, but the trigger ("≥2 new unranked stories") needs custom logic | Custom trigger callable that counts unranked stories |
| **Compilation threshold detection** | No built-in "count files in dir, compare against metadata" trigger | Custom trigger callable |
| **Dynamic target discovery** | Targets are config-driven (static/registry/single). VNN discovers stories by scanning directories | Custom target loader or treat each story as a target via registry file updated externally |
| **Post-success cleanup** | `on_fail` exists but no `on_success` hook | Use `produces` markers or custom action that includes cleanup |

---

## Not Supported (❌) — Key Gaps

### 1. Active Story Lock (cross-stage target locking)

**VNN behavior**: `.active_story` lock file ensures one story flows through the entire pipeline (research → writing → publishing → revision) before the next story starts. Lock tracks `story_id`, `story_dir`, `stage`. Released only on `published.json` or `.gave_up`.

**cronpypeline**: No concept of a cross-stage target lock. Each target is processed independently per tick. `first_actionable_stage` finds the first actionable stage for whichever target has work — it doesn't prioritize a "locked" target.

**Impact**: **High**. Without this, multiple stories could be partially processed simultaneously, which is exactly what VNN's lock prevents. Could be partially worked around with custom triggers that check for a lock file, but the pipeline's target selection logic (`get_target_with_work`) would need to prioritize locked targets.

### 2. Incomplete Task Detection (queue-file-based, not time-based)

**VNN behavior**: When `.processing` exists AND the queue file is gone (agent finished) AND expected output doesn't exist → immediately send reminder. No waiting for timeout.

**cronpypeline**: Stale detection is purely time-based (`timeout_minutes`). No mechanism to check if the queue file still exists or if the expected output was created. The `_handle_stale()` method just re-executes after timeout expires.

**Impact**: **Medium-High**. VNN's approach enables faster retries (seconds vs. 30 minutes). The `check_incomplete_tasks()` function in VNN is ~180 lines of logic that checks queue file existence, validates output, and handles retry/give-up — this is fundamentally different from cronpypeline's timeout approach.

### 3. Reminder Prompts with Conversation ID Continuation

**VNN behavior**: On retry, sends a different prompt (`reminder_prompt` instead of `prompt`) using the same `conversation_id` so the agent has context from the previous attempt.

**cronpypeline**: `_handle_stale()` re-executes the same action with the same prompt. `ConversationQueueHandler` doesn't support `conversation_id` continuation or different prompts for retries.

**Impact**: **Medium**. The reminder prompt is important for agent effectiveness — the agent knows it failed and what to do differently. Would need enhancements to both the pipeline (pass retry context to action handler) and the conversation queue plugin (support `conversation_id`).

### 4. State Synchronization with External File (ranking.json)

**VNN behavior**: `sync_story_states()` reads file existence across all story directories and updates `ranking.json` story states (`pending` → `researching` → `writing` → `publishing` → `published` → `discarded`). Handles duplicate directories by using the most advanced state.

**cronpypeline**: No mechanism to sync derived state back to an external state file. State is read-only derived from markers.

**Impact**: **Medium**. This is VNN-specific business logic, but the pattern of syncing pipeline state to an external tracking file is common. Could be a custom action, but there's no pre-tick or post-tick hook to run it.

### 5. Inconsistent State Cleanup (marker conflict resolution)

**VNN behavior**: `cleanup_inconsistent_state()` fixes stories where both `article.md` and `rejected-article.md` exist (keeps whichever is newer).

**cronpypeline**: No conflict detection between markers. Each marker is checked independently.

**Impact**: **Low-Medium**. Could be handled with a custom pre-tick action, but cronpypeline doesn't have pre-tick hooks.

### 6. Rejection-Based Give-Up (separate counter from retries)

**VNN behavior**: `MAX_REJECTION_COUNT=5` for the publish→revision loop, separate from `MAX_RETRY_COUNT=3` for agent failures. Rejection count tracked in `rejection_log.json`.

**cronpypeline**: Only `max_retries` per stage. No concept of a separate rejection counter or a counter derived from a log file.

**Impact**: **Medium**. Would need custom trigger logic to count rejections from `rejection_log.json` and a custom give-up mechanism.

### 7. Rejection/Revision Loop (circular stage flow)

**VNN behavior**: Publisher can reject an article → revision agent fixes it → publisher reviews again. This is a loop: publish → reject → revise → publish, up to 5 times.

**cronpypeline**: Stages are strictly linear (array order, forward-only). The detector chain walks forward and never loops back. There's no mechanism for a stage to send work back to a previous stage.

**Impact**: **High**. This is a fundamental architectural difference. The revision loop is core to VNN's quality control. Would require either:
- A new "loop" or "goto" stage directive
- Or modeling revision as a separate stage that appears later in the chain (which is what VNN effectively does with its priority system), but the give-up logic tied to rejection count would still need custom work.

### 8. Queue File Path Tracking in Processing Marker

**VNN behavior**: `.processing` marker stores `queue_file` path. The pipeline checks if the queue file still exists to determine if the agent is still running.

**cronpypeline**: Processing marker is created by the pipeline, not the action handler. The queue file path returned by `ConversationQueueHandler.execute()` (in `result.data["queue_file"]`) is not written into the processing marker.

**Impact**: **Medium**. This is what enables incomplete task detection (gap #2). Would need the pipeline to write the queue file path into the processing marker after action execution.

### 9. No Pre-Tick / Post-Tick Hooks

**VNN behavior**: Several operations run before the main stage detection:
- `check_completed_compilations()` — clean up completed compilation markers
- `cleanup_stale_compilation_markers()` — remove stale compilation markers
- `sync_story_states()` — sync ranking.json with filesystem
- `cleanup_inconsistent_state()` — fix conflicting markers

**cronpypeline**: No pre-tick or post-tick hook mechanism. The tick goes straight to state derivation → stage detection → action execution.

**Impact**: **Medium**. These cleanup/sync operations are important for pipeline health. Could be done as a "stage 0" with a custom action that always runs, but that would consume the one-action-per-tick budget.

---

## Summary

| Category | Count | Verdict |
|---|---|---|
| Fully supported | 15 | Direct mapping |
| Partially supported | 6 | Workable with custom plugins or minor enhancements |
| Not supported | 9 | 3 high-impact, 4 medium-impact, 2 low-medium-impact |

### Three highest-impact gaps blocking a clean migration

1. **Active story lock** — requires pipeline-level target locking across stages
2. **Circular stage flow (revision loop)** — requires non-linear stage progression
3. **Incomplete task detection** — requires queue-file-based staleness, not just time-based

### Recommended cronpypeline enhancements for VNN compatibility

1. **Pipeline-level `config_file` enabled check** — trivial to add, just check the file in `_tick_inner()`
2. **Pre-tick hook** — a configurable action that runs before stage detection (for cleanup/sync)
3. **Processing marker enhancement** — write `queue_file` path and `expected_output` into processing marker after action execution
4. **Queue-file-based stale detection** — check if queue file is gone as an alternative to time-based staleness
5. **Retry prompt support** — allow stages to define a `reminder_prompt` for retries
6. **Cross-stage target lock** — a new marker type or pipeline feature that prioritizes a locked target
7. **Circular/loop stage support** — allow stages to redirect to previous stages (or model as priority-based detection with rejection counting)
