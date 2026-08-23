# Changelog

## [Unreleased]

### Fixed
- Chained stage failure now reported in TickResult with ACTION_FAILED status and the failed stage ID.
- Chained stage on_fail actions are now executed when a chained stage fails (previously silently ignored).
- Chained stage failure message no longer has a trailing colon when the action produces no output.
- StageState.is_actionable now treats a rejected stage as actionable when max_rejections=0 (rejection tracking disabled).
- mode_file and config_file paths are now resolved relative to workspace_dir instead of the current working directory.
- http_request action handler now rejects non-http/https URL schemes (e.g. file://) to prevent arbitrary file access.
- Bumped setuptools build requirement to >=83.0.0 to address PYSEC-2026-3447.

### Security
- Addressed bandit findings: added nosec annotations for intentional subprocess/shell usage, resolved git binary via shutil.which, and validated HTTP URL schemes.
