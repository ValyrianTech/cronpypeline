"""VNN prompt builder custom action callables.

Provides:
- `build_ranking_prompt`: Build a prompt asking the ranking agent to order the
  country's compiled stories.
- `build_compile_prompt`: Build a prompt asking the compiler agent to turn
  archived news links into stories.
- `build_research_prompt`: Build a prompt for the research agent.
- `build_writing_prompt`: Build a prompt for the article writer.
- `build_publishing_prompt`: Build a prompt for the publisher.
- `build_revision_prompt`: Build a prompt for the revision agent.
- `queue_ranking_agent`, `queue_compile_agent`, `queue_research_agent`,
  `queue_writing_agent`, `queue_publishing_agent`, `queue_revision_agent`:
  custom actions that build the matching prompt and queue it.
"""

import shutil
from pathlib import Path
from typing import Any

from cronpypeline.actions import ActionResult, TickContext
from cronpypeline.config import ActionSpec
from cronpypeline.plugins.conversation_queue import ConversationQueueHandler
from cronpypeline.plugins.vnn_plugin import NEWS_LINKS_SUBDIR, _compiled_link_ids
from cronpypeline.plugins.vnn_ranking import (
    COMPILED_SUBDIR,
    RANKING_FILENAME,
    compiled_dir,
    load_active_story,
    load_ranking,
    ranking_stories,
    rejection_count,
    story_artifacts,
    story_dir,
    today_utc,
    unranked_stories,
    write_active_story,
)

# ─── Prompt builders ────────────────────────────────────────────────────────


def _story_bullets(stories: list[dict[str, Any]]) -> str:
    """Render compiled stories as a markdown bullet list for a prompt.

    :param stories: Compiled story dicts.
    :returns: Markdown bullet list, or a placeholder when there are none.
    """
    if not stories:
        return "_No compiled stories found._"
    lines = []
    for story in stories:
        lines.append(
            f"- **{story['story_id']}** — {story.get('title_en', '(untitled)')}\n"
            f"  - Category: {story.get('category', 'unknown')}\n"
            f"  - Articles: {story.get('article_count', 0)}\n"
            f"  - Summary: {story.get('summary_en', '')}\n"
            f"  - File: {story.get('file_path', '')}"
        )
    return "\n".join(lines)


def _already_ranked_rule(already_ranked: list[dict[str, Any]]) -> str:
    """Render the rule telling the agent to omit already-ranked ids.

    :param already_ranked: Stories already present in the ranking.
    :returns: A rule line ending in a newline, or an empty string when there is
        no previous ranking.
    """
    if not already_ranked:
        return ""
    return "- Do not include ids from **Already ranked** — they stay where they are.\n"


def build_ranking_prompt(
    country: str,
    date: str,
    stories: list[dict[str, Any]],
    previous: dict[str, Any] | None = None,
    extra_instructions: str = "",
) -> str:
    """Build a prompt for the ranking agent.

    :param country: Country name being ranked.
    :param date: Date in ``YYYYMMDD`` form.
    :param stories: Compiled stories that need to be ranked.
    :param previous: Existing ranking document, if any.
    :param extra_instructions: Optional additional instructions to append.
    :returns: Formatted prompt string.
    """
    already_ranked = ranking_stories(previous)
    prompt = f"""You are ranking news stories for **{country}** on **{date}**.

## Stories to rank

{_story_bullets(stories)}
"""
    if already_ranked:
        prompt += "\n## Already ranked\n\n"
        for story in already_ranked:
            prompt += f"- {story.get('rank')}. {story['story_id']} — {story.get('title_en', '')}\n"
        prompt += (
            "\nThese stories are already in the ranking. Keep them in their current "
            "relative order and do NOT include them in the order you submit — the "
            "script merges your new stories into the existing ranking.\n"
        )

    prompt += f"""
## Task

Order the stories from most to least newsworthy. Judge by impact, novelty,
breadth of coverage, and how many independent sources reported it. Stories
covering the same event should be adjacent.

Save the order with the ranking skill's helper script:

```
RunSkillScript(skill_name="news-ranking", script_name="save_ranking.py",
               arguments='--country "{country}" --order "<id>,<id>,<id>"')
```

Pass the story ids in your chosen order, best first, comma-separated. For long
lists you can write one id per line to a file and pass `--order-file <path>`
instead.

Rules:
- Use every story id from **Stories to rank** exactly once.
{_already_ranked_rule(already_ranked)}- Do not invent story ids and do not drop any — the script rejects both.
- Do not write `{country}/{RANKING_FILENAME}` yourself. The script validates the
  order, fills in the metadata and preserves the `state` of stories that were
  already ranked, so article progress is never reset.
"""
    if extra_instructions:
        prompt += f"\n## Additional Instructions\n\n{extra_instructions}\n"
    return prompt


def build_compile_prompt(
    country: str,
    date: str,
    link_count: int,
    extra_instructions: str = "",
) -> str:
    """Build a prompt for the news compiler agent.

    :param country: Country being compiled.
    :param date: Date in ``YYYYMMDD`` form.
    :param link_count: Number of archived links available.
    :param extra_instructions: Optional additional instructions to append.
    :returns: Formatted prompt string.
    """
    prompt = f"""Load the news-compiler skill and compile the news for **{country}** on **{date}**.

There are {link_count} archived links available for this country.

## Task

1. Use `load_articles.py --country "{country}"` to read the archived links that
   have not been compiled yet. Links already covered by an existing compiled
   story are excluded automatically.
2. Group related links into stories and save each story with
   `save_story.py --country "{country}"`. Stories are written to
   `{country}/{COMPILED_SUBDIR}/` and each file is named `story_<id>.json`.
3. When every story is saved, mark the compilation complete:

```
RunSkillScript(skill_name="news-compiler", script_name="mark_compilation.py",
               arguments='--country "{country}"')
```

   This writes `{country}/compilation_metadata.json`, recording which links were
   compiled so the next run only picks up new ones. Do not write that file
   yourself.

Do not rank the stories — a separate agent does that.
"""
    if extra_instructions:
        prompt += f"\n## Additional Instructions\n\n{extra_instructions}\n"
    return prompt


def _story_context(country: str, story_id: str, story_path: Path) -> str:
    """Render the shared story context block used by article prompts.

    :param country: Country name.
    :param story_id: Story identifier.
    :param story_path: Story directory.
    :returns: Markdown context block.
    """
    found = story_artifacts(story_path)
    present = [name for name, exists in found.items() if exists]
    return f"""- **Country**: {country}
- **Story ID**: {story_id}
- **Story directory**: {story_path}
- **Artefacts present**: {', '.join(present) if present else 'none'}
- **Rejections so far**: {rejection_count(story_path)}
"""


def build_research_prompt(
    country: str,
    story_id: str,
    story_path: Path,
    extra_instructions: str = "",
) -> str:
    """Build a prompt for the news research agent.

    :param country: Country name.
    :param story_id: Story to research.
    :param story_path: Story directory.
    :param extra_instructions: Optional additional instructions to append.
    :returns: Formatted prompt string.
    """
    prompt = f"""Load the news-research skill and research this story.

## Story

{_story_context(country, story_id, story_path)}
## Task

1. Read the story metadata from `{story_path / 'story.json'}`.
2. Research the story using the article links in that metadata.
3. Save the research report with `save_research.py --complete` so that
   `{story_path / 'research.md'}` is written and the processing marker is cleared.
"""
    if extra_instructions:
        prompt += f"\n## Additional Instructions\n\n{extra_instructions}\n"
    return prompt


def build_writing_prompt(
    country: str,
    story_id: str,
    story_path: Path,
    extra_instructions: str = "",
) -> str:
    """Build a prompt for the article writer agent.

    :param country: Country name.
    :param story_id: Story to write about.
    :param story_path: Story directory.
    :param extra_instructions: Optional additional instructions to append.
    :returns: Formatted prompt string.
    """
    prompt = f"""Load the article-writer skill and write the article for this story.

## Story

{_story_context(country, story_id, story_path)}
## Task

1. Use `load_story.py` to read the story metadata and the research report.
2. Write the article following the skill instructions.
3. Save it with `save_article.py` so that `{story_path / 'article.md'}` is
   written and the processing marker is cleared.
"""
    if extra_instructions:
        prompt += f"\n## Additional Instructions\n\n{extra_instructions}\n"
    return prompt


def build_publishing_prompt(
    country: str,
    story_id: str,
    story_path: Path,
    extra_instructions: str = "",
) -> str:
    """Build a prompt for the article publisher agent.

    :param country: Country name.
    :param story_id: Story to publish.
    :param story_path: Story directory.
    :param extra_instructions: Optional additional instructions to append.
    :returns: Formatted prompt string.
    """
    prompt = f"""Load the article-publisher skill and review the article for this story.

## Story

{_story_context(country, story_id, story_path)}
## Task

1. Use `load_article.py` to read the article.
2. Review it against the skill's quality bar.
3. If it is acceptable, publish it with `publish_article.py`. That writes
   `{story_path / 'published.json'}` and releases the active-story lock.
4. If it is not acceptable, reject it with `reject_article.py`, which renames
   the article to `rejected-article.md` and records the reason.
"""
    if extra_instructions:
        prompt += f"\n## Additional Instructions\n\n{extra_instructions}\n"
    return prompt


def build_revision_prompt(
    country: str,
    story_id: str,
    story_path: Path,
    extra_instructions: str = "",
) -> str:
    """Build a prompt for the article revision agent.

    :param country: Country name.
    :param story_id: Story to revise.
    :param story_path: Story directory.
    :param extra_instructions: Optional additional instructions to append.
    :returns: Formatted prompt string.
    """
    prompt = f"""Load the article-revision skill and revise the rejected article.

## Story

{_story_context(country, story_id, story_path)}
## Task

1. Use `load_rejected_article.py` to read `{story_path / 'rejected-article.md'}`,
   the revision notes and the rejection history.
2. Address every point raised by the publisher.
3. Save the revised article with `save_revised_article.py` so that
   `{story_path / 'article.md'}` is written and the rejection files are cleared.
"""
    if extra_instructions:
        prompt += f"\n## Additional Instructions\n\n{extra_instructions}\n"
    return prompt


# ─── Queue action callables ─────────────────────────────────────────────────


def _build_queue_handler(params: dict[str, Any], context: TickContext) -> ConversationQueueHandler:
    """Build a ConversationQueueHandler from action params, falling back to pipeline config.

    Stage action params take precedence; missing keys fall back to the pipeline's
    top-level ``action_handler`` config so that ``queue_dir``, ``prompt_field``,
    ``default_fields``, etc. don't need to be repeated in every stage.

    :param params: Action params dict with optional queue settings.
    :param context: Tick context with pipeline reference for fallback.
    :returns: A :class:`ConversationQueueHandler` instance.
    :raises ValueError: If queue_dir is missing, empty, or contains path
        traversal (``..``) segments.
    """
    fallback: dict[str, Any] = {}
    pipeline = getattr(context, "pipeline", None)
    if pipeline is not None and pipeline.config.action_handler is not None:
        fallback = dict(pipeline.config.action_handler.params)
    merged = {**fallback, **{k: v for k, v in params.items() if v is not None}}
    queue_dir = merged.get("queue_dir", "")
    if not queue_dir:
        raise ValueError("queue_dir is required for ConversationQueueHandler")
    handler = ConversationQueueHandler(
        queue_dir=queue_dir,
        agent_settings_dir=merged.get("agent_settings_dir"),
        prompt_field=merged.get("prompt_field", "prompt"),
        default_fields=merged.get("default_fields"),
        flatten_agent_settings=merged.get("flatten_agent_settings", False),
    )
    qd = Path(handler.queue_dir)
    if not str(qd).strip():
        raise ValueError(f"queue_dir is required: {handler.queue_dir}")
    if ".." in qd.parts:
        raise ValueError(f"queue_dir contains path traversal: {handler.queue_dir}")
    if handler.agent_settings_dir is not None:
        asd = Path(handler.agent_settings_dir)
        if not str(asd).strip():
            raise ValueError(f"agent_settings_dir is required: {handler.agent_settings_dir}")
        if not asd.is_absolute():
            handler.agent_settings_dir = context.workspace_dir / asd
            asd = handler.agent_settings_dir
        if ".." in asd.parts:
            raise ValueError(f"agent_settings_dir contains path traversal: {handler.agent_settings_dir}")
    return handler


def _queue_prompt(action: ActionSpec, context: TickContext, prompt: str) -> ActionResult:
    """Queue a pre-built prompt for the configured agent.

    :param action: Action spec carrying the agent and queue settings.
    :param context: Tick context with target and directories.
    :param prompt: Prompt text to queue.
    :returns: Result from the queue handler, tagged ``async`` on success.
    """
    params = action.params
    handler = _build_queue_handler(params, context)
    queue_action = ActionSpec(
        type=action.type,
        params={
            "agent": params.get("agent", "default"),
            "prompt": prompt,
        },
    )
    result = handler.execute(queue_action, context)
    if result.success:
        result.data = {**result.data, "async": True}
    return result


def _ensure_story_dir(
    target_dir: Path,
    story_id: str,
    stage: str,
) -> Path:
    """Materialise a story directory and claim the active-story lock.

    The research stage is the first stage that needs a story directory, so it
    creates ``<country>/stories/<story_id>/`` and copies the compiled
    ``story.json`` into it. Later stages reuse the existing directory.

    :param target_dir: Country target directory.
    :param story_id: Story to prepare.
    :param stage: Pipeline stage claiming the story.
    :returns: Path to the story directory.
    """
    path = story_dir(target_dir, story_id)
    path.mkdir(parents=True, exist_ok=True)

    story_json = path / "story.json"
    if not story_json.exists():
        compiled = compiled_dir(target_dir) / f"{story_id}.json"
        if compiled.exists():
            shutil.copyfile(compiled, story_json)

    write_active_story(target_dir, story_id, stage)
    return path


def _resolve_story(context: TickContext) -> tuple[str, Path] | None:
    """Resolve the story this tick should work on.

    Prefers the country's ``.active_story`` lock; falls back to the first
    ranked story that still needs article work. The story directory is created
    and the lock claimed when the story has not been started yet.

    :param context: Tick context with target and directories.
    :returns: ``(story_id, story_path)`` or None when there is no story to work on.
    """
    target_dir = context.target_dir
    active = load_active_story(target_dir)
    if active is not None:
        story_id = str(active["story_id"])
        return story_id, _ensure_story_dir(
            target_dir, story_id, str(active.get("stage") or "research")
        )

    for story in ranking_stories(load_ranking(target_dir)):
        if story.get("state") in ("pending", "researching", "writing", "publishing"):
            story_id = str(story["story_id"])
            return story_id, _ensure_story_dir(target_dir, story_id, "research")
    return None


def queue_ranking_agent(action: ActionSpec, context: TickContext) -> ActionResult:
    """Build a ranking prompt and queue it.

    Expected action.params:
        - agent: Agent name to queue
        - queue_dir / agent_settings_dir / prompt_field / default_fields /
          flatten_agent_settings: Queue settings (fall back to pipeline config)
        - extra_instructions: Optional extra instructions for the prompt

    :param action: Action spec with queue params.
    :param context: Tick context with target and directories.
    :returns: Result from the queue handler.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    target_dir = context.target_dir
    date = today_utc()
    stories = unranked_stories(target_dir)
    if not stories:
        # The trigger and the action both read the ranking, so the agent may
        # have written ranking.json between the two. Nothing to do is a no-op,
        # not a failure: the stage's completion signal is already satisfied.
        return ActionResult(
            success=True,
            stdout="No unranked compiled stories to rank",
            data={"skipped": True},
        )

    prompt = build_ranking_prompt(
        country=context.target,
        date=date,
        stories=stories,
        previous=load_ranking(target_dir),
        extra_instructions=action.params.get("extra_instructions", ""),
    )
    return _queue_prompt(action, context, prompt)


def _pending_link_count(target_dir: Path) -> int:
    """Count archived links that have not been compiled yet.

    :param target_dir: Country target directory.
    :returns: Number of link files not recorded in ``compilation_metadata.json``.
    """
    links_dir = target_dir / NEWS_LINKS_SUBDIR
    if not links_dir.is_dir():
        return 0
    current = {p.stem for p in links_dir.glob("*.json") if not p.name.startswith(".")}
    return len(current - _compiled_link_ids(target_dir))


def queue_compile_agent(action: ActionSpec, context: TickContext) -> ActionResult:
    """Build a compilation prompt and queue it.

    Expected action.params: same queue settings as :func:`queue_ranking_agent`,
    plus an optional ``link_count`` override.

    :param action: Action spec with queue params.
    :param context: Tick context with target and directories.
    :returns: Result from the queue handler.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    link_count = action.params.get("link_count")
    if link_count is None:
        link_count = _pending_link_count(context.target_dir)

    prompt = build_compile_prompt(
        country=context.target,
        date=today_utc(),
        link_count=int(link_count),
        extra_instructions=action.params.get("extra_instructions", ""),
    )
    return _queue_prompt(action, context, prompt)


def _queue_story_prompt(
    action: ActionSpec,
    context: TickContext,
    builder: Any,
) -> ActionResult:
    """Resolve the active story and queue a story-scoped prompt.

    :param action: Action spec with queue params.
    :param context: Tick context with target and directories.
    :param builder: Prompt builder taking ``(country, story_id, story_path, extra_instructions)``.
    :returns: Result from the queue handler.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    resolved = _resolve_story(context)
    if resolved is None:
        # The story may have been published or given up between the trigger
        # evaluation and this action. Nothing to do is a no-op, not a failure.
        return ActionResult(
            success=True,
            stdout="No story available to work on",
            data={"skipped": True},
        )
    story_id, story_path = resolved

    prompt = builder(
        context.target,
        story_id,
        story_path,
        action.params.get("extra_instructions", ""),
    )
    return _queue_prompt(action, context, prompt)


def queue_research_agent(action: ActionSpec, context: TickContext) -> ActionResult:
    """Build a research prompt for the active story and queue it.

    :param action: Action spec with queue params.
    :param context: Tick context with target and directories.
    :returns: Result from the queue handler.
    """
    return _queue_story_prompt(action, context, build_research_prompt)


def queue_writing_agent(action: ActionSpec, context: TickContext) -> ActionResult:
    """Build a writing prompt for the active story and queue it.

    :param action: Action spec with queue params.
    :param context: Tick context with target and directories.
    :returns: Result from the queue handler.
    """
    return _queue_story_prompt(action, context, build_writing_prompt)


def queue_publishing_agent(action: ActionSpec, context: TickContext) -> ActionResult:
    """Build a publishing prompt for the active story and queue it.

    :param action: Action spec with queue params.
    :param context: Tick context with target and directories.
    :returns: Result from the queue handler.
    """
    return _queue_story_prompt(action, context, build_publishing_prompt)


def queue_revision_agent(action: ActionSpec, context: TickContext) -> ActionResult:
    """Build a revision prompt for the active story and queue it.

    :param action: Action spec with queue params.
    :param context: Tick context with target and directories.
    :returns: Result from the queue handler.
    """
    return _queue_story_prompt(action, context, build_revision_prompt)


__all__ = [
    "build_compile_prompt",
    "build_publishing_prompt",
    "build_ranking_prompt",
    "build_research_prompt",
    "build_revision_prompt",
    "build_writing_prompt",
    "queue_compile_agent",
    "queue_publishing_agent",
    "queue_ranking_agent",
    "queue_research_agent",
    "queue_revision_agent",
    "queue_writing_agent",
]
