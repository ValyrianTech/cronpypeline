"""Serendipity conversation queue action handler.

Writes JSON files to a conversation queue directory. The conversation_queue_monitor
picks them up asynchronously and dispatches them to agents.
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cronpypeline.actions import (
    ActionHandler,
    ActionResult,
    TickContext,
    build_template_variables,
    format_template,
)
from cronpypeline.config import ActionSpec
from cronpypeline.io_utils import write_json_atomic
from cronpypeline.template_safety import (
    _is_scrubbable_key,
    scrub_secrets_deep,
)


class ConversationQueueHandler(ActionHandler):
    """Action handler that writes JSON files to a conversation queue directory.

    The JSON file format is compatible with Serendipity's conversation_queue_monitor.

    :param queue_dir: Directory to write queue entry JSON files.
    :param agent_settings_dir: Optional directory with per-agent JSON settings.
    :param prompt_field: Name of the field for the prompt text (default ``"prompt"``).
        Set to ``"content"`` for Serendipity compatibility.
    :param default_fields: Static fields injected into every queue entry.
        E.g. ``{"sender": "SWE_PIPELINE", "folder_name": "SWE", "runs_left": 3}``.
    :param flatten_agent_settings: When True, merge agent settings flat into the entry
        instead of nesting under ``"agent_config"``.
    """

    def __init__(
        self,
        queue_dir: str,
        agent_settings_dir: str | None = None,
        prompt_field: str = "prompt",
        default_fields: dict[str, Any] | None = None,
        flatten_agent_settings: bool = False,
    ) -> None:
        """Initialize the conversation queue handler.

        :param queue_dir: Directory to write queue entry JSON files.
        :type queue_dir: str
        :param agent_settings_dir: Optional directory with per-agent JSON settings.
        :type agent_settings_dir: str | None
        :param prompt_field: Name of the field for the prompt text (default ``"prompt"``).
        :type prompt_field: str
        :param default_fields: Static fields injected into every queue entry.
        :type default_fields: dict[str, Any] | None
        :param flatten_agent_settings: When True, merge agent settings flat into the entry
            instead of nesting under ``"agent_config"``.
        :type flatten_agent_settings: bool
        """
        self.queue_dir = Path(queue_dir)
        self.agent_settings_dir = Path(agent_settings_dir) if agent_settings_dir else None
        self.prompt_field = prompt_field
        self.default_fields = dict(default_fields) if default_fields else {}
        self.flatten_agent_settings = flatten_agent_settings

    def _agent_settings_path(self, safe_agent: str) -> Path | None:
        """Resolve the settings file for an agent.

        Supports both layouts:

        - flat: ``<agent_settings_dir>/<Agent>.json``
        - nested: ``<agent_settings_dir>/<Agent>/settings.json`` (Serendipity)

        :param safe_agent: Sanitized agent name.
        :returns: Path to the settings file, or None when neither layout exists.
        """
        if self.agent_settings_dir is None:
            return None
        flat = self.agent_settings_dir / f"{safe_agent}.json"
        if flat.exists():
            return flat
        nested = self.agent_settings_dir / safe_agent / "settings.json"
        if nested.exists():
            return nested
        return None

    def execute(self, action: ActionSpec, context: TickContext) -> ActionResult:
        """Execute the queue action: build a prompt and write a JSON entry.

        :param action: Action spec with agent, prompt/prompt_template, and optional params.
        :param context: Tick context for template substitution and retry handling.
        :returns: Result with queue file path and entry ID.
        """
        if context.dry_run:
            return ActionResult(success=True, dry_run=True)

        params = action.params
        agent = params.get("agent", "default")
        safe_agent = re.sub(r"[^A-Za-z0-9_.-]", "_", agent)
        if safe_agent in ("", ".", ".."):
            safe_agent = "default"
        prompt = params.get("prompt", "")
        prompt_template = params.get("prompt_template", "")
        reminder_prompt = params.get("reminder_prompt", "")
        reminder_prompt_template = params.get("reminder_prompt_template", "")

        # Determine which prompt to use based on retry context
        is_retry = context.retry_count > 0
        if is_retry and reminder_prompt_template:
            prompt = reminder_prompt_template
            is_template = True
        elif is_retry and reminder_prompt:
            prompt = reminder_prompt
            is_template = False
        elif prompt_template:
            prompt = prompt_template
            is_template = True
        else:
            # use prompt as-is
            is_template = False

        # Format prompt with context variables
        variables = build_template_variables(context)
        try:
            if is_template:
                prompt = format_template(prompt, variables)
            else:
                # Plain prompts may contain unescaped braces from dynamic content
                # (e.g., issue bodies with JSON literals). Try to format, but fall
                # back to using the prompt as-is if formatting fails.
                try:
                    prompt = format_template(prompt, variables)
                except ValueError:
                    pass
        except ValueError as e:
            return ActionResult(success=False, stderr=str(e))

        # Build queue entry — start with default fields, then add dynamic fields
        entry = scrub_secrets_deep(dict(self.default_fields))

        # Apply runs_left decrement on retry
        if is_retry and "runs_left" in self.default_fields:
            entry["runs_left"] = max(0, self.default_fields["runs_left"] - context.retry_count)

        # Add core fields — no `id` field; Serendipity assigns its own
        # conversation IDs internally. conversation_id stays as whatever
        # default_fields sets (typically "").
        entry["agent"] = agent
        entry[self.prompt_field] = prompt
        entry["target"] = context.target
        entry["timestamp"] = time.time()

        # Add optional model/temperature/max_tokens from action params
        _standard_params = {
            "agent", "prompt", "prompt_template", "reminder_prompt",
            "reminder_prompt_template", "model", "temperature", "max_tokens",
        }
        for opt_key in ("model", "temperature", "max_tokens"):
            val = params.get(opt_key)
            if val is not None:
                entry[opt_key] = val

        # Copy any non-standard action params into the entry (override defaults),
        # skipping sensitive-looking keys so secrets never reach the queue file.
        for pk, pv in params.items():
            if pk in _standard_params or pv is None or _is_scrubbable_key(str(pk)):
                continue
            entry[pk] = scrub_secrets_deep(pv)

        # Load agent settings if configured. Malformed or unreadable settings
        # files fail open (skipped) rather than aborting the whole pipeline tick.
        if self.agent_settings_dir:
            agent_config_path = self._agent_settings_path(safe_agent)
            if agent_config_path is not None:
                try:
                    agent_config = json.loads(
                        agent_config_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    agent_config = None
                if isinstance(agent_config, dict):
                    if self.flatten_agent_settings:
                        # Merge flat — agent settings override defaults but not action params
                        for ak, av in agent_config.items():
                            if _is_scrubbable_key(str(ak)):
                                continue
                            if ak not in entry or entry[ak] == self.default_fields.get(ak):
                                entry[ak] = scrub_secrets_deep(av)
                    else:
                        entry["agent_config"] = scrub_secrets_deep(agent_config)

        # Remove None values
        entry = {k: v for k, v in entry.items() if v is not None}

        # Write to queue — filename based on agent name and timestamp,
        # matching the format used by the original SWE pipeline runner.
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        queue_filename = f"{safe_agent}_{ts}.json"
        queue_file = (self.queue_dir / queue_filename).resolve()
        if not queue_file.is_relative_to(self.queue_dir.resolve()):
            raise ValueError(f"Queue file escapes queue directory: {queue_filename}")
        write_json_atomic(queue_file, entry)

        return ActionResult(
            success=True,
            stdout=f"Queued agent {agent} for target {context.target}",
            data={"queue_file": str(queue_file), "entry_id": queue_filename},
        )

    def check_complete(self, action: ActionSpec, context: TickContext) -> bool:
        """Check if the queue is empty (all agents have finished).

        :param action: Action spec (unused).
        :param context: Tick context (unused).
        :returns: True if the queue directory is empty or doesn't exist.
        """
        if not self.queue_dir.exists():
            return True
        return not any(self.queue_dir.iterdir())


def register() -> None:
    """Register the conversation queue handler.

    Note: This is a no-op placeholder. The actual handler must be instantiated
    with queue_dir and registered via ``Pipeline.__init__()`` from action_handler config,
    or manually via ``register_handler(ActionType.QUEUE_AGENT, ConversationQueueHandler(...))``.

    :returns: None.
    """
