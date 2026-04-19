"""Jinja2 prompt renderer — single source of truth for all prompt assembly."""

from pathlib import Path
from jinja2 import Environment, FileSystemLoader

from .filters import budget_truncate, elapsed_str, json_compact

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
_env.filters["budget"] = budget_truncate
_env.filters["elapsed"] = elapsed_str
_env.filters["json_compact"] = json_compact


def render_showrunner_system(*, phase, mission_text, mission_version,
                              timestamp, elapsed_secs, agents, sr_info,
                              workspace_tree, container_env, tools,
                              notes, state_json, knowledge_base,
                              working_memory, long_term_memory,
                              active_tasks, task_history,
                              conversation_summary, recent_history,
                              user_responses, budgets, **kw):
    """Render the complete Showrunner system prompt."""
    tpl = _env.get_template("showrunner.j2")
    return tpl.render(
        phase=phase,
        mission_text=mission_text,
        mission_version=mission_version,
        timestamp=timestamp,
        elapsed_secs=elapsed_secs,
        agents=agents,
        sr_info=sr_info,
        workspace_tree=workspace_tree,
        container_env=container_env,
        tools=tools,
        notes=notes,
        state_json=state_json,
        knowledge_base=knowledge_base,
        working_memory=working_memory,
        long_term_memory=long_term_memory,
        active_tasks=active_tasks,
        task_history=task_history,
        conversation_summary=conversation_summary,
        recent_history=recent_history,
        user_responses=user_responses,
        budgets=budgets,
        **kw,
    )


def render_agent_system(*, agent, mission_text, tier, scratchpad,
                         knowledge_base, plan_context, tools, **kw):
    """Render system prompt for tier-2+ flock agents."""
    tpl = _env.get_template("agent.j2")
    return tpl.render(
        agent=agent,
        mission_text=mission_text,
        tier=tier,
        scratchpad=scratchpad,
        knowledge_base=knowledge_base,
        plan_context=plan_context,
        tools=tools,
        **kw,
    )


def render_agent_lite(*, agent, mission_text, scratchpad, **kw):
    """Render minimal system prompt for tier-1 flock agents."""
    tpl = _env.get_template("agent_lite.j2")
    return tpl.render(
        agent=agent,
        mission_text=mission_text,
        scratchpad=scratchpad,
        **kw,
    )


def render_naming_prompt(*, mission_text, endpoints, existing_names):
    """Render the flock agent naming/role assignment prompt."""
    tpl = _env.get_template("naming.j2")
    return tpl.render(
        mission_text=mission_text,
        endpoints=endpoints,
        existing_names=existing_names,
    )
