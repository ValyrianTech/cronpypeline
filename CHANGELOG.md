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
- `retry_count` is now reset to 0 in the processing marker when a `queue_agent` action re-queues work (previously carried over from the stale processing marker, causing incorrect retry counting).
- Pipeline now stops chaining when a custom action returns `data: {"async": true}`, matching `queue_agent` behavior; previously async custom actions would incorrectly chain to the next stage.
- `ActionHandlerConfig.from_dict` now treats an empty `params: {}` dict as present (not missing), so other top-level keys are no longer incorrectly merged into `params`.

### Changed
- `configs/swe_pipeline.json` now defines `processing` markers for the A2-fix-agent, A6-fix-agent, C-code, and C-review stages, aligning the example config with the async processing-marker behavior.

### Security
- Addressed bandit findings: added nosec annotations for intentional subprocess/shell usage, resolved git binary via shutil.which, and validated HTTP URL schemes.
