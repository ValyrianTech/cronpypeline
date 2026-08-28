# Changelog

## [Unreleased]

### Fixed
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
- `TickResult.__str__` now includes stderr output, so captured tracebacks are shown to the user.
- `tick()` exception handler now reports the actual failing target instead of `*`.
- `tick_all()` now continues processing remaining targets even if one raises an exception, and captures the traceback in the returned TickResult's stderr field.
- `MarkerSpec.resolve_path` now rejects path traversal (`..` segments) and absolute paths that escape the workspace.
- Template substitution failures in `command`-type actions, `conversation_queue`, and `run_diagnostic` now return an `ACTION_FAILED` result with the error message instead of silently using the unformatted template.

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
