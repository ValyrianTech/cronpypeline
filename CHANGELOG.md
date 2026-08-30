# Changelog

## [Unreleased]

### Added
- The SWE pipeline's test-coverage target is now configurable per-target via a `coverage_threshold` key in the target's registry config (defaulting to 100%). The threshold drives the `C-coverage` stage (a coverage issue is created when coverage falls below it), the `C-review`/`C-doc-sync`/`C-publish` gates (which only proceed once coverage meets the threshold), and the issue-fix plugin's SELECT/GATE coverage verification (the threshold is stored on the task at SELECT time and enforced at GATE time).

### Changed
- `_build_queue_handler` in the SWE prompt builders now raises a `ValueError` when `queue_dir` is missing or empty, instead of silently defaulting to an empty string (which previously resolved to the current working directory). This affects the `queue_fix_agent`, `queue_coder_agent`, and `queue_review_agent` actions, which now require `queue_dir` to be configured either in the stage action params or in the pipeline's top-level `action_handler` config.

### Fixed
- The SWE plugin's `run_a5_bandit`, `run_a6_vulture`, and `run_a8_radon` actions now check `context.target_dir.is_dir()` (instead of `(context.target_dir / context.target).is_dir()`) when deciding whether to scan the target directory or the current directory. Previously the incorrect path check meant the default scan target was almost never the target directory, so these actions could scan the wrong path when no custom `security_cmd`/`deadcode_cmd`/`complexity_cmd` was configured.
- The issue-fix plugin's `run_gate` now prints a WARNING message when `git checkout` of the integration branch (for baseline coverage on non-coverage issues) or the task branch (for verification) fails (non-zero return code), instead of silently proceeding and potentially running coverage/verification commands on the wrong branch.
- The SWE `queue_fix_agent` action now writes its deduplication marker (`queued_for_{report_stem}.marker`) only after the agent is successfully queued (and only when not in dry-run mode), instead of before queueing. Previously the marker was written before the queue action, so a queue failure would leave the dedup marker in place and the work would be lost (the stage would appear queued when it wasn't). Now a failed queue action leaves no dedup marker, so the work is retried on a subsequent tick.
- The issue-fix plugin's `_is_task_stale` now falls back to the task file's modification time (mtime) when the task's `created_at` field is missing or unparseable (corrupt JSON, invalid date), instead of treating the task as immediately stale. This prevents valid in-progress tasks from being prematurely cleaned up just because they lack a `created_at` timestamp.
- The issue-fix plugin's stale-task cleanup now uses `git reset --hard HEAD` instead of `git clean -fd`, so untracked files (e.g. `.venv`, generated artifacts) are preserved when a stale task's git state is reset.
- Chained stage failure now reported in TickResult with ACTION_FAILED status and the failed stage ID.
- Chained stage on_fail actions are now executed when a chained stage fails (previously silently ignored).
- Chained stage failure message no longer has a trailing colon when the action produces no output.
- StageState.is_actionable now treats a rejected stage as actionable when max_rejections=0 (rejection tracking disabled).
- mode_file and config_file paths are now resolved relative to workspace_dir instead of the current working directory.
- http_request action handler now rejects non-http/https URL schemes (e.g. file://) to prevent arbitrary file access.
- Async custom actions (e.g. queue_fix_agent, queue_coder_agent, queue_review_agent) no longer create their completion marker immediately; the pipeline now respects `data: {"async": true}` returned by custom actions and defers completion to the external agent.
- Custom action handler now passes through `ActionResult` return values instead of stringifying them.
- `ActionResult.data` now defaults to `{}` when `None` is passed, fixing an AttributeError when accessing `result.data.get('async')`.
- Async chained stages now create a processing marker (with `retry_count=0` and the result data merged in) to prevent duplicate agent queueing.
- Non-chained async custom actions now create a processing marker (with `retry_count=0` and the result data merged in) so they are not re-triggered on every tick, preventing duplicate agent queueing.
- `_handle_stale` now returns a DRY_RUN result before deleting processing markers or re-queueing when in dry-run mode, and correctly reports "Would give up" when the retry limit is reached.
- `_handle_stale` now returns ACTION_FAILED (and runs `on_fail`) when the re-executed action fails, instead of reporting ACTION_EXECUTED.
- `FileLock.__enter__` now raises `RuntimeError` when the lock cannot be acquired instead of silently continuing.
- MarkerSpec objects are no longer mutated in-place when the pipeline creates processing markers (e.g. for async actions, chained stages, or stale re-queueing); the pipeline now uses `dataclasses.replace()` to build new marker specs, preventing shared config objects from being modified during tick execution.
- Bumped setuptools build requirement to >=83.0.0 to address PYSEC-2026-3447.
- Rejection marker is now cleared only when the stage's work actually completes (when the completion marker is created), not when a `queue_agent` action re-queues work below `max_rejections`. Applies to both regular and chained stages.
- Rejection count now increments only when the stage's trigger actually fires (i.e., the stage will be re-processed this tick), rather than on every tick regardless of the trigger.
- FILE-type rejection markers never accumulate a rejection count; when used with `max_rejections`, the marker is simply deleted (FILE markers can't store data).
- Rejection count is now written back into the JSON rejection marker below `max_rejections` so it accumulates across ticks instead of being lost when the marker is cleared.
- `retry_count` is now read from the actual on-disk processing marker data (`stage_state.processing_data`) instead of the static config content, so it is correctly preserved across re-queues by a `queue_agent` action.
- Pipeline now stops chaining when a custom action returns `data: {"async": true}`, matching `queue_agent` behavior; previously async custom actions would incorrectly chain to the next stage.
- `ActionHandlerConfig.from_dict` now treats an empty `params: {}` dict as present (not missing), so other top-level keys are no longer incorrectly merged into `params`.
- `ActionHandlerConfig.from_dict` now raises a `ValueError` with a clear message when the required `type` field is missing, instead of an unhandled `KeyError`.
- `TickResult.__str__` now includes stderr output, so captured tracebacks are shown to the user.
- `tick()` exception handler now reports the actual failing target instead of `*`.
- `tick_all()` now continues processing remaining targets even if one raises an exception, and captures the traceback in the returned TickResult's stderr field.
- `MarkerSpec.resolve_path` now rejects path traversal (`..` segments) and absolute paths that escape the workspace.
- `MarkerSpec.resolve_path` now resolves the base directory (following symlinks) before the traversal check and returns the fully-resolved path, preventing a TOCTOU race where a symlinked base directory could be swapped between the check and use.
- Template substitution failures in `command`-type actions, `conversation_queue`, and `run_diagnostic` now return an `ACTION_FAILED` result with the error message instead of silently using the unformatted template.
- The issue store's YAML frontmatter parser (_parse_value) now correctly parses boolean values (true/false/yes/no) and null values (null/none/~) instead of treating them as truthy strings. This fixes config toggles like enabled: false being treated as truthy when read from issue frontmatter.
- The issue store's YAML frontmatter parser (parse_frontmatter) now correctly handles quoted values containing colons (e.g. `title: "Fix: Login bug"`), instead of truncating the value at the first colon.
- The SWE plugin now normalizes naive datetime strings to UTC when parsing timestamps from GitHub session files, review issue frontmatter, and queued markers (doc_sync, pr_review), preventing comparison errors and incorrect behavior when naive datetimes are compared against timezone-aware datetimes.
- The SWE plugin's `_git` helper now enforces a timeout (default 60 seconds) on git subprocess calls, so a hung git command can no longer block the pipeline indefinitely. On timeout a `TimeoutExpired` is raised (with stdout/stderr decoded to str), and callers (`ensure_phase_a_branch`, `commit_phase_a_change`, `run_c_doc_sync`) now handle `TimeoutExpired` gracefully.
- When a stale sync action (command, subprocess, or custom) is re-executed successfully, the pipeline now creates the stage's produced markers and completion marker, clears the rejection marker, and invalidates other stages' markers — matching the behavior of the normal (non-stale) execution path. Previously, re-executed stale sync actions never created their completion marker, so the stage would be re-triggered on every subsequent tick.
- The `run_diagnostic` action now returns `success=False` when the underlying diagnostic command exits with a non-zero exit code (e.g. a linter finding errors, a test suite failing, a security scanner finding vulnerabilities), instead of always returning `success=True`. The report file is still written regardless of the exit code.
- The `run_a9_dep_audit` action (pip-audit dependency scanner) now proceeds to parse the diagnostic output and create dependency-audit issues even when the diagnostic command exits non-zero (the normal case when pip-audit finds vulnerabilities), and forces `result.success = True` after creating the issues so the stage completes successfully. Previously it returned early when the command failed, so vulnerabilities were never turned into issues.

### Security
- Addressed bandit findings: added nosec annotations for intentional subprocess/shell usage, resolved git binary via shutil.which, and validated HTTP URL schemes.
- `http_request` action handler now redacts URLs (removes userinfo and query params) in result data to avoid leaking sensitive information.
- Template-substituted variables (`target`, `target_dir`, `workspace_dir`, and target config values) are now shell-quoted with `shlex.quote()` before substitution into `command`-type actions and `run_diagnostic` commands, preventing command injection when values contain shell metacharacters.
- `command`-type actions and `run_diagnostic` now execute commands via an argument list (`shell=False` using `shlex.split()`) instead of a shell, preventing shell injection through template-substituted values.
- The `run_lint_autofix` action in the SWE plugin now executes its command via an argument list (`shell=False` using `shlex.split()`) instead of a shell, preventing command injection through template-substituted values; invalid commands return an ACTION_FAILED result.
- The `_run` helper in the issue-fix plugin now executes commands via an argument list (`shell=False` using `shlex.split()`) instead of a shell, preventing command injection through template-substituted values; invalid commands return an error result.
- `run_diagnostic` now validates command config values (e.g. `test_cmd`, `lint_cmd`) and rejects values containing shell metacharacters.
- `format_template` now raises `ValueError` on substitution failure (missing key, bad format) instead of silently returning the unformatted template; callers return an `ACTION_FAILED` result with the error message.
- Fixed prompt template escaping in SWE plugin and prompt builders so JSON literals embedded in prompts render correctly after `.format()` substitution.
- Conversation queue handler now sanitizes agent names (replacing any non-alphanumeric, underscore, dot, or hyphen character with `_`) before using them in queue filenames, and validates the resolved queue file path with `.resolve()`/`is_relative_to()`; queue files that would escape the queue directory now raise a `ValueError`, preventing path traversal attacks.
- File-based trigger conditions (`file_missing`, `file_exists`, `file_older_than`, `marker_state`) now validate their `path` against path traversal, rejecting `..` segments, absolute paths, and paths that resolve outside the workspace/base directory (including via symlinks), raising a `ValueError` for invalid paths.
- The `queue_empty` trigger now validates its `queue_dir` value against path traversal, rejecting `..` segments and paths that resolve outside the base directory (including via symlinks), raising a `ValueError` for invalid paths.
- The issue store's `create_issue()` now rejects issue IDs containing `..` (e.g. `../../evil`, `foo..bar`, or `..`) by raising a `ValueError`, instead of sanitizing them; single dots remain allowed (e.g. `foo.bar`). For other characters, the issue ID is sanitized before use as a filename: any character that is not alphanumeric, dot, underscore, or hyphen is replaced with `-` (leading/trailing dashes stripped), and an empty sanitized ID falls back to `issue`. The resolved path is validated with `.resolve()`/`is_relative_to()`; a filename that would still escape the issues directory raises a `ValueError`. This prevents path traversal attacks via malicious issue IDs (e.g. absolute paths like `/etc/passwd`).
