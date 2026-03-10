"""Engager agent for content interaction decisions and execution.

DEPRECATED: This agent is part of the old multi-agent architecture.
Use create_instagram_agent() from instagram_agent.py instead.

The unified agent combines Navigator, Observer, and Engager into a single
agent with mode-based behavior, reducing LLM calls from 7-12 to 1-2 per post.
"""
import warnings
warnings.warn(
    "Engager agent is deprecated. Use create_instagram_agent() instead.",
    DeprecationWarning,
    stacklevel=2,
)

import logging
from pathlib import Path
from typing import Any

from google.adk.agents import LlmAgent
from google.genai import types

from ..config import settings
from ..tools.firerpa_tools import create_engager_tools
from ..tools.memory_tools import engager_memory_tools
from ..utils.prompt_loader import load_prompt

logger = logging.getLogger("eidola")


def create_engager_agent(
    device_ip: str | None = None,
    persona_path: str | Path | None = None,
    model: str | None = None,
) -> LlmAgent:
    """
    Create the Engager agent for content interaction (DEPRECATED).

    NOTE: This is legacy code. Use create_instagram_agent() instead.

    Args:
        device_ip: FIRERPA device IP. Defaults to config setting.
        persona_path: Path to persona prompt file. Defaults to default_persona.md.
        model: LLM model to use. Defaults to config setting.

    Returns:
        Configured LlmAgent for engagement tasks.
    """
    ip = device_ip or settings.firerpa_device_ip
    llm_model = model or settings.engager_model or settings.default_model

    # Load comment style config (raw text injected into prompt)
    comment_style_config = ""
    try:
        comment_style_config = settings.comment_styles_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("Comment style config not found: %s", settings.comment_styles_path)

    # Load main instruction with config injection
    instruction = load_prompt(
        "engager.md",
        variables={"comment_style_config": comment_style_config},
    )

    # Load and append persona if specified
    if persona_path:
        try:
            persona_content = load_prompt(str(persona_path))
            instruction = f"{instruction}\n\n## Persona\n{persona_content}"
        except FileNotFoundError:
            pass  # Use instruction without persona

    logger.info(
        "Engager model=%s temperature=%s",
        llm_model,
        settings.engager_temperature,
    )

    generate_config = types.GenerateContentConfig(
        temperature=settings.engager_temperature,
        top_p=0.95,
        max_output_tokens=65535,
        thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
    )

    # Combine FIRERPA tools (device actions) + Memory tools (tracking)
    all_tools = create_engager_tools(ip) + engager_memory_tools
    
    return LlmAgent(
        name="Engager",
        model=llm_model,
        instruction=instruction,
        description=(
            "Interaction worker for Instagram. "
            "Decides and executes engagement actions (like, comment, share, follow). "
            "Generates human-like comments matching the account's persona. "
            "Tracks interactions to avoid duplicates."
        ),
        tools=all_tools,
        generate_content_config=generate_config,
        # Output engagement result to state
        output_key="engagement_result",
        # CRITICAL: Explicitly set sub_agents=[] to prevent ADK from auto-adding transfer_to_agent
        sub_agents=[],
    )
