"""FIRERPA MCP toolset integration for ADK agents."""

from google.adk.tools.mcp_tool import McpToolset

from ..config import settings

# Try to import StreamableHTTPConnectionParams (ADK >= 0.5.0)
# Fall back to SseConnectionParams for older versions
try:
    from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
    _HAS_STREAMABLE_HTTP = True
except ImportError:
    _HAS_STREAMABLE_HTTP = False

try:
    from google.adk.tools.mcp_tool.mcp_session_manager import SseConnectionParams
    _HAS_SSE = True
except ImportError:
    _HAS_SSE = False


def create_firerpa_toolset(
    device_ip: str | None = None,
    timeout: int | None = None,
    tool_filter: list[str] | None = None,
) -> McpToolset:
    """
    Create an MCP toolset connected to FIRERPA device.

    FIRERPA v9.x uses streamable-http protocol on port 65000.
    FIRERPA v8.x uses SSE protocol.

    Args:
        device_ip: IP address of the FIRERPA device. Defaults to config setting.
        timeout: Connection timeout in seconds. Defaults to config setting.
        tool_filter: Optional list of tool names to expose. If None, all tools are exposed.

    Returns:
        McpToolset configured for FIRERPA MCP server.

    Example:
        >>> toolset = create_firerpa_toolset("192.168.1.100")
        >>> agent = LlmAgent(tools=[toolset])
    """
    ip = device_ip or settings.firerpa_device_ip
    connection_timeout = timeout or settings.firerpa_mcp_timeout

    url = f"http://{ip}:65000/firerpa/mcp/"

    # Select connection params based on available classes
    # FIRERPA v9.x uses streamable-http, v8.x uses SSE
    if _HAS_STREAMABLE_HTTP:
        connection_params = StreamableHTTPConnectionParams(
            url=url,
            timeout=connection_timeout,
        )
    elif _HAS_SSE:
        # Fallback to SSE for older ADK versions or FIRERPA v8.x
        connection_params = SseConnectionParams(
            url=url,
            headers={},
            timeout=connection_timeout,
        )
    else:
        raise ImportError(
            "No MCP connection params available. "
            "Please install google-adk >= 0.5.0 with MCP support."
        )

    toolset_kwargs = {
        "connection_params": connection_params,
    }

    # Add tool filter if specified
    if tool_filter:
        toolset_kwargs["tool_filter"] = tool_filter

    return McpToolset(**toolset_kwargs)


# Pre-defined tool filters for different agent roles
NAVIGATOR_TOOLS = [
    "screenshot",
    "get_screen_xml",
    "tap",
    "swipe",
    "press_key",
    "press_back",
    "press_home",
    "open_notification",
    "wait_for_idle",
]

OBSERVER_TOOLS = [
    "screenshot",
    "get_screen_xml",
    "dump_window_hierarchy",
]

ENGAGER_TOOLS = [
    "screenshot",
    "tap",
    "swipe",
    "set_text",
    "clear_text_field",
    "press_key",
]


def create_navigator_toolset(device_ip: str | None = None) -> McpToolset:
    """Create toolset with navigation-specific tools."""
    return create_firerpa_toolset(device_ip=device_ip, tool_filter=NAVIGATOR_TOOLS)


def create_observer_toolset(device_ip: str | None = None) -> McpToolset:
    """Create toolset with observation-specific tools."""
    return create_firerpa_toolset(device_ip=device_ip, tool_filter=OBSERVER_TOOLS)


def create_engager_toolset(device_ip: str | None = None) -> McpToolset:
    """Create toolset with engagement-specific tools."""
    return create_firerpa_toolset(device_ip=device_ip, tool_filter=ENGAGER_TOOLS)
