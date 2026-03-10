"""Navigator agent for UI navigation in Instagram.

DEPRECATED: This agent is part of the old multi-agent architecture.
Use create_instagram_agent() from instagram_agent.py instead.

The unified agent combines Navigator, Observer, and Engager into a single
agent with mode-based behavior, reducing LLM calls from 7-12 to 1-2 per post.
"""
import warnings
warnings.warn(
    "Navigator agent is deprecated. Use create_instagram_agent() instead.",
    DeprecationWarning,
    stacklevel=2,
)

import logging
from typing import Any

from google.adk.agents import LlmAgent
from google.genai import types

from ..config import settings
from ..tools.firerpa_tools import create_navigator_tools
from ..utils.prompt_loader import load_prompt

logger = logging.getLogger("eidola")


def create_navigator_agent(
    device_ip: str | None = None,
    model: str | None = None,
) -> LlmAgent:
    """
    Create the Navigator agent for UI navigation (DEPRECATED).

    NOTE: This is legacy code. Use create_instagram_agent() instead.

    Args:
        device_ip: FIRERPA device IP. Defaults to config setting.
        model: LLM model to use. Defaults to config setting.

    Returns:
        Configured LlmAgent for navigation tasks.
    """
    ip = device_ip or settings.firerpa_device_ip
    llm_model = model or settings.navigator_model or settings.fast_model

    instruction = load_prompt("navigator.md")

    logger.info(
        "Navigator model=%s temperature=%s",
        llm_model,
        settings.navigator_temperature,
    )

    generate_config = types.GenerateContentConfig(
        temperature=settings.navigator_temperature,
        top_p=0.95,
        max_output_tokens=65535,
        thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
    )

    return LlmAgent(
        name="Navigator",
        model=llm_model,
        instruction=instruction,
        description=(
            "UI navigation worker for Instagram. "
            "Determines current screen location and navigates to target destinations. "
            "Can perform taps, swipes, and recover from navigation errors."
        ),
        tools=create_navigator_tools(ip),
        generate_content_config=generate_config,
        # Output navigation result to state
        output_key="navigation_result",
        # CRITICAL: Explicitly set sub_agents=[] to prevent ADK from auto-adding transfer_to_agent
        sub_agents=[],
    )
