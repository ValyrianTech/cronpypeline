"""Serendipity conversation queue action handler.

Writes JSON files to a conversation queue directory. The conversation_queue_monitor
picks them up asynchronously and dispatches them to agents.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cronpypeline.actions import (
    ActionHandler,
    ActionResult,
    TickContext,
    format_template,
)
from cronpypeline.config import ActionSpec


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
        prompt = params.get("prompt", "")
        prompt_template = params.get("prompt_template", "")
        reminder_prompt = params.get("reminder_prompt", "")
        reminder_prompt_template = params.get("reminder_prompt_template", "")

        # Determine which prompt to use based on retry context
        is_retry = context.retry_count > 0
        if is_retry and reminder_prompt_template:
            prompt = reminder_prompt_template
        elif is_retry and reminder_prompt:
            prompt = reminder_prompt
        elif prompt_template:
            prompt = prompt_template
        # else: use prompt as-is

        # Format prompt with context variables
        variables = {
            "target": context.target,
            "target_dir": str(context.target_dir),
            "workspace_dir": str(context.workspace_dir),
            "target_config": context.target_config,
        }
        # Flatten target_config keys for direct template access (e.g. {test_cmd})
        for k, v in context.target_config.items():
            if k not in variables:
                variables[k] = v
        try:
            if prompt_template:
                prompt = format_template(prompt_template, variables)
            else:
                prompt = format_template(prompt, variables)
        except ValueError as e:
            return ActionResult(success=False, stderr=str(e))

        # Build queue entry — start with default fields, then add dynamic fields
        entry = dict(self.default_fields)

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

        # Copy any non-standard action params into the entry (override defaults)
        for pk, pv in params.items():
            if pk not in _standard_params and pv is not None:
                entry[pk] = pv

        # Load agent settings if configured
        if self.agent_settings_dir:
            agent_config_path = self.agent_settings_dir / f"{agent}.json"
            if agent_config_path.exists():
                agent_config = json.loads(agent_config_path.read_text())
                if self.flatten_agent_settings:
                    # Merge flat — agent settings override defaults but not action params
                    for ak, av in agent_config.items():
                        if ak not in entry or entry[ak] == self.default_fields.get(ak):
                            entry[ak] = av
                else:
                    entry["agent_config"] = agent_config

        # Remove None values
        entry = {k: v for k, v in entry.items() if v is not None}

        # Write to queue — filename based on agent name and timestamp,
        # matching the format used by the original SWE pipeline runner.
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        queue_filename = f"{agent}_{ts}.json"
        queue_file = self.queue_dir / queue_filename
        queue_file.write_text(json.dumps(entry, indent=2))

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
