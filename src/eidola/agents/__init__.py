"""Agent definitions for Eidola."""

# Legacy multi-agent architecture (deprecated, kept for compatibility)
from .orchestrator import create_orchestrator
from .navigator import create_navigator_agent
from .observer import create_observer_agent
from .engager import create_engager_agent

# New unified agent architecture
from .instagram_agent import create_instagram_agent

# Generic system agent for device tasks
from .system_agent import create_system_agent, SystemAgentRunner

__all__ = [
    # New unified agent (recommended)
    "create_instagram_agent",
    
    # System agent for device tasks
    "create_system_agent",
    "SystemAgentRunner",
    
    # Legacy agents (deprecated)
    "create_orchestrator",
    "create_navigator_agent",
    "create_observer_agent",
    "create_engager_agent",
]
