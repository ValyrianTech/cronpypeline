"""Serendipity conversation queue action handler.

Writes JSON files to a conversation queue directory. The conversation_queue_monitor
picks them up asynchronously and dispatches them to agents.
"""

import json
import time
import uuid
from pathlib import Path
from typing import Any

from cronpypeline.actions import ActionHandler, ActionResult, TickContext, format_template
from cronpypeline.config import ActionSpec, ActionType
from cronpypeline.actions import register_handler


class ConversationQueueHandler(ActionHandler):
    """Action handler that writes JSON files to a conversation queue directory.

    The JSON file format is compatible with Serendipity's conversation_queue_monitor.
    """

    def __init__(self, queue_dir: str, agent_settings_dir: str = None) -> None:
        self.queue_dir = Path(queue_dir)
        self.agent_settings_dir = Path(agent_settings_dir) if agent_settings_dir else None

    def execute(self, action: ActionSpec, context: TickContext) -> ActionResult:
        if context.dry_run:
            return ActionResult(success=True, dry_run=True)

        params = action.params
        agent = params.get("agent", "default")
        prompt = params.get("prompt", "")
        prompt_template = params.get("prompt_template", "")

        # Format prompt with context variables
        variables = {
            "target": context.target,
            "target_dir": str(context.target_dir),
            "workspace_dir": str(context.workspace_dir),
        }
        if prompt_template:
            prompt = format_template(prompt_template, variables)
        else:
            prompt = format_template(prompt, variables)

        # Build queue entry
        entry = {
            "id": str(uuid.uuid4()),
            "agent": agent,
            "prompt": prompt,
            "target": context.target,
            "timestamp": time.time(),
            "model": params.get("model"),
            "temperature": params.get("temperature"),
            "max_tokens": params.get("max_tokens"),
        }

        # Remove None values
        entry = {k: v for k, v in entry.items() if v is not None}

        # Load agent settings if configured
        if self.agent_settings_dir:
            agent_config_path = self.agent_settings_dir / f"{agent}.json"
            if agent_config_path.exists():
                agent_config = json.loads(agent_config_path.read_text())
                entry["agent_config"] = agent_config

        # Write to queue
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        queue_file = self.queue_dir / f"{entry['id']}.json"
        queue_file.write_text(json.dumps(entry, indent=2))

        return ActionResult(
            success=True,
            stdout=f"Queued agent {agent} for target {context.target}",
            data={"queue_file": str(queue_file), "entry_id": entry["id"]},
        )

    def check_complete(self, action: ActionSpec, context: TickContext) -> bool:
        """Check if the queue is empty (all agents have finished)."""
        if not self.queue_dir.exists():
            return True
        return not any(self.queue_dir.iterdir())


def register():
    """Register the conversation queue handler."""
    register_handler(ActionType.QUEUE_AGENT, "placeholder")  # Will be replaced by instance
