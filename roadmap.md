# cronpypeline Roadmap: Full SWE + VNN Compatibility

> Derived from `SWE_compatibility.md` and `VNN_compatibility.md` (re-evaluated August 2026).
> Tiers 1–4 of the previous roadmap are fully implemented. Phases 1–3 are now also implemented
> (433 tests passing). This roadmap covers **all remaining work** needed to achieve full
> compatibility with both existing pipelines.
>
> **Status as of August 2026**: Phases 1–3 are **complete**. Phase 4 (VNN architectural gaps)
> and Phase 5 (integration & validation) remain.

---

## Phase 1: Critical Blocker — Queue Entry Format Fix ✅ Complete

> **Affects**: SWE + VNN
> **Priority**: Critical — without this, no agents queued by cronpypeline will be dispatched by Serendipity's `conversation_queue_monitor`.
> **Effort**: Small–Medium
> **Status**: ✅ **Complete** — `ConversationQueueHandler` extended with `prompt_field`, `default_fields`, `flatten_agent_settings`, and `runs_left` decrement on retry. 9 tests in `tests/test_plugins.py`.

### 1.1 Extend `ConversationQueueHandler` for Serendipity-compatible format

**Problem**: `ConversationQueueHandler` produces entries with `prompt`, `id`, `target`, `timestamp`, `model`, `max_tokens`. Serendipity's `conversation_queue_monitor` expects `content` (not `prompt`), `sender`, `conversation_id`, `folder_name`, `model_name`, `runs_left`. Agent settings are loaded as a nested `agent_config` sub-object instead of flat merged fields.

**Fix**: Add configurable field mapping and default fields to `ConversationQueueHandler`:

1. **Prompt field name**: Make the prompt field name configurable (default `prompt`, set to `content` for Serendipity compatibility)
2. **Required default fields**: Support injecting static default fields into every queue entry (`sender`, `conversation_id`, `folder_name`, `model_name`, `runs_left`)
3. **Agent settings flattening**: When loading `{agent}.json` settings, merge fields flat into the entry (e.g. `user_name`, `model_name`, `temperature`) instead of nesting under `agent_config`
4. **Config-driven**: Expose these as handler params in `action_handler` config so no code change is needed per pipeline

```json
"action_handler": {
  "type": "conversation_queue",
  "params": {
    "queue_dir": "/path/to/queue",
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

**Relevant code**: `cronpypeline/plugins/conversation_queue.py:28-96` (`ConversationQueueHandler.execute`)

### 1.2 Tests for queue entry format ✅ Complete

Tests verifying:
- Default format (backward compatibility — `prompt` field, no extra fields)
- Serendipity format (`content` field, `sender`/`conversation_id`/`folder_name`/`model_name`/`runs_left` present)
- Agent settings flattening (flat fields, not nested `agent_config`)
- `runs_left` decrement on retry entries

**Tests**: `TestConversationQueueSerendipityFormat` in `tests/test_plugins.py`

---

## Phase 2: SWE Plugin Implementation ✅ Complete

> **Affects**: SWE only
> **Priority**: High — required for SWE migration
> **Effort**: Medium–Large
> **Approach**: All SWE-specific logic lives in plugin code (`swe_plugin.py` + new modules). No core cronpypeline changes needed except Phase 1.
> **Status**: ✅ **Complete** — All sub-phases implemented and tested.

### 2.1 Fix `swe_plugin.py` stub bugs ✅ Complete

**Problem**: The existing stubs have known bugs:

- `detect_agent_forgot_marker` line 70: `iterfile()` should be `iterdir()` — `Path.iterfile()` does not exist
- `detect_open_issue` reads `.SWE/issues.json` (JSON array) — the SWE pipeline uses `.SWE/issues/*.md` with YAML frontmatter
- `reset_issue_status` reads/writes `.SWE/issues.json` — same format mismatch
- `cleanup_git_branch` accesses `context.target_dir` as attribute — custom actions receive `(action, context)` where `context` is a `TickContext` (has `.target_dir` attribute, so this may work, but should be verified)

**Fix** (all done):
1. ✅ Fixed `iterfile()` → `iterdir()` in `detect_agent_forgot_marker`
2. ✅ Rewrote `detect_open_issue` to scan `.SWE/issues/*.md` files, parse YAML frontmatter, check for `status: open`
3. ✅ Rewrote `reset_issue_status` to read the specific issue `.md` file, update frontmatter `status` field, write back
4. ✅ Verified `cleanup_git_branch` context access pattern works with `TickContext`

**Relevant code**: `cronpypeline/plugins/swe_plugin.py`

### 2.2 SWE issue store plugin ✅ Complete

**Problem**: SWE's Phase C revolves around an issue store — a directory of markdown files with YAML frontmatter (`id`, `source`, `type`, `status`, `attempts`, `hivemind_score`, `rank`, `repo`, `labels`, `github_number`, `github_url`, `created_at`). Multiple stages read from and write to this shared store. cronpypeline has no concept of a shared work queue.

**Fix** (implemented): Created `cronpypeline/plugins/issue_store.py` with:

- ✅ `load_issues(target_dir) -> list[Issue]` — scan `.SWE/issues/*.md`, parse YAML frontmatter
- ✅ `get_issue(target_dir, issue_id) -> Issue` — read single issue
- ✅ `set_issue_status(target_dir, issue_id, status) -> bool` — update frontmatter `status` field
- ✅ `create_issue(target_dir, issue_data) -> Issue` — write new issue `.md` file with frontmatter
- ✅ `finalize_issue_outcome(target_dir, issue_id, outcome) -> bool` — set final status, update `attempts`
- ✅ `Issue` dataclass with all frontmatter fields
- ✅ Built-in YAML frontmatter parser/serializer (no external YAML dependency)

**Tests**: 30 tests in `tests/test_issue_store.py`

**Relevant code**: `cronpypeline/plugins/issue_store.py`

### 2.3 SWE report action handler ✅ Complete

**Problem**: SWE's Phase A diagnostics (A1–A9) each run a tool, parse its output, write a structured markdown report with tables/metadata, and create a `latest.md` symlink. `CommandActionHandler` only captures stdout/stderr/exit_code. The `reporting.py` utilities exist but nothing wires them together.

**Fix** (implemented): Created `cronpypeline/plugins/swe_diagnostics.py` with:

1. ✅ `run_diagnostic` — runs the configured command, parses output, writes timestamped markdown report, creates `latest.md` symlink
2. ✅ Returns `ActionResult(success=True, data={"report_path": str(path), "exit_code": ..., "status": ..., "parsed": ...})`
3. ✅ Configurable parser via dotted path
4. ✅ Dry-run support, failed command reports, no-parser mode (raw output)

Implemented parsers: `parse_pytest_output`, `parse_ruff_output`, `parse_mypy_output`, `parse_pydocstyle_output`, `parse_vulture_output`, `parse_coverage_output`, `parse_bandit_output`, `parse_pip_audit_output`, `parse_radon_output`

**Tests**: 29 tests in `tests/test_swe_diagnostics.py`

**Relevant code**: `cronpypeline/plugins/swe_diagnostics.py`, `cronpypeline/reporting.py`

### 2.4 SWE prompt builder ✅ Complete

**Problem**: SWE agent prompts include full report contents, issue details (title, body, frontmatter), cycle numbers, integration branch SHA, diff stats, commit hints, and instructions. Target config template variables alone are insufficient for these dynamic prompts.

**Fix** (implemented): Created `cronpypeline/plugins/swe_prompts.py` with:

- ✅ `queue_fix_agent(action, context)` — reads report file, builds fix prompt, queues via `ConversationQueueHandler`
- ✅ `queue_coder_agent(action, context)` — reads issue from issue store, builds coder prompt with git state, queues
- ✅ `queue_review_agent(action, context)` — builds review prompt with cycle numbers, diff stats, PR state, queues
- ✅ Prompt builders: `build_fix_prompt`, `build_coder_prompt`, `build_review_prompt`
- ✅ Git helpers: `_get_integration_sha`, `_get_diff_stats`

**Tests**: 13 tests in `tests/test_swe_prompts.py`

**Relevant code**: `cronpypeline/plugins/swe_prompts.py`, `cronpypeline/plugins/conversation_queue.py`

### 2.5 GitHub session adapter ✅ Complete

**Problem**: SWE's `.SWE/github_session.json` has a richer format than cronpypeline's `mode_file` (`{"mode": "..."}`). The session file contains session metadata, not just a mode string.

**Fix** (implemented): Adapter hook approach — `sync_session_mode` pre_tick hook reads `.SWE/github_session.json`, checks `active` field, writes `{"mode": "github"}` or `{"mode": "default"}` to the `mode_file` path.

- ✅ Handles active/inactive sessions, missing files, corrupt JSON
- ✅ `mode_file` path configurable via `target_config`

**Tests**: 6 tests in `tests/test_session_adapter.py`

**Relevant code**: `cronpypeline/plugins/swe_plugin.py` (`sync_session_mode`), `cronpypeline/pipeline.py`

### 2.6 SWE pipeline JSON config ✅ Complete

**Problem**: No actual SWE pipeline JSON config existed yet — only the compatibility analysis.

**Fix** (implemented): Created `configs/swe_pipeline.json` with all SWE stages:
- Phase A: A1–A9 diagnostics (with `run_diagnostic` + parsers), A2-fix-agent, A6-fix-agent (with `queue_fix_agent`)
- Phase B: B1-fetch-issues (GitHub API, `github` mode)
- Phase C: C-select, C-gate, C-code (`queue_coder_agent`), C-publish (GitHub PR), C-pr-status, C-pr-review (`queue_review_agent`), C-session-terminal, C-stale (`detect_agent_forgot_marker`)
- Pre-tick hook: `sync_session_mode`
- Action handler: Serendipity-compatible `ConversationQueueHandler` config
- Mode switching, marker invalidation, `on_fail` rollback

**Tests**: 14 tests in `tests/test_swe_config.py`

### 2.7 Tests for SWE plugin ✅ Complete

- ✅ Unit tests for issue store (create, read, update status, finalize, attempt counting) — 30 tests
- ✅ Unit tests for each diagnostic parser (pytest, ruff, mypy, etc.) — 29 tests
- ✅ Unit tests for prompt builders (verify report contents, issue details, git state in prompt) — 13 tests
- ✅ Unit tests for session adapter — 6 tests
- ✅ Unit tests for pipeline config loading and structure — 14 tests
- ⏳ Integration test: multi-tick simulation of Phase A → Phase B → Phase C flow (deferred to Phase 5.3)

---

## Phase 3: VNN Plugin Implementation ✅ Complete

> **Affects**: VNN only
> **Priority**: Medium — VNN's article processing stages map well already; the remaining work is plugin code + architectural decisions
> **Effort**: Medium–Large
> **Status**: ✅ **Complete** — All sub-phases implemented and tested.

### 3.1 VNN queue entry format ✅ Complete (no additional code)

**Problem**: Same as SWE Gap 0 — VNN's `conversation_queue_monitor` expects `content` (not `prompt`), `sender`, `conversation_id`, `folder_name`, `model_name`, `runs_left`.

**Fix**: Uses the same `ConversationQueueHandler` extension from Phase 1.1 with VNN-specific defaults. No additional code needed beyond Phase 1.

### 3.2 Conversation ID continuation ✅ Complete

**Problem**: `ConversationQueueHandler` creates a new UUID per queue entry. On retry, the `entry_id` from the processing marker is not reused — the agent gets a new conversation instead of continuing the previous one.

**Fix** (implemented):
1. ✅ Added `retry_data: Optional[dict]` field to `TickContext` in `cronpypeline/actions.py`
2. ✅ `_handle_stale` in `cronpypeline/pipeline.py` passes `stage_state.processing_data` as `retry_data`
3. ✅ `ConversationQueueHandler.execute()` checks `context.retry_data` for `entry_id` on retry, reuses it as both `id` and `conversation_id`

**Tests**: 4 tests in `tests/test_vnn_plugin.py` (`TestConversationIdContinuation`)

**Relevant code**: `cronpypeline/plugins/conversation_queue.py`, `cronpypeline/actions.py` (`TickContext.retry_data`), `cronpypeline/pipeline.py` (`_handle_stale`)

### 3.3 Rejection audit trail ✅ Complete

**Problem**: cronpypeline tracks rejection count in a simple JSON marker. VNN uses `rejection_log.json` — an append-only audit log with detailed entries (reasons, timestamps, rejection metadata).

**Fix** (implemented): Created `log_rejection` post_tick hook in `cronpypeline/plugins/vnn_plugin.py`:
1. ✅ Checks if a `.rejection` marker exists in the target directory
2. ✅ Appends a detailed entry to `.VNN/rejection_log.json` with timestamp, target, stage, rejection count, reason
3. ✅ Append-only — loads existing log and appends new entries
4. ✅ No-op when no rejection marker exists

**Tests**: 4 tests in `tests/test_vnn_plugin.py` (`TestRejectionAuditTrail`)

**Relevant code**: `cronpypeline/plugins/vnn_plugin.py` (`log_rejection`), `cronpypeline/pipeline.py` (post-tick hook execution)

### 3.4 VNN plugin module ✅ Complete

Created `cronpypeline/plugins/vnn_plugin.py` with all 7 hook callables:

- ✅ `sync_story_states(context) -> bool` — pre_tick hook: sync `.VNN/ranking.json` with filesystem state
- ✅ `cleanup_inconsistent_state(context) -> bool` — pre_tick hook: remove stale processing markers when completion marker exists
- ✅ `check_completed_compilations(context) -> bool` — pre_tick hook: check for `.compilation_complete` markers
- ✅ `cleanup_stale_compilation_markers(context) -> bool` — pre_tick hook: remove stale compilation markers
- ✅ `log_rejection(context, result) -> None` — post_tick hook: append to `.VNN/rejection_log.json`
- ✅ `queue_empty_global(context) -> bool` — pre_tick hook: return `False` if conversation queue is not empty
- ✅ `discover_stories(context) -> bool` — pre_tick hook: scan workspace for story directories, write registry file

**Tests**: 6 tests in `tests/test_vnn_plugin.py` for `queue_empty_global`, `sync_story_states`, `cleanup_inconsistent_state`

### 3.5 Tests for VNN plugin ✅ Complete

- ✅ Unit tests for conversation ID continuation (4 tests)
- ✅ Unit test for rejection audit trail (4 tests — append-only, multiple entries, no-op, reason)
- ✅ Unit tests for `queue_empty_global` (3 tests)
- ✅ Unit tests for `sync_story_states` (1 test)
- ✅ Unit tests for `cleanup_inconsistent_state` (2 tests)
- ⏳ Integration test: multi-tick simulation of research → writing → publishing → revision loop (deferred to Phase 5.3)

---

## Phase 4: VNN Architectural Gaps

> **Affects**: VNN only
> **Priority**: Lower — these are fundamental design differences requiring structural solutions
> **Effort**: Large
> **Note**: These may require core cronpypeline changes or external coordination. Evaluate whether to address in core or via workarounds before implementing.

### 4.1 Two-level target hierarchy (country + story)

**Problem**: VNN stages 2–3 (compilation, ranking) operate on **countries** within a date directory. Stages 4–6b (research, writing, publishing, revision) operate on **stories** within country/date directories. cronpypeline supports one `TargetSpec` per pipeline — all stages operate on the same set of targets.

**Options**:

1. **Two pipelines** (no core change): Run a country-level pipeline and a story-level pipeline as separate cronpypeline instances. The country pipeline writes compilation/ranking results; the story pipeline picks up stories from the output. Coordinate via filesystem state (shared date directory).
   - Pro: No core change, clean separation
   - Con: Two cron entries, two configs, coordination via filesystem

2. **Custom `TargetSpec`** (core change): Add a `hierarchical` target type that can represent both countries and stories. Stages declare which level they operate on.
   - Pro: Single pipeline, single config
   - Con: Significant core change to `targets.py` and `state.py`, complex state derivation

3. **Flatten to story-level** (no core change): Handle compilation/ranking outside cronpypeline (external script or pre_tick hook). Only the story-level stages (research → writing → publishing → revision) run in cronpypeline.
   - Pro: Simplest, leverages cronpypeline's strengths
   - Con: Compilation/ranking logic lives outside the framework

**Recommendation**: Start with option 3 (flatten to story-level), move to option 1 (two pipelines) if compilation/ranking need to be brought into the framework.

### 4.2 Agent-side directory and marker creation

**Problem**: VNN's research agent runs `load_next_story.py` which creates the story directory, copies `story.json`, creates `ranking_metadata.json`, creates `.processing` marker, and creates `.active_story` lock. The pipeline doesn't know which story will be researched — it queues a generic "research the top pending story" prompt and the agent decides. cronpypeline's model is the opposite: the pipeline knows the target, creates the processing marker, and gives the agent a specific target.

**Options**:

1. **Pre-create story directories** (no core change): An external script or `pre_tick` hook scans compiled stories, creates story directories, and registers them in the registry file before the pipeline runs. The pipeline then operates on pre-created story targets normally.
   - Pro: No core change, fits cronpypeline's model
   - Con: Story selection logic duplicated outside the agent

2. **Custom action handler wrapping** (no core change): A custom action handler that wraps the "pick a story" logic — it creates the story directory before the pipeline creates the processing marker. The handler receives the target as a "slot" and fills it with the actual story.
   - Pro: Keeps logic in the pipeline
   - Con: Inverts the pipeline's assumption about target identity

3. **Lazy target binding** (core change): Add a mechanism where a stage's action can dynamically determine the actual target at execution time, creating the target directory as a side effect.
   - Pro: Most flexible
   - Con: Breaks the invariant that target directories exist before state derivation

**Recommendation**: Option 1 (pre-create via pre_tick hook). The `discover_stories` hook from Phase 3.4 can handle this — it scans compiled stories, creates directories, and updates the registry.

### 4.3 Active story lock richness

**Problem**: VNN's `.active_story` lock is a JSON file with `story_id`, `story_dir`, `locked_at`, and `stage`. It's created by both the pipeline and `load_next_story.py` (agent-side), updated with the current stage as the story progresses, and used for diagnostics and cross-pipeline coordination. cronpypeline's `target_lock: true` only provides blocking — no stage tracking, no agent-side creation, no metadata.

**Options**:

1. **Separate metadata file via hooks** (no core change): Use `target_lock: true` for blocking. Maintain a separate `.active_story` file via `pre_tick`/`post_tick` hooks for diagnostics and agent coordination. The hook updates the `stage` field after each tick.
   - Pro: No core change, blocking works
   - Con: Agent-side creation of the lock file (by `load_next_story.py`) needs separate handling

2. **Enrich `target_lock` metadata** (core change): Extend `target_lock` to write a JSON lock file with metadata (target, stage, timestamp) instead of just blocking. Stages update the lock file as they execute.
   - Pro: Integrated solution
   - Con: Core change, agent-side creation still needs coordination

**Recommendation**: Option 1 (separate metadata file via hooks). The blocking behavior is equivalent; the metadata is supplementary. Agent-side creation can be handled by having `load_next_story.py` write the `.active_story` file directly (it already does this in VNN).

---

## Phase 5: Integration & Validation

> **Affects**: Both pipelines
> **Priority**: Medium — proves full compatibility
> **Effort**: Medium
> **Status**: Partially complete — SWE config done (5.1), README updated (5.4 partial)

### 5.1 SWE pipeline JSON config ✅ Complete

Created `configs/swe_pipeline.json` with all SWE stages:
- Phase A: A1–A9 diagnostics (with `run_diagnostic` + parsers), A2-fix-agent, A6-fix-agent (with `queue_fix_agent`)
- Phase B: B1-fetch-issues (GitHub API, `github` mode)
- Phase C: C-select, C-gate, C-code, C-publish, C-pr-status, C-pr-review, C-session-terminal, C-stale
- Mode switching for GitHub sessions, pre_tick hook for `sync_session_mode`
- All triggers, actions, markers, invalidates, modes configured

**Tests**: 14 tests in `tests/test_swe_config.py`

### 5.2 VNN pipeline JSON config ⏳ Pending

Write the complete VNN pipeline config (`configs/vnn_pipeline.json`) with all stages:
- Compilation, ranking (if using two-pipeline approach: separate config)
- Research, writing, publishing, revision (story-level stages)
- Pre-tick hooks for state sync, cleanup, story discovery
- Post-tick hook for rejection audit trail
- Target lock, rejection tracking, queue-file stale detection

### 5.3 End-to-end migration validation ⏳ Pending

- Run SWE pipeline config against a test repo in dry-run mode — verify all stages trigger correctly
- Run VNN pipeline config against a test story directory in dry-run mode — verify all stages trigger correctly
- Verify queue entries are picked up by Serendipity's `conversation_queue_monitor` (format compatibility)
- Multi-tick simulation: verify state flows correctly through all stages for both pipelines

### 5.4 Documentation update ✅ Partial

- ✅ Update `README.md` — add SWE and VNN config examples, plugin documentation
- ⏳ Update `SWE_compatibility.md` — mark all gaps as resolved
- ⏳ Update `VNN_compatibility.md` — mark all gaps as resolved
- ⏳ Document the patterns used (revision loop, multi-state stages, two-level targets)

---

## Summary: Priority-Ordered Work Items

| # | Item | Phase | Pipelines | Effort | Blocks Migration? | Status |
|---|------|-------|-----------|--------|-------------------|--------|
| 1 | Queue entry format fix | 1 | Both | Small–Med | **Yes — critical** | ✅ Complete |
| 2 | Fix `swe_plugin.py` stub bugs | 2 | SWE | Small | Yes | ✅ Complete |
| 3 | SWE issue store plugin | 2 | SWE | Medium | Yes (Phase C) | ✅ Complete |
| 4 | SWE report action handler | 2 | SWE | Medium | Yes (Phase A) | ✅ Complete |
| 5 | SWE prompt builder | 2 | SWE | Medium | Yes (fix agents) | ✅ Complete |
| 6 | GitHub session adapter | 2 | SWE | Small | No | ✅ Complete |
| 7 | SWE pipeline JSON config | 5 | SWE | Medium | — (integration) | ✅ Complete |
| 8 | Conversation ID continuation | 3 | VNN | Small–Med | No | ✅ Complete |
| 9 | Rejection audit trail | 3 | VNN | Small | No | ✅ Complete |
| 10 | VNN plugin module | 3 | VNN | Medium | Yes | ✅ Complete |
| 11 | VNN pipeline JSON config | 5 | VNN | Medium | — (integration) | ⏳ Pending |
| 12 | Two-level target hierarchy | 4 | VNN | Large | No (workaround exists) | ⏳ Pending |
| 13 | Agent-side directory creation | 4 | VNN | Medium | No (workaround exists) | ⏳ Pending |
| 14 | Active story lock richness | 4 | VNN | Small | No | ⏳ Pending |
| 15 | End-to-end validation | 5 | Both | Medium | — (validation) | ⏳ Pending |
| 16 | Documentation update | 5 | Both | Small | — (polish) | ✅ Partial (README done) |

### Dependency graph

```
Phase 1 (queue format) ──┬──> Phase 2 (SWE plugins) ──┬──> Phase 5 (integration)
                         │                             │
                         └──> Phase 3 (VNN plugins) ──┘
                                   │
Phase 4 (VNN architectural) ──────┘
```

Phase 1 unblocks everything. Phases 2 and 3 can proceed in parallel. Phase 4 is independent and lower priority. Phase 5 requires 2 and 3 to be complete.

### What's already done

**Previous roadmap (Tiers 1–4)** — 16 features, 316 tests:
- `config_file` enabled check, `ConversationQueueHandler` wiring, `http_request` handler
- Enriched `TickContext`, custom trigger context, dynamic marker naming, dynamic symlink targets
- Dynamic prompt templates with flattened target config keys
- Cross-stage marker invalidation, pre/post-tick hooks, pipeline mode switching, target lock
- Queue-file-based stale detection, processing marker enhancement, retry prompts, separate rejection counter

**Phases 1–3 (this roadmap)** — 117 new tests (433 total):
- Phase 1: `ConversationQueueHandler` extended with `prompt_field`, `default_fields`, `flatten_agent_settings`, `runs_left` decrement
- Phase 2: SWE issue store (YAML frontmatter), diagnostic report handler + 9 parsers, prompt builders (fix/coder/review), GitHub session adapter, full SWE pipeline JSON config
- Phase 3: Conversation ID continuation (`retry_data` in `TickContext`), rejection audit trail (`log_rejection`), VNN plugin module (7 hook callables)

**Remaining work**: Phase 4 (VNN architectural gaps) and Phase 5.2–5.4 (VNN config, end-to-end validation, compatibility doc updates).
