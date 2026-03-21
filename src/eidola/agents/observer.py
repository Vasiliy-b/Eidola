"""Observer agent for screen analysis and content understanding.

DEPRECATED: This agent is part of the old multi-agent architecture.
Use create_instagram_agent() from instagram_agent.py instead.

The unified agent combines Navigator, Observer, and Engager into a single
agent with mode-based behavior, reducing LLM calls from 7-12 to 1-2 per post.
"""
import warnings
warnings.warn(
    "Observer agent is deprecated. Use create_instagram_agent() instead.",
    DeprecationWarning,
    stacklevel=2,
)

import logging
from typing import Any

from google.adk.agents import LlmAgent
from google.genai import types

from ..config import settings
from ..tools.firerpa_tools import create_observer_tools
from ..tools.memory_tools import observer_memory_tools
from ..utils.prompt_loader import load_prompt

logger = logging.getLogger("eidola")


def create_observer_agent(
    device_ip: str | None = None,
    model: str | None = None,
) -> LlmAgent:
    """
    Create the Observer agent for screen analysis (DEPRECATED).

    NOTE: This is legacy code. Use create_instagram_agent() instead.

    Args:
        device_ip: FIRERPA device IP. Defaults to config setting.
        model: LLM model to use. Defaults to config setting.

    Returns:
        Configured LlmAgent for observation tasks.
    """
    ip = device_ip or settings.firerpa_device_ip
    llm_model = model or settings.observer_model or settings.default_model

    instruction = load_prompt("observer.md")

    logger.info(
        "Observer model=%s temperature=%s",
        llm_model,
        settings.observer_temperature,
    )

    generate_config = types.GenerateContentConfig(
        temperature=settings.observer_temperature,
        top_p=0.95,
        max_output_tokens=65535,
        thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
    )

    # Combine FIRERPA tools (screen reading) + Memory tools (nurtured accounts)
    all_tools = create_observer_tools(ip) + observer_memory_tools
    
    return LlmAgent(
        name="Observer",
        model=llm_model,
        instruction=instruction,
        description=(
            "Screen analysis worker for Instagram. "
            "Uses vision + XML to understand what's on screen, "
            "detect ads, classify content type, and extract engagement context. "
            "Can check if accounts are nurtured for priority engagement."
        ),
        tools=all_tools,
        generate_content_config=generate_config,
        # Output analysis result to state
        output_key="observation_result",
        # CRITICAL: Explicitly set sub_agents=[] to prevent ADK from auto-adding transfer_to_agent
        sub_agents=[],
    )
