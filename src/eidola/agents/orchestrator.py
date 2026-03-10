"""Orchestrator agent - main coordinator for the multi-agent system.

DEPRECATED: This agent is part of the old multi-agent architecture.
Use create_instagram_agent() from instagram_agent.py instead.

The unified agent combines Navigator, Observer, and Engager into a single
agent with mode-based behavior, reducing LLM calls from 7-12 to 1-2 per post.

For scheduling, use the new Scheduler from scheduler/scheduler.py.
"""
import warnings
warnings.warn(
    "Orchestrator agent is deprecated. Use create_instagram_agent() and Scheduler instead.",
    DeprecationWarning,
    stacklevel=2,
)

import logging
from pathlib import Path
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.genai import types

from ..config import settings
from ..utils.prompt_loader import load_prompt
# NOTE: action_budget removed - sessions now use scheduler with real-time budget
# See src/eidola/scheduler/session_runner.py for new approach
from .engager import create_engager_agent
from .navigator import create_navigator_agent
from .observer import create_observer_agent

logger = logging.getLogger("eidola")


def create_orchestrator(
    device_ip: str | None = None,
    persona_path: str | Path | None = None,
    model: str | None = None,
    session_variables: dict | None = None,
) -> LlmAgent:
    """
    Create the Orchestrator agent - main coordinator (DEPRECATED).

    NOTE: This is legacy code. Use create_instagram_agent() instead.
    Context management is now handled by ADK's EventsCompactionConfig.

    Args:
        device_ip: FIRERPA device IP. Defaults to config setting.
        persona_path: Path to persona prompt file for the Engager.
        model: LLM model to use. Defaults to config setting.
        session_variables: Variables to inject into the orchestrator prompt.

    Returns:
        Configured LlmAgent with Navigator, Observer, and Engager as sub-agents.
    """
    ip = device_ip or settings.firerpa_device_ip
    persona = persona_path or "persona/default_persona.md"
    model_override = model
    orchestrator_model = (
        model_override
        or settings.orchestrator_model
        or settings.default_model
    )
    navigator_model = (
        model_override
        or settings.navigator_model
        or settings.fast_model
    )
    observer_model = (
        model_override
        or settings.observer_model
        or settings.default_model
    )
    engager_model = (
        model_override
        or settings.engager_model
        or settings.default_model
    )

    # Load instruction with optional variable substitution
    variables = session_variables or {}
    instruction = load_prompt("orchestrator.md", variables=variables)

    # Create sub-agents
    navigator = create_navigator_agent(
        device_ip=ip, 
        model=navigator_model,
    )
    observer = create_observer_agent(
        device_ip=ip, 
        model=observer_model,
    )
    engager = create_engager_agent(
        device_ip=ip, 
        persona_path=persona, 
        model=engager_model,
    )

    logger.info(
        "Orchestrator model=%s temperature=%s",
        orchestrator_model,
        settings.orchestrator_temperature,
    )

    generate_config = types.GenerateContentConfig(
        temperature=settings.orchestrator_temperature,
        top_p=0.95,
        max_output_tokens=65535,
        thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
    )

    return LlmAgent(
        name="Eidola_Orchestrator",
        model=orchestrator_model,
        instruction=instruction,
        description=(
            "Main coordinator for Instagram automation. "
            "Plans activities, delegates to Navigator/Observer/Engager, "
            "tracks limits, and handles recovery."
        ),
        sub_agents=[navigator, observer, engager],
        generate_content_config=generate_config,
        tools=[],
    )
