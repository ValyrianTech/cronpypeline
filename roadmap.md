# cronpypeline Roadmap: Full SWE + VNN Compatibility

> Derived from `SWE_compatibility.md` and `VNN_compatibility.md` (re-evaluated August 2026).
> Tiers 1–4 of the previous roadmap are fully implemented (316 tests passing). This roadmap
> covers **all remaining work** needed to achieve full compatibility with both existing pipelines.

---

## Phase 1: Critical Blocker — Queue Entry Format Fix

> **Affects**: SWE + VNN
> **Priority**: Critical — without this, no agents queued by cronpypeline will be dispatched by Serendipity's `conversation_queue_monitor`.
> **Effort**: Small–Medium

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

### 1.2 Tests for queue entry format

Add tests verifying:
- Default format (backward compatibility — `prompt` field, no extra fields)
- Serendipity format (`content` field, `sender`/`conversation_id`/`folder_name`/`model_name`/`runs_left` present)
- Agent settings flattening (flat fields, not nested `agent_config`)
- `runs_left` decrement or override on retry entries

---

## Phase 2: SWE Plugin Implementation

> **Affects**: SWE only
> **Priority**: High — required for SWE migration
> **Effort**: Medium–Large
> **Approach**: All SWE-specific logic lives in plugin code (`swe_plugin.py` + new modules). No core cronpypeline changes needed except Phase 1.

### 2.1 Fix `swe_plugin.py` stub bugs

**Problem**: The existing stubs have known bugs:

- `detect_agent_forgot_marker` line 70: `iterfile()` should be `iterdir()` — `Path.iterfile()` does not exist
- `detect_open_issue` reads `.SWE/issues.json` (JSON array) — the SWE pipeline uses `.SWE/issues/*.md` with YAML frontmatter
- `reset_issue_status` reads/writes `.SWE/issues.json` — same format mismatch
- `cleanup_git_branch` accesses `context.target_dir` as attribute — custom actions receive `(action, context)` where `context` is a `TickContext` (has `.target_dir` attribute, so this may work, but should be verified)

**Fix**:
1. Fix `iterfile()` → `iterdir()` in `detect_agent_forgot_marker`
2. Rewrite `detect_open_issue` to scan `.SWE/issues/*.md` files, parse YAML frontmatter, check for `status: open`
3. Rewrite `reset_issue_status` to read the specific issue `.md` file, update frontmatter `status` field, write back
4. Verify `cleanup_git_branch` context access pattern works with `TickContext`

**Relevant code**: `cronpypeline/plugins/swe_plugin.py:14-126`

### 2.2 SWE issue store plugin

**Problem**: SWE's Phase C revolves around an issue store — a directory of markdown files with YAML frontmatter (`id`, `source`, `type`, `status`, `attempts`, `hivemind_score`, `rank`, `repo`, `labels`, `github_number`, `github_url`, `created_at`). Multiple stages read from and write to this shared store. cronpypeline has no concept of a shared work queue.

**Fix**: Create `cronpypeline/plugins/issue_store.py` with:

- `load_issues(target_dir) -> list[Issue]` — scan `.SWE/issues/*.md`, parse YAML frontmatter
- `get_issue(target_dir, issue_id) -> Issue` — read single issue
- `set_issue_status(target_dir, issue_id, status) -> bool` — update frontmatter `status` field
- `create_issue(target_dir, issue_data) -> Issue` — write new issue `.md` file with frontmatter
- `finalize_issue_outcome(target_dir, issue_id, outcome) -> bool` — set final status, update `attempts`
- `Issue` dataclass with all frontmatter fields

This module is used by custom trigger callables and custom action handlers in the SWE pipeline config. It lives outside cronpypeline's per-stage marker model.

**Relevant code**: New module `cronpypeline/plugins/issue_store.py`. SWE pipeline reference: `issue_store.py`, `run_issue_fix.py`

### 2.3 SWE report action handler

**Problem**: SWE's Phase A diagnostics (A1–A9) each run a tool, parse its output, write a structured markdown report with tables/metadata, and create a `latest.md` symlink. `CommandActionHandler` only captures stdout/stderr/exit_code. The `reporting.py` utilities exist but nothing wires them together.

**Fix**: Create a `run_diagnostic` custom action callable in `swe_plugin.py` (or a new `swe_diagnostics.py` module) that:

1. Runs the configured command (using `subprocess.run`)
2. Passes stdout to a configurable parser callable (e.g. `parse_pytest_output`, `parse_ruff_output`)
3. Writes a timestamped markdown report via `reporting.write_report()`
4. Creates/updates `latest.md` symlink via `reporting.update_latest_symlink()`
5. Returns `ActionResult(success=True, data={"report_path": str(path)})`

Each diagnostic stage (A1–A9) references this callable with different parser and report config:

```json
{
  "type": "custom",
  "params": {
    "callable": "cronpypeline.plugins.swe_plugin.run_diagnostic",
    "command": "{test_cmd}",
    "report_dir": ".SWE/reports/test-infra",
    "parser": "cronpypeline.plugins.swe_plugin.parse_pytest_output"
  }
}
```

Implement parsers for: pytest (A1), ruff (A2), pydocstyle (A3), mypy (A4), bandit/pip-audit (A5), vulture (A6), pytest --cov (A7), radon (A8), pip-audit deps (A9).

**Relevant code**: `cronpypeline/reporting.py` (`write_report`, `update_latest_symlink`), new code in `cronpypeline/plugins/swe_plugin.py` or `cronpypeline/plugins/swe_diagnostics.py`

### 2.4 SWE prompt builder

**Problem**: SWE agent prompts include full report contents, issue details (title, body, frontmatter), cycle numbers, integration branch SHA, diff stats, commit hints, and instructions. Target config template variables alone are insufficient for these dynamic prompts.

**Fix**: Create custom action callables in `swe_plugin.py` that build prompts programmatically:

- `queue_fix_agent(action, context)` — reads report file from `action.params["report_path"]`, builds prompt with report contents, queues via `ConversationQueueHandler`
- `queue_coder_agent(action, context)` — reads issue from issue store, builds prompt with issue details + git state
- `queue_review_agent(action, context)` — builds prompt with cycle numbers, diff stats, PR state

These callables use `format_template()` with any variables they construct (report contents, issue fields, git SHA, etc.) and then dispatch to the conversation queue.

**Relevant code**: `cronpypeline/plugins/conversation_queue.py` (`ConversationQueueHandler`), `cronpypeline/actions.py` (`format_template`)

### 2.5 GitHub session adapter

**Problem**: SWE's `.SWE/github_session.json` has a richer format than cronpypeline's `mode_file` (`{"mode": "..."}`). The session file contains session metadata, not just a mode string.

**Fix**: Either:
1. **Adapter hook** (preferred): A `pre_tick` hook that reads `.SWE/github_session.json`, extracts/derives the mode, and writes `{"mode": "github"}` to the `mode_file` path. No core change needed.
2. **Config mapping**: If the session file can be extended with a `mode` field, point `mode_file` directly at it.

Implement as a `pre_tick` callable: `cronpypeline.plugins.swe_plugin.sync_session_mode`.

**Relevant code**: `cronpypeline/pipeline.py` (`_get_current_mode`, pre-tick hook execution)

### 2.6 SWE pipeline JSON config

**Problem**: No actual SWE pipeline JSON config exists yet — only the compatibility analysis.

**Fix**: Write `configs/swe_pipeline.json` that defines all SWE stages (A0–A9, A2–A7 fix agents, B1, C-select/gate/stale, C-review/publish/pr-review/pr-status/session-terminal) using the plugin callables from 2.1–2.5. This is the integration artifact that proves full compatibility.

### 2.7 Tests for SWE plugin

- Unit tests for issue store (create, read, update status, finalize, attempt counting)
- Unit tests for each diagnostic parser (pytest, ruff, mypy, etc.)
- Unit tests for prompt builders (verify report contents, issue details, git state in prompt)
- Integration test: multi-tick simulation of Phase A → Phase B → Phase C flow

---

## Phase 3: VNN Plugin Implementation

> **Affects**: VNN only
> **Priority**: Medium — VNN's article processing stages map well already; the remaining work is plugin code + architectural decisions
> **Effort**: Medium–Large

### 3.1 VNN queue entry format

**Problem**: Same as SWE Gap 0 — VNN's `conversation_queue_monitor` expects `content` (not `prompt`), `sender`, `conversation_id`, `folder_name`, `model_name`, `runs_left`.

**Fix**: Use the same `ConversationQueueHandler` extension from Phase 1.1 with VNN-specific defaults:

```json
"action_handler": {
  "type": "conversation_queue",
  "params": {
    "queue_dir": "/path/to/queue",
    "prompt_field": "content",
    "default_fields": {
      "sender": "VNN_PIPELINE",
      "conversation_id": "",
      "folder_name": "VNN",
      "model_name": "default_model",
      "runs_left": 3
    },
    "flatten_agent_settings": true
  }
}
```

No additional code needed beyond Phase 1.

### 3.2 Conversation ID continuation

**Problem**: `ConversationQueueHandler` creates a new UUID per queue entry. On retry, the `entry_id` from the processing marker is not reused — the agent gets a new conversation instead of continuing the previous one.

**Fix**: Extend `ConversationQueueHandler.execute()` to check `context.retry_count > 0` and read the previous `entry_id` from the processing marker (available in `context` via the processing marker data). Reuse it as `conversation_id` in the new queue entry.

This requires:
1. Passing processing marker data into `TickContext` (or a `retry_data` field)
2. `ConversationQueueHandler` reading `conversation_id` from `retry_data` on retry

**Relevant code**: `cronpypeline/plugins/conversation_queue.py:28-96`, `cronpypeline/actions.py` (`TickContext`), `cronpypeline/pipeline.py` (`_handle_stale` — passes retry context)

### 3.3 Rejection audit trail

**Problem**: cronpypeline tracks rejection count in a simple JSON marker. VNN uses `rejection_log.json` — an append-only audit log with detailed entries (reasons, timestamps, rejection metadata).

**Fix**: Create a `post_tick` hook callable that:
1. Checks if the tick result involves a rejection (rejection marker was created/updated)
2. Appends a detailed entry to `rejection_log.json` with timestamp, reason, stage, target

Implement as: `cronpypeline.plugins.vnn_plugin.log_rejection`

No core change needed — `post_tick` hooks receive the `TickResult` and context.

**Relevant code**: `cronpypeline/pipeline.py` (post-tick hook execution)

### 3.4 VNN plugin module

Create `cronpypeline/plugins/vnn_plugin.py` with:

- `sync_story_states(context) -> bool` — pre_tick hook: sync `ranking.json` with filesystem state
- `cleanup_inconsistent_state(context) -> bool` — pre_tick hook: resolve marker conflicts
- `check_completed_compilations(context) -> bool` — pre_tick hook: check for completed compilation markers
- `cleanup_stale_compilation_markers(context) -> bool` — pre_tick hook: remove stale compilation markers
- `log_rejection(context, result) -> None` — post_tick hook: append to `rejection_log.json`
- `queue_empty_global(context) -> bool` — pre_tick hook: return `False` if conversation queue is not empty (global queue-empty gate)
- `discover_stories(context) -> bool` — pre_tick hook: scan story directories, update registry file

### 3.5 Tests for VNN plugin

- Unit tests for each pre_tick hook
- Unit test for rejection audit trail (verify append-only log entries)
- Integration test: multi-tick simulation of research → writing → publishing → revision loop

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

### 5.1 SWE pipeline JSON config

Write the complete SWE pipeline config (`configs/swe_pipeline.json`) with all stages:
- Phase A: A0 (briefing), A1–A9 (diagnostics with report handlers), A2–A7 fix agents (with prompt builders)
- Phase B: B1 (GitHub issue gathering)
- Phase C: C-select, C-gate, C-stale (as separate stages), C-review-ranking, C-review-issue, C-coverage-issue, C-doc-sync, C-pr-publish, C-pr-review, C-pr-status, C-session-terminal
- Mode switching for GitHub sessions
- All triggers, actions, markers, invalidates, modes configured

### 5.2 VNN pipeline JSON config

Write the complete VNN pipeline config (`configs/vnn_pipeline.json`) with all stages:
- Compilation, ranking (if using two-pipeline approach: separate config)
- Research, writing, publishing, revision (story-level stages)
- Pre-tick hooks for state sync, cleanup, story discovery
- Post-tick hook for rejection audit trail
- Target lock, rejection tracking, queue-file stale detection

### 5.3 End-to-end migration validation

- Run SWE pipeline config against a test repo in dry-run mode — verify all stages trigger correctly
- Run VNN pipeline config against a test story directory in dry-run mode — verify all stages trigger correctly
- Verify queue entries are picked up by Serendipity's `conversation_queue_monitor` (format compatibility)
- Multi-tick simulation: verify state flows correctly through all stages for both pipelines

### 5.4 Documentation update

- Update `SWE_compatibility.md` — mark all gaps as resolved
- Update `VNN_compatibility.md` — mark all gaps as resolved
- Update `README.md` — add SWE and VNN config examples, plugin documentation
- Document the patterns used (revision loop, multi-state stages, two-level targets)

---

## Summary: Priority-Ordered Work Items

| # | Item | Phase | Pipelines | Effort | Blocks Migration? |
|---|------|-------|-----------|--------|-------------------|
| 1 | Queue entry format fix | 1 | Both | Small–Med | **Yes — critical** |
| 2 | Fix `swe_plugin.py` stub bugs | 2 | SWE | Small | Yes |
| 3 | SWE issue store plugin | 2 | SWE | Medium | Yes (Phase C) |
| 4 | SWE report action handler | 2 | SWE | Medium | Yes (Phase A) |
| 5 | SWE prompt builder | 2 | SWE | Medium | Yes (fix agents) |
| 6 | GitHub session adapter | 2 | SWE | Small | No |
| 7 | SWE pipeline JSON config | 5 | SWE | Medium | — (integration) |
| 8 | Conversation ID continuation | 3 | VNN | Small–Med | No |
| 9 | Rejection audit trail | 3 | VNN | Small | No |
| 10 | VNN plugin module | 3 | VNN | Medium | Yes |
| 11 | VNN pipeline JSON config | 5 | VNN | Medium | — (integration) |
| 12 | Two-level target hierarchy | 4 | VNN | Large | No (workaround exists) |
| 13 | Agent-side directory creation | 4 | VNN | Medium | No (workaround exists) |
| 14 | Active story lock richness | 4 | VNN | Small | No |
| 15 | End-to-end validation | 5 | Both | Medium | — (validation) |
| 16 | Documentation update | 5 | Both | Small | — (polish) |

### Dependency graph

```
Phase 1 (queue format) ──┬──> Phase 2 (SWE plugins) ──┬──> Phase 5 (integration)
                         │                             │
                         └──> Phase 3 (VNN plugins) ──┘
                                   │
Phase 4 (VNN architectural) ──────┘
```

Phase 1 unblocks everything. Phases 2 and 3 can proceed in parallel. Phase 4 is independent and lower priority. Phase 5 requires 2 and 3 to be complete.

### What's already done (previous roadmap, Tiers 1–4)

All 16 features from the previous roadmap are implemented (316 tests passing):
- `config_file` enabled check, `ConversationQueueHandler` wiring, `http_request` handler
- Enriched `TickContext`, custom trigger context, dynamic marker naming, dynamic symlink targets
- Dynamic prompt templates with flattened target config keys
- Cross-stage marker invalidation, pre/post-tick hooks, pipeline mode switching, target lock
- Queue-file-based stale detection, processing marker enhancement, retry prompts, separate rejection counter

No core cronpypeline changes are needed for Phases 2–3 (all plugin code). Phase 1 requires a small extension to `ConversationQueueHandler`. Phase 4 may require core changes depending on chosen approach.
