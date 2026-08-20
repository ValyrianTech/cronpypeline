# SWE Pipeline vs cronpypeline: Feature Compatibility Analysis

> **Objective**: Assess how easily the existing SWE pipeline (in `spellbook/apps/Serendipity/SWE/`) could be configured using `cronpypeline`, and whether all necessary features are supported.

---

## Table of Contents

1. [Architectural Overview](#architectural-overview)
2. [What Works: Shared Patterns](#what-works-shared-patterns)
3. [Critical Gaps: Missing Features](#critical-gaps-missing-features)
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
- Stages have `TriggerCondition`, `ActionSpec`, `markers`, `chain`, `timeout_minutes`, `max_retries`, `on_fail`.
- Supports `file_missing`, `file_exists`, `file_older_than`, `marker_state`, `queue_empty`, `custom`, `and`, `or` trigger types.
- Supports `command`, `queue_agent`, `subprocess`, `http_request`, `custom` action types.
- Markers can be `file`, `json`, or `symlink` type.
- Plugin system via `register_handler()` for custom action handlers.

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
| **Plugin system for actions** | `queue_agent()` writes to conversation queue | `register_handler()`, `ConversationQueueHandler` plugin | ✅ Supported (partially — see gap #11) |
| **Custom triggers** | Detector functions with arbitrary logic | `custom` trigger type with Python callable | ✅ Supported (limited — see gap #2) |
| **Template variables in prompts** | Repo paths in prompts | `{target}`, `{target_dir}`, `{workspace_dir}` | ✅ Supported (limited — see gap #8) |

---

## Critical Gaps: Missing Features

### Gap 1: Report generation + symlink management

**SWE Pipeline**: Mechanical stages (A1–A9) don't just run commands — they:
- Parse tool output (pytest summaries, ruff counts, coverage %, vulture items, pip-audit vulnerabilities, etc.)
- Write structured markdown reports with tables, metadata, and parsed summaries
- Create `latest.md` symlinks pointing to the timestamped report file
- Reports are the state that downstream stages check (e.g., A2-fix-agent checks if A2 report says FAIL)

**cronpypeline**: The `command` action handler runs a command and captures stdout/stderr/exit_code — but has **no report generation** and **no dynamic symlink creation**. The `produces` markers are static specs created after success. The `symlink` marker type requires a pre-configured `target` path — it can't point to a dynamically-named file that was just created.

**Impact**: Every Phase A diagnostic stage (A1–A9) would need a `custom` action handler that replicates the report-writing + symlink logic. The config can't express "run pytest, parse the coverage %, write a markdown report, and symlink `latest.md` to it."

**Relevant code**:
- SWE pipeline: `_write_a1_report()`, `_write_a2_report()`, `_write_a6_report()`, `_write_a7_report()`, etc. in `run_swe_pipeline.py`
- cronpypeline: `CommandActionHandler` in `cronpypeline/actions.py` — no report generation capability

---

### Gap 2: Complex trigger conditions

**SWE Pipeline**: Triggers check things like:
- "Does the latest A2 report have `errors > 0` AND `fixable == 0`?" (parsing report content)
- "Are there ≥2 open review-sourced issues without `hivemind_score`?" (scanning issue store)
- "Is the conversation queue empty AND does the task branch have git commits but no completion marker?" (agent-forgot-marker)
- "Is a GitHub session active?" (reading session JSON)
- "Has the PR been published but not reviewed?" (checking multiple JSON markers)
- "Is coverage < 100% AND no open issues AND A1 is passing?" (multi-condition)

**cronpypeline**: Built-in triggers (`file_missing`, `file_exists`, `file_older_than`, `marker_state`, `queue_empty`) are too simple for these checks. The `custom` trigger type passes only a minimal context dict — it doesn't receive repo config, issue store state, report contents, or git state.

The `swe_plugin.py` file has stubs for `detect_open_issue` and `detect_agent_forgot_marker`, but:
- `detect_open_issue` reads a JSON file (`issues.json`) — not the YAML-frontmatter issue store the SWE pipeline actually uses
- `detect_agent_forgot_marker` has a bug (`iterfile` instead of `iterdir` on line 70)
- Neither receives repo config or report contents in context

**Impact**: Most SWE pipeline triggers would need `custom` callables with enriched context. The custom trigger context would need to be extended to include repo config, issue store state, and report file paths.

**Relevant code**:
- SWE pipeline: `detect_a2_fix_agent()`, `detect_c_review_ranking()`, `detect_c_coverage_issue()`, etc. in `run_swe_pipeline.py`
- cronpypeline: `evaluate_trigger()` and `_eval_custom()` in `cronpypeline/triggers.py`
- cronpypeline: `swe_plugin.py` stubs — skeletal and partially broken

---

### Gap 3: Issue store with YAML frontmatter

**SWE Pipeline**: The issue store (`.SWE/issues/*.md`) is the work queue for Phase C. Each issue has:
- YAML frontmatter with `id`, `source`, `type`, `status`, `attempts`, `hivemind_score`, `rank`, `repo`, `labels`, `github_number`, `github_url`, `created_at`, etc.
- Status lifecycle: `open` → `triaged` → `done` / `discarded`
- Attempt counting with `MAX_ATTEMPTS` enforcement (3 attempts before discard)
- Source discrimination (`dep-audit`, `pipeline`, `review`, `github`) that changes pipeline behavior
- Shared module `issue_store.py` for reading/writing issues

**cronpypeline**: Has **no concept of an issue store**. Its state model is per-stage markers (completion/processing/give_up) — there's no shared work queue that multiple stages read from and write to. The `MarkerSpec` system tracks individual stage state, not a collection of work items with rich metadata.

**Impact**: The entire Phase C fix loop (C-select, C-gate, C-coverage, C-review, C-review-ranking, C-pr-status) revolves around the issue store. This would need to be implemented as custom callables that manage the issue store externally, completely outside cronpypeline's state model.

**Relevant code**:
- SWE pipeline: `issue_store.py` module, `load_issues()`, `set_issue_status()`, `finalize_issue_outcome()` in `run_issue_fix.py`
- cronpypeline: No equivalent — `MarkerSpec` and `StageState` are per-stage, not a shared work queue

---

### Gap 4: Dynamic marker naming

**SWE Pipeline**: Uses per-report deduplication markers:
- `queued_for_{a2_report_stem}.marker` — keyed to the source report filename
- `applied_for_{a2_report_stem}.marker` — same pattern
- `ranked_{N}.marker` — keyed to the count of unranked issues
- `coverage-{sha[:8]}.md` — issue ID keyed to integration HEAD SHA

**cronpypeline**: `MarkerSpec` has a static `name` field — it can't be dynamically keyed to another file's stem or a count of issues. The marker name is fixed at config-load time.

**Impact**: Deduplication logic (preventing re-queueing the same report, preventing re-ranking the same issue set) would need to move entirely into custom action handlers.

**Relevant code**:
- SWE pipeline: `queued_for_{a6_report.stem}.marker` in `detect_a6_fix_agent()`, `ranked_{unranked}.marker` in `detect_c_review_ranking()`
- cronpypeline: `MarkerSpec.name` is a static string in `cronpypeline/markers.py`

---

### Gap 5: Cross-stage side effects (marker invalidation)

**SWE Pipeline**: Stages modify other stages' state:
- A2-autofix deletes A2's `latest.md` so A2 re-runs after autofix
- A2-fix-agent deletes A1 + A2 `latest.md` so both re-run after agent fixes
- A6-fix-agent deletes A1 + A2 + A6 `latest.md` so all three re-run
- C-gate deletes A1 + A7 `latest.md` so tests/coverage re-measure after code changes
- C-pr-status deletes `pr_reviewed.json` + `pr_review_queued.json` to re-trigger C-pr-review
- C-pr-review deletes `pr_reviewed.json` to re-trigger after changes requested

**cronpypeline**: Stages are independent — one stage's action doesn't delete another stage's markers. The `produces` list only creates markers; there's no `deletes` or `invalidates` mechanism. This is a fundamental architectural difference.

**Impact**: Cross-stage invalidation would need to happen inside custom action handlers, making the config less declarative and more like the existing Python code.

**Relevant code**:
- SWE pipeline: `latest.unlink()` / `latest.symlink_to()` calls in fix-agent execute functions, `delete_marker` calls in C-gate/C-pr-review
- cronpypeline: `ActionSpec.produces` only creates markers — no deletion mechanism in `cronpypeline/config.py`

---

### Gap 6: GitHub API integration

**SWE Pipeline**: Makes GitHub API calls for:
- **B1**: Fetching open issues with a label (`GET /repos/.../issues`)
- **C-publish**: Opening PRs (`POST /repos/.../pulls`)
- **C-pr-status**: Polling PR state, fetching reviews (`GET /repos/.../pulls/{n}`, `GET /repos/.../pulls/{n}/reviews`)
- **C-pr-review**: Posting reviews via `post_pr_review.py` CLI subprocess
- **C-session-terminal**: Closing issues, posting comments (`POST /repos/.../issues/{n}/comments`, `PATCH /repos/.../issues/{n}`)
- Token resolution: per-repo config → `SWE_GITHUB_TOKEN` env → `GITHUB_TOKEN` env → `.env` file

**cronpypeline**: `ActionType.HTTP_REQUEST` exists in the enum but **no handler is registered** for it. The `_HANDLERS` dict only contains `command`, `subprocess`, and `custom`.

**Impact**: GitHub API calls would need custom action handlers, or the `http_request` handler would need to be implemented from scratch. Token management and GitHub-specific headers would need custom logic.

**Relevant code**:
- SWE pipeline: `_gh_api_get()`, `_gh_api_post()`, `_gh_api_patch()`, `_gh_api_get_list()` in `run_swe_pipeline.py`
- cronpypeline: `ActionType.HTTP_REQUEST` in `cronpypeline/config.py:34` — no handler in `cronpypeline/actions.py`

---

### Gap 7: Per-target configuration

**SWE Pipeline**: `repos.json` has rich per-repo config:
- Custom commands: `test_cmd`, `lint_cmd`, `typecheck_cmd`, `security_cmd`, `deadcode_cmd`, `coverage_cmd`, `complexity_cmd`, `dep_audit_cmd`
- Thresholds: `coverage_threshold`, `max_review_generations`, `max_review_issues_per_generation`, `max_pr_review_cycles`
- GitHub: `slug`, `issue_label`, `github_token`, `default_branch`
- Flags: `skip_deadcode`, `delivery`, `enabled`

**cronpypeline**: Target registry (`load_targets()` in `cronpypeline/targets.py`) only extracts `item["name"]` from the registry — it doesn't pass per-target config to stages or actions. The `TickContext` only has `target`, `workspace_dir`, `dry_run`, `verbose`, `env`, `state`.

**Impact**: Per-repo commands and thresholds can't be configured via the target registry. Either each repo needs its own pipeline config, or the target loading and `TickContext` would need to be extended to pass per-target config into stages and actions.

**Relevant code**:
- SWE pipeline: `load_repo_registry()` in `run_swe_pipeline.py`, `repo.get('lint_cmd')`, `repo.get('coverage_threshold')`, etc.
- cronpypeline: `load_targets()` in `cronpypeline/targets.py:40-51` — only extracts names
- cronpypeline: `TickContext` in `cronpypeline/actions.py` — no per-target config field

---

### Gap 8: Dynamic prompt generation

**SWE Pipeline**: Prompts include:
- Full report contents (lint report, coverage report, dead code report, etc.)
- Repo-specific paths and commands
- Issue details (title, body, frontmatter metadata)
- Cycle numbers for PR review ("cycle 2 of 3")
- Integration branch SHA, diff stats
- Phase A commit hints (branch name, commit message template)
- Instructions to delete `latest.md` files after committing

**cronpypeline**: `prompt_template` in `ConversationQueueHandler` only substitutes `{target}`, `{target_dir}`, `{workspace_dir}`.

**Impact**: Prompts would need to be generated by custom action handlers that can read reports, issue files, and git state. The template variable system is too limited for the SWE pipeline's needs.

**Relevant code**:
- SWE pipeline: Multi-line f-string prompts in `detect_a2_fix_agent()`, `detect_a6_fix_agent()`, `detect_a7_fix_agent()`, etc.
- cronpypeline: `format_template()` with 3 variables in `cronpypeline/plugins/conversation_queue.py:38-46`

---

### Gap 9: Multi-state task machine (C-select / C-gate / C-wait / C-stale)

**SWE Pipeline**: The Phase C fix loop is a state machine with 4 sub-states:
- **C-select**: No active task + open issue → create `task.json`, create task branch, queue CoderAgent
- **C-gate**: `coding_complete.marker` exists → re-run verification tools, capture diff, merge into integration branch, finalize issue status
- **C-wait**: Active task, no completion marker → idle (return action but don't execute)
- **C-stale**: Task older than 30 min → cleanup task dir, reset issue to open (or discard if max attempts), select next issue

Each sub-state has different triggers and actions, but they share a task directory (`workspace/tasks/{task_id}/`) and issue state that spans multiple files.

**cronpypeline**: This doesn't map to cronpypeline's single-stage trigger→action model. Each sub-state would need to be a separate stage, but they share state (task.json, coding_complete.marker, issue status) that cronpypeline doesn't track as a unit.

**Impact**: The entire Phase C fix loop would need to be implemented as a custom action handler or a set of custom trigger+action pairs that manage shared state externally.

**Relevant code**:
- SWE pipeline: `detect_phase_c_fix()` in `run_issue_fix.py:1068+` — 4-way state machine
- cronpypeline: No multi-state stage concept — each stage is independent

---

### Gap 10: Pipeline-wide mode switching (GitHub session)

**SWE Pipeline**: The GitHub session marker (`.SWE/github_session.json`) changes behavior across ~10 detectors simultaneously:
- C-select only picks `source: github` issues (filters out dep-audit/review issues)
- C-review uses delta scope (generation 2) instead of full-tree review
- C-review-ranking skips entirely
- C-coverage/review/publish/pr-review/pr-status all check session state
- C-session-terminal handles PR merge/close/comment

**cronpypeline**: No concept of pipeline-wide mode. The `config_file` field in `PipelineConfig` is a toggle file for enable/disable, but there's no mode-switching mechanism that changes multiple stages' behavior based on a shared state file.

**Impact**: Mode switching would need to be implemented as custom trigger callables that each check the session file independently, duplicating the logic across stages.

**Relevant code**:
- SWE pipeline: `is_github_session_active()`, `is_github_session_completed()`, `_read_github_session()` in `run_swe_pipeline.py`
- cronpypeline: No mode-switching mechanism

---

### Gap 11: `ConversationQueueHandler` plugin is incomplete

**SWE Pipeline**: The `queue_agent()` function writes JSON files to a conversation queue directory with agent name, prompt, folder, and extra metadata (repo_name, repo_dir, stage, report paths).

**cronpypeline**: The `ConversationQueueHandler` class exists but:
- The `register()` function at `cronpypeline/plugins/conversation_queue.py:88-91` registers a string `"placeholder"` instead of an actual handler instance
- The handler needs to be instantiated with `queue_dir` and `agent_settings_dir` — there's no code that instantiates a handler from `ActionHandlerConfig` and registers it
- The `ActionHandlerConfig` in `config.py` has `type` and `params`, but the pipeline initialization code doesn't use it to create and register handlers
- The `extra` metadata that SWE pipeline passes (repo_name, repo_dir, stage, report paths) has no equivalent in the handler

**Impact**: The conversation queue plugin needs to be wired up properly before it can be used. The `ActionHandlerConfig` → handler instantiation → registration pipeline is missing.

**Relevant code**:
- cronpypeline: `ConversationQueueHandler.register()` in `cronpypeline/plugins/conversation_queue.py:88-91`
- cronpypeline: `ActionHandlerConfig` in `cronpypeline/config.py:179-193` — defined but not used in `Pipeline.__init__()`

---

### Gap 12: `http_request` action handler not implemented

**cronpypeline**: `ActionType.HTTP_REQUEST` exists in the enum (`cronpypeline/config.py:34`) but no handler is registered in the `_HANDLERS` dict (`cronpypeline/actions.py`). Calling `execute_action()` with this type would raise `ValueError("No handler registered for action type: http_request")`.

**Impact**: GitHub API calls (B1 issue intake, C-publish PR creation, C-pr-status polling, C-session-terminal issue closing) cannot use the built-in action system. They would need custom action handlers.

---

## Stage-by-Stage Mapping

### Phase A — Diagnostics & Hygiene

| Stage | SWE Pipeline Function | Trigger Logic | Action | cronpypeline Feasibility |
|-------|-----------------------|---------------|--------|-------------------------|
| **A0** (briefing) | `detect_a0_briefing` | `repo_briefing.md` missing | Queue RepoResearchAgent | ✅ `file_missing` trigger + `queue_agent` action. Prompt is static enough. |
| **A1** (test infra) | `detect_a1_test_infra` | `latest.md` missing in test-infra reports | Mechanical: run test infra check, write report + symlink | ⚠️ `file_missing` trigger works, but `command` action can't write reports or create dynamic symlinks. Needs `custom` handler. |
| **A2** (lint) | `detect_a2_lint` | `latest.md` missing in lint reports | Mechanical: run ruff, write report + symlink | ⚠️ Same as A1. |
| **A2-autofix** | `detect_a2_autofix` | A2 report exists + has fixable errors + no `applied_for_*.marker` | Mechanical: run `ruff --fix`, delete A2 `latest.md` | ❌ Requires parsing report content for trigger, dynamic marker naming, and cross-stage marker deletion. All need `custom`. |
| **A2-fix-agent** | `detect_a2_fix_agent` | A2 report exists + FAIL + no fixable + no `queued_for_*.marker` | Queue LintFixAgent with report content in prompt | ❌ Requires report content parsing, dynamic marker naming, dynamic prompt with report contents, cross-stage marker deletion. |
| **A3** (docstrings) | `detect_a3_docstrings` | `latest.md` missing in docstring reports | Mechanical: run pydocstyle, write report + symlink | ⚠️ Same as A1. |
| **A3-fix-agent** | `detect_a3_fix_agent` | A3 report exists + FAIL + no `queued_for_*.marker` | Queue DocstringAgent with report content | ❌ Same as A2-fix-agent. |
| **A4** (typecheck) | `detect_a4_typecheck` | `latest.md` missing in typecheck reports | Mechanical: run mypy, write report + symlink | ⚠️ Same as A1. |
| **A4-fix-agent** | `detect_a4_fix_agent` | A4 report exists + FAIL + no `queued_for_*.marker` | Queue TypeFixAgent with report content | ❌ Same as A2-fix-agent. |
| **A5** (security) | `detect_a5_security` | `latest.md` missing in security reports | Mechanical: run bandit/pip-audit, write report + symlink | ⚠️ Same as A1. |
| **A5-fix-agent** | `detect_a5_fix_agent` | A5 report exists + FAIL + no `queued_for_*.marker` | Queue SecurityFixAgent with report content | ❌ Same as A2-fix-agent. |
| **A6** (deadcode) | `detect_a6_deadcode` | `latest.md` missing in deadcode reports | Mechanical: run vulture, write report + symlink | ⚠️ Same as A1. |
| **A7** (coverage) | `detect_a7_coverage` | `latest.md` missing in coverage reports | Mechanical: run pytest --cov, write report + symlink | ⚠️ Same as A1. |
| **A7-fix-agent** | `detect_a7_fix_agent` | A7 report exists + coverage < threshold + no `queued_for_*.marker` | Queue CoverageAgent with report content | ❌ Same as A2-fix-agent, plus per-repo threshold. |
| **A8** (complexity) | `detect_a8_complexity` | `latest.md` missing in complexity reports | Mechanical: run radon, write report + symlink | ⚠️ Same as A1. |
| **A9** (dep audit) | `detect_a9_dep_audit` | `latest.md` missing in dep-audit reports | Mechanical: run pip-audit, write report + symlink, create issues for vulnerabilities | ❌ Report generation + issue creation in one action. Needs `custom`. |

### Phase B — GitHub Issue Intake

| Stage | SWE Pipeline Function | Trigger Logic | Action | cronpypeline Feasibility |
|-------|-----------------------|---------------|--------|-------------------------|
| **B1** (issue gathering) | `detect_b1_issue_gathering` | No active/completed GitHub session + recheck interval elapsed + token available | Mechanical: GitHub API GET, write issue to store, create session JSON | ❌ Requires GitHub API handler, session management, recheck interval logic, issue store writes. All `custom`. |
| **B2** (TaskCompiler) | `_not_implemented` | N/A | N/A | N/A — not implemented in SWE pipeline either. |
| **B3** (Targaryen Council) | `_not_implemented` | N/A | N/A | N/A — not implemented in SWE pipeline either. |

### Phase C — Code Writing

| Stage | SWE Pipeline Function | Trigger Logic | Action | cronpypeline Feasibility |
|-------|-----------------------|---------------|--------|-------------------------|
| **C-review-ranking** | `detect_c_review_ranking` | No active task + ≥2 unranked review issues + no `ranked_{N}.marker` + not in GitHub session | Mechanical: run `run_swe_issue_ranking.py` subprocess | ❌ Requires counting unranked issues (custom trigger), dynamic marker naming, subprocess with repo-specific args. |
| **C-pr-status** | `detect_c_pr_status` | `pr_published.json` exists + no `pr_reviewed.json` | Mechanical: GitHub API GET PR state, handle merge/close/changes | ❌ Requires GitHub API, PR state parsing, conditional behavior based on review state. |
| **C-issue-fix** | `detect_c_issue_fix` | Active task needs gating OR stale task needs cleanup OR open issue needs selection | Queue CoderAgent (select) or run verification (gate) or cleanup (stale) | ❌ Multi-state task machine. Doesn't fit single trigger→action model. |
| **C-session-terminal** | `detect_c_github_session_terminal` | GitHub session completed + PR merged | Mechanical: close GitHub issue, post comment, delete session | ❌ GitHub API + session management. |
| **C-coverage-issue** | `detect_c_coverage_issue` | No open issues + A1 passing + coverage < target + no pending PR review | Mechanical: create coverage issue in issue store | ❌ Multi-condition trigger with report parsing + issue store writes. |
| **C-review-issue** | `detect_c_review_issue` | No open issues + coverage ≥ target + review generations < max + no pending PR | Mechanical: create review issue in issue store | ❌ Multi-condition trigger with report parsing + issue store writes + generation counting. |
| **C-doc-sync** | `detect_c_doc_sync` | No open issues + no `doc_sync.json` marker + integration branch has commits | Queue DocSyncAgent | ⚠️ Trigger is expressible with `and` conditions + `file_missing`. Prompt needs dynamic content. |
| **C-pr-publish** | `detect_c_pr_publish` | No open issues + coverage ≥ target + no `pr_published.json` + review generations exhausted | Mechanical: push integration branch, create PR via GitHub API, write `pr_published.json` | ❌ GitHub API + multi-condition trigger + git operations. |
| **C-pr-review** | `detect_c_pr_review` | `pr_published.json` exists + no `pr_reviewed.json` + no `pr_review_queued.json` | Queue PRReviewAgent or run `post_pr_review.py` | ❌ Multi-marker trigger + GitHub API + review state parsing. |

---

## Summary and Recommendations

### Bottom Line

**The SWE pipeline cannot be configured in cronpypeline as-is.** The core orchestration patterns match (tick-based, detector chain, file lock, chaining, retries), but the SWE pipeline's complexity far exceeds what cronpypeline's config model can express.

The SWE pipeline's 5,771 lines of detector logic — report parsing, issue store management, GitHub API integration, git operations, multi-state task machine, session mode switching — would all need to be reimplemented as custom callables and action handlers. The JSON config would become a thin wrapper around Python code that does all the real work, at which point the config-driven approach provides little value over the existing code.

### Feature Parity Matrix

| Category | What works | What's missing |
|----------|-----------|---------------|
| **Phase A diagnostics (A1-A9)** | `command` action can run tools | No report generation, no output parsing, no dynamic symlinks, no per-report dedup markers |
| **Phase A fix-agents** | `queue_agent` action can dispatch agents | No report-content-based triggers, no dynamic prompts with report contents, no cross-stage `latest.md` deletion |
| **Phase B (GitHub intake)** | `http_request` type exists in enum | No handler implemented, no GitHub API helpers, no session management |
| **Phase C fix loop** | `custom` triggers/actions theoretically possible | No issue store, no multi-state task machine, no agent-forgot-marker recovery, no dynamic prompt generation |
| **Phase C review/publish** | `command`/`subprocess` can run scripts | No GitHub API integration, no PR state polling, no change-request parsing, no revision issue filing |
| **Cross-cutting** | Lock, chaining, dry-run, targets | No per-target config, no pipeline modes, no cross-stage invalidation |

### Key Additions Needed in cronpypeline

To make cronpypeline viable for this pipeline, these additions would be needed (in priority order):

1. **Enriched custom trigger context** — Pass repo config, issue store state, report contents, and git state into custom trigger callables
2. **Report-writing action handler** — Run command → parse output → write structured markdown report → create `latest.md` symlink
3. **Issue store module** — YAML frontmatter read/write, status lifecycle (open→done/discarded), attempt counting
4. **`http_request` handler implementation** — Support GET/POST/PATCH with headers, auth tokens, and JSON bodies
5. **Dynamic marker naming** — Allow marker names to be templated from context (e.g., `queued_for_{report_stem}.marker`)
6. **Cross-stage marker invalidation** — Add `deletes`/`invalidates` field to `Stage` config so one stage can reset another's markers
7. **Per-target config passthrough** — Extend `load_targets()` to return full config dicts, and extend `TickContext` to include per-target config
8. **Dynamic prompt templates** — Support including file contents, report summaries, issue details, and git SHAs in prompts
9. **ActionHandler instantiation from config** — Wire `ActionHandlerConfig` to instantiate and register handler plugins from JSON
10. **Multi-state stage support** — Allow a stage to represent a state machine with sub-states (or provide a pattern for composing multiple stages that share state)
