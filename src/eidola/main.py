"""Entry point for Eidola - Instagram automation agent."""

# =============================================================================
# PATH SETUP (must be FIRST, before any project imports)
# =============================================================================
import sys
from pathlib import Path

# Add src/ to Python path so we can run from anywhere
_src_path = Path(__file__).parent.parent  # src/eidola/main.py → src/
if str(_src_path) not in sys.path:
    sys.path.insert(0, str(_src_path))

import asyncio
import logging
import os
import warnings
from datetime import datetime
from typing import AsyncIterator, Any

# =============================================================================
# LOAD .env FILE (must be FIRST, before any google imports)
# =============================================================================
from dotenv import load_dotenv

# Load .env from project root (parent of src/)
_env_path = _src_path.parent / ".env"
load_dotenv(_env_path)

# =============================================================================
# VERTEX AI CONFIGURATION (must be set BEFORE importing google.adk/genai)
# =============================================================================
# Use Vertex AI instead of Gemini API (requires GCP service account auth)
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")

# Suppress experimental feature warnings from ADK
warnings.filterwarnings("ignore", message=".*EXPERIMENTAL.*")

from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.models import Gemini
from google.genai import types

from .agents import create_instagram_agent, create_orchestrator  # New unified + legacy
from .config import settings
from .memory.sync_memory import SyncAgentMemory
from .memory.windowed_session import WindowedSessionService  # Token overflow prevention
from .tools.memory_tools import set_memory
from .tools.firerpa_tools import set_warmup_mode


# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================
# Console: INFO level, human-readable format with emoji prefixes
# File: DEBUG level, full details including agent thoughts
#
# Verbose loggers (pymongo, lamda.client, etc.) are suppressed on console
# but still write to file for debugging.

def setup_logging(account_id: str = "default", log_dir: str = "logs") -> Path:
    """Setup dual logging: console (human-readable) + file (full debug).
    
    Console output features:
    - Emoji prefixes for different action types
    - Clean, short timestamp (HH:MM:SS)
    - Suppressed verbose loggers (pymongo, lamda, etc.)
    - Human-readable action summaries
    
    File output features:
    - Full DEBUG level logging
    - Complete timestamps
    - All logger output including suppressed ones
    
    Returns the path to the log file.
    """
    from .utils.agent_logging import (
        HumanReadableFormatter,
        SUPPRESSED_LOGGERS,
        SuppressFilter,
    )
    
    # Create logs directory
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    
    # Log filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_path / f"{account_id}_{timestamp}.log"
    
    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    
    # Enable ADK debug logging for compaction visibility
    logging.getLogger("google_adk").setLevel(logging.WARNING)
    
    # Clear existing handlers
    root_logger.handlers.clear()
    
    # =========================================================================
    # Console handler - Human-readable format with emoji prefixes
    # =========================================================================
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(HumanReadableFormatter())
    
    # Add filter to suppress verbose loggers on console
    console_handler.addFilter(SuppressFilter(SUPPRESSED_LOGGERS))
    
    root_logger.addHandler(console_handler)
    
    # =========================================================================
    # File handler - Full DEBUG format (no suppression)
    # =========================================================================
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(file_format)
    
    # Suppress noisy loggers in file output too (saves ~40% file size)
    FILE_SUPPRESSED_LOGGERS = [
        "pymongo",
        "urllib3",
        "google_adk",
        "httpcore",
        "httpx",
        "google.auth",
        "google.api_core",
    ]
    file_handler.addFilter(SuppressFilter(FILE_SUPPRESSED_LOGGERS))
    root_logger.addHandler(file_handler)
    
    # =========================================================================
    # Activity logger for high-level agent actions
    # =========================================================================
    # Setup the activity logger's console output
    activity_console = logging.getLogger("eidola.activity.console")
    activity_console.setLevel(logging.INFO)
    activity_console.propagate = False  # Don't double-log
    
    activity_console_handler = logging.StreamHandler()
    activity_console_handler.setLevel(logging.INFO)
    activity_console_handler.setFormatter(HumanReadableFormatter())
    activity_console.addHandler(activity_console_handler)
    
    # Activity file logger uses standard format
    activity_file = logging.getLogger("eidola.activity")
    activity_file.setLevel(logging.DEBUG)
    # Will propagate to root and write to file
    
    return log_file

logger = logging.getLogger("eidola")


def format_event_for_log(event: Any) -> tuple[str, dict]:
    """Format an ADK event for logging.
    
    Extracts structured information from events:
    - Agent text/thoughts
    - Function calls and their arguments
    - Function responses
    
    Args:
        event: ADK event object
        
    Returns:
        Tuple of (formatted_string, extracted_info_dict)
        - formatted_string: Clean string for logging
        - extracted_info: Dict with keys like 'text', 'function_call', 'function_response'
    """
    extracted = {
        "text": None,
        "function_call": None,
        "function_response": None,
        "is_thought": False,
    }
    
    if not hasattr(event, "content") or event.content is None:
        return str(event)[:500], extracted
    
    content = event.content
    
    # If content has parts, extract useful information
    if hasattr(content, "parts") and content.parts:
        parts_info = []
        for part in content.parts:
            if hasattr(part, "text") and part.text:
                text = part.text.strip()
                extracted["text"] = text
                
                # Check if this looks like agent reasoning
                if any(word in text.lower() for word in ["i will", "i'll", "let me", "checking", "analyzing", "decided", "because"]):
                    extracted["is_thought"] = True
                
                # Truncate for log string
                text_preview = text[:300] + "..." if len(text) > 300 else text
                parts_info.append(f"text: {text_preview}")
                
            elif hasattr(part, "function_call") and part.function_call:
                fc = part.function_call
                extracted["function_call"] = {
                    "name": fc.name,
                    "args": dict(fc.args) if fc.args else {},
                }
                args_str = str(fc.args)[:100] + "..." if len(str(fc.args)) > 100 else str(fc.args)
                parts_info.append(f"call: {fc.name}({args_str})")
                
            elif hasattr(part, "function_response") and part.function_response:
                fr = part.function_response
                response_data = fr.response if fr.response else None
                
                # Strip heavy/noisy fields from response before logging
                if isinstance(response_data, dict):
                    response_data = {
                        k: v for k, v in response_data.items()
                        if k not in ("sdk_http_response", "thought_signature", "raw_response")
                    }
                
                extracted["function_response"] = {
                    "name": fr.name,
                    "response": response_data,
                }
                # Truncate to 300 chars for file log readability
                response_preview = str(response_data)[:300] if response_data else "None"
                parts_info.append(f"response: {fr.name} -> {response_preview}")
            # Skip thought_signature and other binary data
        
        if parts_info:
            return " | ".join(parts_info), extracted
    
    # Fallback: convert to string but truncate
    content_str = str(content)
    # Remove thought_signature from string representation
    if "thought_signature" in content_str:
        import re
        content_str = re.sub(r"thought_signature=b'[^']*'\.\.\.'\)", "thought_signature=<hidden>)", content_str)
        content_str = re.sub(r"thought_signature=b'[^']*'", "thought_signature=<hidden>", content_str)
    
    result = content_str[:500] + "..." if len(content_str) > 500 else content_str
    return result, extracted


# Tools suppressed from console (noisy, low-value)
_QUIET_TOOLS = frozenset({
    "wait_for_idle", "invalidate_xml_cache", "detect_screen",
    "get_screen_elements", "get_screen_xml", "element_exists",
    "check_post_liked",
})

# Tools that represent navigation actions
_NAV_TOOLS = frozenset({
    "open_instagram", "press_back", "press_home", "restart_instagram",
    "force_close_instagram", "escape_to_instagram",
})

# Tools that represent engagement actions
_ENGAGE_TOOLS = frozenset({
    "double_tap_like", "tap", "tap_element", "comment_on_post",
    "post_comment", "type_text", "swipe_carousel", "watch_media",
    "save_post",
})


def _log_activity_from_event(activity_logger, extracted: dict, author: str):
    """Log agent activity as a clean semantic action stream."""
    # Agent reasoning — single compact line
    text = extracted.get("text")
    if text and author not in ("user", "tool"):
        clean = text.strip().replace("\n", " ")
        if len(clean) > 150:
            clean = clean[:147] + "..."
        activity_logger.logger.info(f"\033[2m💭 {clean}\033[0m")
    
    # Function call — semantic one-liner
    if extracted.get("function_call") and author not in ("user", "tool"):
        fc = extracted["function_call"]
        name = fc["name"]
        args = fc.get("args", {})
        
        if name in _QUIET_TOOLS:
            pass
        elif name == "screenshot":
            activity_logger.logger.info("\033[33m📸 SCREENSHOT (visual analysis)\033[0m")
        elif name == "scroll_feed":
            mode = args.get("mode", "normal")
            activity_logger.logger.info(f"\033[36m📜 Scroll feed ({mode})\033[0m")
        elif name == "analyze_feed_posts":
            n = args.get("max_posts", 3)
            activity_logger.logger.info(f"\033[36m🔍 Analyzing {n} posts...\033[0m")
        elif name in _NAV_TOOLS:
            activity_logger.logger.info(f"\033[36m📱 {name.replace('_', ' ').title()}\033[0m")
        elif name == "comment_on_post":
            user = args.get("author_username", "?")
            activity_logger.logger.info(f"\033[35m💬 Commenting on @{user}...\033[0m")
        elif name in ("double_tap_like",):
            activity_logger.logger.info(f"\033[31m❤️ Double-tap like\033[0m")
        elif name == "type_text":
            t = args.get("text", "")[:30]
            activity_logger.logger.info(f"\033[35m⌨️ Typing: \"{t}{'...' if len(args.get('text', '')) > 30 else ''}\"\033[0m")
        elif name in ("tap", "tap_element"):
            sel = args.get("text") or args.get("resource_id") or args.get("content_desc") or ""
            activity_logger.logger.info(f"\033[36m👆 Tap: {sel[:40]}\033[0m")
        elif name == "get_next_nurtured_to_visit":
            activity_logger.logger.info(f"\033[33m⭐ Picking next nurtured account...\033[0m")
        elif name == "is_nurtured_account":
            user = args.get("username", "?")
            activity_logger.logger.info(f"\033[2m⭐ Check nurtured: @{user}\033[0m")
        else:
            args_str = ", ".join(f"{k}={v}" for k, v in args.items())[:50]
            activity_logger.logger.info(f"🔧 {name}({args_str})")
    
    # Function response — only log meaningful results
    if extracted.get("function_response"):
        fr = extracted["function_response"]
        name = fr["name"]
        response = fr.get("response", {})
        
        if name in _QUIET_TOOLS:
            pass
        elif name == "analyze_feed_posts" and isinstance(response, dict):
            action = response.get("recommended_action", "?")
            count = response.get("post_count", 0)
            target = response.get("target_post")
            target_user = target.get("username", "?") if isinstance(target, dict) else ""
            nav = response.get("_nav_hint", {})
            depth_str = f" d={nav.get('depth', '?')}" if nav else ""
            if target_user:
                activity_logger.logger.info(
                    f"\033[1m🎯 {action.upper()} @{target_user} ({count} posts){depth_str}\033[0m"
                )
            else:
                activity_logger.logger.info(
                    f"📊 {action} ({count} posts){depth_str}"
                )
        elif name == "is_nurtured_account" and isinstance(response, dict):
            is_n = response.get("is_nurtured", False)
            user = response.get("username", "?")
            prio = response.get("priority", "")
            if is_n:
                activity_logger.logger.info(f"\033[1;33m⭐ @{user} is NURTURED ({prio})\033[0m")
        elif name == "comment_on_post" and isinstance(response, dict):
            posted = response.get("posted") or response.get("success")
            comment = response.get("comment_text") or response.get("comment", "")
            icon = "\033[32m✅" if posted else "\033[31m❌"
            activity_logger.logger.info(f"{icon} Comment: \"{comment[:50]}\"\033[0m")
        elif name == "get_next_nurtured_to_visit" and isinstance(response, dict):
            user = response.get("username", "?")
            reason = response.get("reason", "")[:40]
            activity_logger.logger.info(f"\033[33m⭐ Next nurtured: @{user} ({reason})\033[0m")
        elif name in _NAV_TOOLS and isinstance(response, dict):
            nav = response.get("_nav_hint", {})
            screen = nav.get("screen", "?")
            depth = nav.get("depth", "?")
            activity_logger.logger.info(f"\033[36m📱 → {screen} (depth={depth})\033[0m")
        elif name in ("tap", "tap_element") and isinstance(response, dict):
            ok = response.get("tapped", False)
            if not ok:
                err = response.get("error", "not found")
                activity_logger.logger.info(f"\033[31m👆 Tap failed: {err}\033[0m")


async def create_app(
    device_ip: str | None = None,
    persona: str | None = None,
    mode: str = "active_engage",
    use_legacy: bool = False,
) -> tuple[App, Runner]:
    """
    Create and configure the ADK App and Runner.
    
    Context Management Strategy (Jan 2026):
    - InMemorySessionService: Simple session storage (no custom windowing)
    - before_model_callback: Index-aware trimming BEFORE LLM calls
    - ContextCacheConfig: Caches system prompts (safe optimization)
    
    Args:
        device_ip: FIRERPA device IP address.
        persona: Path to persona prompt file.
        mode: Agent mode (feed_scroll, active_engage, nurture_accounts, respond, warmup).
        use_legacy: If True, use old 4-agent orchestrator (deprecated).

    Returns:
        Tuple of (App, Runner) instances.
    """
    ip = device_ip or settings.firerpa_device_ip
    persona_path = persona or "persona/default_persona.md"

    if use_legacy:
        # Legacy mode: Use old 4-agent architecture (deprecated)
        import warnings
        warnings.warn(
            "Legacy orchestrator mode is deprecated. Use unified agent instead.",
            DeprecationWarning,
        )
        root_agent = create_orchestrator(
            device_ip=ip,
            persona_path=persona_path,
        )
        agent_name = "orchestrator (legacy)"
    else:
        # New unified agent - single agent with all tools
        root_agent = create_instagram_agent(
            device_ip=ip,
            mode=mode,
        )
        agent_name = f"unified agent (mode={mode})"
    
    logger.info(f"Using {agent_name}")

    # =========================================================================
    # Session Service — safety net for token overflow
    # =========================================================================
    # Primary compaction: ADK EventsCompactionConfig (summarizes old events via LLM)
    # Safety net: WindowedSessionService (hard limit + XML/screenshot compression)
    session_service = WindowedSessionService(
        max_turns=100,
        max_events=50,       # Aligned with context_max_contents=30
        compress_xml=True,   # Strip full XML/images, keep metadata
    )
    logger.info("Using WindowedSessionService (max_events=50, compress_xml=True)")

    # =========================================================================
    # ADK App Configuration
    # =========================================================================
    app = App(
        name="eidola",
        root_agent=root_agent,
        events_compaction_config=EventsCompactionConfig(
            compaction_interval=10,
            overlap_size=2,
            summarizer=LlmEventSummarizer(
                llm=Gemini(model="gemini-2.5-flash"),
            ),
        ),
        context_cache_config=ContextCacheConfig(
            min_tokens=settings.context_cache_min_tokens,
            ttl_seconds=settings.context_cache_ttl,
            cache_intervals=settings.context_cache_intervals,
        ),
    )
    
    logger.info(
        f"Created App '{app.name}' with EventsCompaction(interval=10, overlap=2) + ContextCache"
    )

    # Create Runner with App
    runner = Runner(
        app=app,
        session_service=session_service,
    )

    return app, runner


def parse_duration_from_task(task: str) -> int | None:
    """Extract duration in seconds from task string.
    
    Examples:
        "browse feed for 5 minutes" -> 300
        "engage for 10 min" -> 600
        "scroll for 2 minutes" -> 120
        
    Returns:
        Duration in seconds, or None if not found.
    """
    import re
    
    # Pattern: "for X minute(s)" or "for X min"
    pattern = r'for\s+(\d+)\s+(?:minute|min)'
    match = re.search(pattern, task, re.IGNORECASE)
    if match:
        minutes = int(match.group(1))
        return minutes * 60
    
    # Pattern: "X minute session"
    pattern2 = r'(\d+)\s*(?:minute|min)\s*session'
    match = re.search(pattern2, task, re.IGNORECASE)
    if match:
        minutes = int(match.group(1))
        return minutes * 60
    
    return None


DEFAULT_SESSION_DURATION = 900  # 15 minutes


async def run_session(
    runner: Runner,
    user_id: str,
    task: str,
    instagram_username: str | None = None,
    duration_seconds: int | None = None,
    device_ip: str | None = None,
    mode: str = "active_engage",
    device_id: str | None = None,  # For periodic isolation verification
) -> AsyncIterator[str]:
    """
    Run an agent session with the given task.

    Args:
        runner: The ADK Runner instance.
        user_id: Account identifier.
        task: The task to perform (e.g., "Check feed and engage").
        instagram_username: Instagram @username for visual detection.
        duration_seconds: Optional explicit duration limit in seconds.
        device_ip: Device IP for activity logging.
        mode: Agent mode for activity logging.

    Yields:
        Event messages from the agent.
    """
    import time as time_module
    from .utils.agent_logging import AgentActivityLogger, set_activity_logger, get_activity_logger
    
    # Create and set activity logger for human-readable output
    activity = AgentActivityLogger(logger)
    set_activity_logger(activity)
    
    # Parse duration from task if not explicitly provided
    if duration_seconds is None:
        duration_seconds = parse_duration_from_task(task)
    if duration_seconds is None:
        duration_seconds = DEFAULT_SESSION_DURATION
        logger.warning(f"No duration specified, using default: {duration_seconds}s (15 min)")
    
    # Track session timing
    session_start_time = time_module.monotonic()
    
    # Our Instagram username for visual comment detection
    our_username = instagram_username or user_id
    
    # Log session start with activity logger
    activity.session_start(
        account=user_id,
        mode=mode,
        device=device_ip or settings.firerpa_device_ip,
    )
    
    # Create a new session
    session = await runner.session_service.create_session(
        user_id=user_id,
        app_name="eidola",
        state={
            "current_account": user_id,
            "our_username": our_username,
            "instagram_username": our_username,
            "mode": mode,
            "session_limits": {
                "max_actions": settings.session_max_actions,
                "max_likes_per_hour": settings.session_max_likes_per_hour,
                "max_comments_per_hour": settings.session_max_comments_per_hour,
            },
            "session_start_time": session_start_time,
            "session_duration_seconds": duration_seconds,
        },
    )

    duration_info = f" (duration: {duration_seconds}s)" if duration_seconds else ""
    logger.debug(f"Created session {session.id} for user {user_id}{duration_info}")

    # Create the initial user message
    user_message = types.Content(
        role="user",
        parts=[types.Part(text=task)],
    )

    # Track last responding agent to detect when to continue
    last_author = None
    turn_count = 0
    max_turns = 100  # Safety limit
    last_time_log = session_start_time  # For periodic time logging
    
    # Track current post for activity logging
    current_post_username = None
    
    # Track last agent text for stop signal detection
    last_agent_text = ""
    
    # Track cache creation time for proactive refresh
    cache_created_at = session_start_time
    cache_ttl_seconds = settings.context_cache_ttl
    cache_refresh_margin = 300  # Refresh 5 minutes before expiry
    
    # Track last isolation verification for periodic re-checks
    last_isolation_check = session_start_time
    isolation_check_interval = 1800  # 30 minutes
    isolation_check_failures = 0
    max_isolation_failures = 2
    consecutive_api_errors = 0

    # Token budget tracking
    session_input_tokens = 0
    session_output_tokens = 0
    session_cached_tokens = 0
    max_session_input_tokens = 5_000_000  # Hard budget: 5M input tokens per session
    
    try:
        while turn_count < max_turns:
            turn_count += 1
            logger.info(f"📊 Starting invocation #{turn_count}")
            
            # Check time limit BEFORE each turn
            if duration_seconds:
                elapsed = time_module.monotonic() - session_start_time
                remaining = duration_seconds - elapsed
                
                if remaining <= 0:
                    logger.info(f"Session duration limit reached: {elapsed:.1f}s >= {duration_seconds}s")
                    yield f"Session time limit reached ({elapsed:.1f}s / {duration_seconds}s)"
                    break
                
                # Log progress every 30 seconds
                if elapsed - (last_time_log - session_start_time) >= 30:
                    logger.debug(f"Session progress: {elapsed:.1f}s elapsed, {remaining:.1f}s remaining")
                    last_time_log = time_module.monotonic()
            
            # PROACTIVE cache check: end session before TTL expires
            cache_age = time_module.monotonic() - cache_created_at
            if cache_age >= (cache_ttl_seconds - cache_refresh_margin):
                cache_remaining = cache_ttl_seconds - cache_age
                logger.warning(f"⚠️ Cache TTL approaching ({cache_remaining:.0f}s left) - ending session proactively")
                yield f"Session ended proactively (cache TTL: {cache_age/3600:.1f}h used of {cache_ttl_seconds/3600:.1f}h)"
                break
            
            # PERIODIC isolation verification for long sessions
            time_since_isolation_check = time_module.monotonic() - last_isolation_check
            if device_id and device_ip and time_since_isolation_check >= isolation_check_interval:
                logger.info("🔒 Performing periodic isolation verification...")
                try:
                    from .device import ProfileManager
                    manager = ProfileManager(device_ip)
                    quick_check = manager.quick_verify()
                    
                    proxy_ok = quick_check.get("proxy_ip", {}).get("success", False)
                    location_ok = quick_check.get("location_active", {}).get("mock_active", False)
                    
                    if proxy_ok and location_ok:
                        logger.info("✅ Periodic isolation verification passed")
                        isolation_check_failures = 0  # Reset on success
                    else:
                        logger.error("❌ ISOLATION FAILED MID-SESSION!")
                        logger.error(f"   Proxy: {'OK' if proxy_ok else 'FAILED'}")
                        logger.error(f"   Location: {'OK' if location_ok else 'FAILED'}")
                        yield "SESSION ABORTED: Device isolation verification failed mid-session"
                        break
                    
                    last_isolation_check = time_module.monotonic()
                except Exception as e:
                    isolation_check_failures += 1
                    logger.warning(f"⚠️ Isolation check error ({isolation_check_failures}/{max_isolation_failures}): {e}")
                    if isolation_check_failures >= max_isolation_failures:
                        logger.error("❌ Too many consecutive isolation check failures - aborting session")
                        yield "SESSION ABORTED: Cannot verify device isolation"
                        break
            
            # Device health check before invoking agent
            try:
                from .tools.firerpa_tools import get_device_manager
                dm = get_device_manager()
                if dm and dm.device:
                    dm.device.device_info()  # Quick heartbeat ~200ms
            except Exception as e:
                logger.error(f"Device disconnected: {e}")
                break
            
            # Run the agent for this turn
            try:
                async for event in runner.run_async(
                    user_id=user_id,
                    session_id=session.id,
                    new_message=user_message,
                ):
                    # Log the event (clean format, no binary data)
                    author = getattr(event, "author", "unknown")
                    formatted, extracted = format_event_for_log(event)
                    
                    # Log to file with full details
                    logger.debug(f"[{author}] {formatted}")
                    
                    # Log to activity logger for human-readable output
                    _log_activity_from_event(activity, extracted, author)
                    
                    # Track post context from function calls
                    if extracted.get("function_call"):
                        fc = extracted["function_call"]
                        # Detect when analyzing a new post
                        if fc["name"] in ("is_nurtured_account", "get_caption_info", "detect_carousel"):
                            username = fc["args"].get("username") or fc["args"].get("target_username")
                            if username and username != current_post_username:
                                # New post being analyzed
                                if current_post_username:
                                    activity.post_end()  # End previous post block
                                current_post_username = username
                                activity.post_start(username)
                    
                    yield f"[{author}] {formatted}"
                    
                    # Track who responded last and capture agent text
                    if hasattr(event, "content") and event.content:
                        if hasattr(event.content, "parts") and event.content.parts:
                            for part in event.content.parts:
                                if hasattr(part, "text") and part.text:
                                    last_author = author
                                    if author not in ("user", "tool"):
                                        last_agent_text = part.text
                    
                    # Track token usage from event metadata
                    usage = getattr(event, "usage_metadata", None)
                    if usage:
                        session_input_tokens += getattr(usage, "prompt_token_count", 0) or 0
                        session_output_tokens += getattr(usage, "candidates_token_count", 0) or 0
                        session_cached_tokens += getattr(usage, "cached_content_token_count", 0) or 0
                    
                    # Check time limit during event streaming (for long tool calls)
                    if duration_seconds:
                        elapsed = time_module.monotonic() - session_start_time
                        if elapsed >= duration_seconds:
                            logger.info(f"Session duration limit reached during event: {elapsed:.1f}s")
                            yield f"Session time limit reached ({elapsed:.1f}s / {duration_seconds}s)"
                            return  # Exit immediately
                
                # Token budget check after each turn
                if session_input_tokens > max_session_input_tokens:
                    logger.warning(
                        f"🚨 Token budget exceeded: {session_input_tokens:,} / {max_session_input_tokens:,} input tokens"
                    )
                    yield f"Token budget exceeded ({session_input_tokens:,} input tokens) — ending session"
                    break
                
                # Periodic token logging (every 3 turns)
                if turn_count % 3 == 0 and session_input_tokens > 0:
                    logger.info(
                        f"📊 Token usage (turn {turn_count}): "
                        f"input={session_input_tokens:,}, output={session_output_tokens:,}, "
                        f"cached={session_cached_tokens:,}"
                    )
            
            except Exception as e:
                error_str = str(e)
                # Handle Cache Expired gracefully (fallback - proactive check should prevent this)
                if "Cache content" in error_str and "expired" in error_str:
                    logger.error(f"❌ Cache expired (fallback triggered) after {time_module.monotonic() - session_start_time:.0f}s")
                    logger.error("This shouldn't happen - proactive check should end session before TTL")
                    yield f"Cache expired (fallback) - session ended ({turn_count} turns)"
                    return  # End session gracefully, scheduler will start new one
                elif "RESOURCE_EXHAUSTED" in error_str or "429" in error_str:
                    # API quota/rate limit — wait and retry
                    consecutive_api_errors += 1
                    retry_wait = min(3 * (2 ** min(consecutive_api_errors - 1, 3)), 15)  # 3s, 6s, 12s, 15s max
                    logger.warning(f"⚠️ API rate limit (429) — waiting {retry_wait}s before retry (attempt {consecutive_api_errors})")
                    if consecutive_api_errors >= 5:
                        logger.error("❌ Too many consecutive API rate limit errors — ending session")
                        yield f"API rate limit — session ended after {consecutive_api_errors} retries"
                        return
                    await asyncio.sleep(retry_wait)
                    continue  # Retry the turn
                elif "Tool '" in error_str and "not found" in error_str:
                    logger.warning(f"⚠️ Tool not found: {error_str[:100]} — sending error to agent")
                    user_message = types.Content(
                        role="user",
                        parts=[types.Part(text=f"ERROR: {error_str}. Use a different tool or approach. Available tools are listed in your instructions.")],
                    )
                    continue
                elif "ProtocolError" in error_str or "not JSON serializable" in error_str:
                    logger.warning(f"⚠️ Device protocol error — retrying: {error_str[:100]}")
                    consecutive_api_errors += 1
                    if consecutive_api_errors >= 3:
                        logger.error("Too many protocol errors — ending session")
                        yield f"Protocol error — session ended"
                        return
                    await asyncio.sleep(2)
                    continue
                else:
                    raise
            
            # Reset API error counter on successful turn
            consecutive_api_errors = 0
            
            # Check if agent finished or needs continuation
            # Unified agent: "InstagramAgent" 
            # Legacy agents: "Eidola_Orchestrator", "Navigator", "Observer", "Engager"
            
            # ADK EventsCompaction summarizes old context automatically.
            # WindowedSessionService is a safety net (max_events=50).
            
            if last_author in ["InstagramAgent", "Eidola_Orchestrator"]:
                # Main agent returned text response
                # Check if agent signaled it wants to stop
                stop_signals = ["cannot continue", "unable to proceed", "login required",
                                "app crashed", "session complete", "stopping session",
                                "critical error", "device disconnected"]
                if any(signal in last_agent_text.lower() for signal in stop_signals):
                    logger.info(f"Agent signaled stop: {last_agent_text[:100]}")
                    break
                
                # Continue until time limit OR max_turns
                elapsed = time_module.monotonic() - session_start_time
                
                if duration_seconds and elapsed >= duration_seconds:
                    logger.info(f"⏰ Time limit reached ({elapsed:.0f}s) - ending session")
                    break
                
                remaining = (duration_seconds - elapsed) if duration_seconds else "unlimited"
                logger.info(f"📍 Invocation #{turn_count} complete - {remaining}s remaining, continuing...")
                user_message = types.Content(
                    role="user",
                    parts=[types.Part(text="continue browsing and engaging")],
                )
                continue  # Continue to next invocation!
            else:
                # Agent finished tool calls without text response
                elapsed = time_module.monotonic() - session_start_time
                
                if duration_seconds and elapsed >= duration_seconds:
                    logger.info(f"⏰ Time limit reached ({elapsed:.0f}s) - ending session")
                    break
                
                remaining = (duration_seconds - elapsed) if duration_seconds else "unlimited"
                logger.info(f"📍 Invocation #{turn_count} complete (tools only) - {remaining}s remaining")
                user_message = types.Content(
                    role="user",
                    parts=[types.Part(text="continue browsing and engaging")],
                )
                last_author = None
                continue
        
        if turn_count >= max_turns:
            logger.warning(f"Reached max turns ({max_turns}) - ending session")
            activity.session_end("max turns reached")
        else:
            activity.session_end("completed")
            
    except KeyboardInterrupt:
        activity.session_end("interrupted")
        raise
    except Exception as e:
        activity.session_end(f"error: {e}")
        raise
    finally:
        # Token summary — always print regardless of how session ended
        logger.info(
            f"📊 SESSION TOKEN SUMMARY [{user_id}]: "
            f"turns={turn_count}, "
            f"input={session_input_tokens:,}, "
            f"output={session_output_tokens:,}, "
            f"cached={session_cached_tokens:,}, "
            f"cache_rate={session_cached_tokens / max(session_input_tokens, 1) * 100:.1f}%"
        )

        # Close Instagram at end of session
        try:
            from .tools.firerpa_tools import get_device_manager
            dm = get_device_manager()
            if dm and dm.device:
                dm.device.execute_script("am force-stop com.instagram.android 2>/dev/null || true")
                logger.info("Instagram closed (end of session)")
        except Exception:
            pass

    total_elapsed = time_module.monotonic() - session_start_time
    logger.debug(f"Session completed: {total_elapsed:.1f}s elapsed, {turn_count} turns")


# =============================================================================
# FLEET MANAGEMENT FUNCTIONS
# =============================================================================

async def run_system_agent(device_ip: str, task: str, device_id: str | None = None) -> None:
    """
    Run the generic system agent for device tasks.
    
    Args:
        device_ip: FIRERPA device IP
        task: Task description in natural language
        device_id: Device ID for config lookups (e.g., "phone_01")
    """
    from .agents.system_agent import SystemAgentRunner
    
    logger.info(f"Running system agent for task: {task}")
    if device_id:
        logger.info(f"Device ID: {device_id} (use for Gmail: get_gmail_credentials('{device_id}'))")
    
    # Augment task with device context if device_id provided
    if device_id:
        augmented_task = f"""Device context: device_id="{device_id}"
For Gmail/Play Store login, use:
- get_gmail_credentials("{device_id}") to get email and password
- generate_gmail_2fa_code("{device_id}") for 2FA code

Task: {task}"""
    else:
        augmented_task = task
    
    runner = SystemAgentRunner(device_ip)
    result = await runner.run_task(augmented_task)
    
    if result.get("success"):
        logger.info(f"✅ Task completed in {result.get('turns', '?')} turns")
        for r in result.get("results", []):
            logger.info(f"  - {r}")
    else:
        logger.error(f"❌ Task failed: {result.get('error', 'Unknown error')}")


async def run_setup_isolation(device_ip: str, device_id: str | None, country: str) -> None:
    """
    Setup device isolation (proxy, fingerprint, GPS).
    
    Args:
        device_ip: FIRERPA device IP
        device_id: Optional device ID to load config from
        country: Country code for proxy/GPS
    """
    from .device import ProfileManager
    from .config import (
        load_device_config,
        DeviceConfig,
        GeoConfig,
        ProxyConfig,
        FingerprintConfig,
        LocationConfig,
        DeviceMetadata,
    )
    
    logger.info(f"Setting up device isolation for {device_ip}")
    
    # Load device config if device_id provided
    if device_id:
        config = load_device_config(device_id)
        if not config:
            logger.error(f"Device config not found: {device_id}")
            return
    else:
        # Create minimal config for the country
        config = DeviceConfig(
            device_id="manual",
            device_ip=device_ip,
            geo=GeoConfig(country=country, country_code=country.upper()),
            proxy=ProxyConfig(),
            fingerprint=FingerprintConfig(),
            location=LocationConfig(),
            metadata=DeviceMetadata(created_at="2026-01-01"),
        )
    
    # Apply isolation
    manager = ProfileManager(device_ip)
    result = manager.apply_from_device_config(config)
    
    if result.success:
        logger.info("✅ Device isolation configured successfully")
        logger.info(f"  - Proxy: {result.proxy_result}")
        logger.info(f"  - Fingerprint: {result.fingerprint_result}")
        logger.info(f"  - Location: {result.location_result}")
    else:
        logger.error(f"❌ Isolation setup failed: {result.errors}")


async def setup_and_verify_isolation(
    device_ip: str,
    device_id: str | None = None,
    required: bool = True,
) -> tuple[bool, dict | None]:
    """
    Setup and verify device isolation before Instagram session.
    
    Applies proxy, fingerprint, and GPS spoofing, then verifies
    they are working correctly.
    
    Args:
        device_ip: Device IP address
        device_id: Device ID for config lookup
        required: If True, abort on failure. If False, warn but continue.
        
    Returns:
        Tuple of (success: bool, verification_result: dict | None)
    """
    from .device import ProfileManager
    from .config import load_device_config
    import time as time_module
    
    logger.info("=" * 60)
    logger.info("🔒 DEVICE ISOLATION SETUP")
    logger.info("=" * 60)
    
    # Load device config
    if not device_id:
        if required:
            logger.error("❌ Device ID required for isolation setup")
            return False, None
        else:
            logger.warning("⚠️ No device_id provided - skipping isolation")
            return True, None  # Allow to continue without isolation
    
    config = load_device_config(device_id)
    if not config:
        error_msg = f"Device config not found: {device_id}"
        if required:
            logger.error(f"❌ {error_msg}")
            return False, None
        else:
            logger.warning(f"⚠️ {error_msg} - continuing without isolation")
            return True, None
    
    # Check if proxy is enabled in config
    if not config.proxy or not config.proxy.enabled:
        logger.warning(f"⚠️ Proxy not enabled for {device_id} - skipping isolation")
        if required:
            logger.error("❌ Isolation required but proxy not enabled in device config")
            return False, None
        return True, None
    
    logger.info(f"📋 Device config loaded: {device_id}")
    logger.info(f"   Proxy: {config.proxy.host}:{config.proxy.port}")
    logger.info(f"   Geo: {config.geo.country} ({config.geo.city})")
    if config.adb_serial:
        logger.info(f"   ADB: {config.adb_serial} (WiFi MAC changes enabled)")
    
    # Apply isolation
    logger.info("🔧 Applying device isolation...")
    manager = ProfileManager(device_ip, adb_serial=config.adb_serial)
    
    try:
        result = manager.apply_from_device_config(config, verify=True)
    except Exception as e:
        logger.error(f"❌ Isolation setup error: {e}")
        if required:
            return False, None
        return True, None
    
    if result.success:
        verification = result.verification or {}
        logger.info("✅ ISOLATION VERIFIED SUCCESSFULLY")
        logger.info(f"   🌐 Proxy IP: {verification.get('proxy_ip', 'N/A')}")
        logger.info(f"   🌍 Country: {verification.get('proxy_country', 'N/A').upper()}")
        logger.info(f"   📍 GPS Mock: {'Active' if verification.get('location_verified') else 'Inactive'}")
        logger.info(f"   🔢 Fingerprint: {'Applied' if verification.get('fingerprint_verified') else 'Not Applied'}")
        logger.info("=" * 60)
        return True, verification
    else:
        error_summary = "; ".join(result.errors) if result.errors else "Unknown error"
        logger.error(f"❌ ISOLATION VERIFICATION FAILED")
        logger.error(f"   Errors: {error_summary}")
        logger.info("=" * 60)
        
        if required:
            logger.error("Aborting session - isolation is required for safety")
            return False, None
        else:
            logger.warning("Continuing without isolation (not required)")
            return True, None


async def run_fleet_scheduler(fleet_config_path: str, default_mode: str) -> None:
    """
    Run the fleet scheduler for multi-device, multi-account management.
    
    Args:
        fleet_config_path: Path to fleet.yaml
        default_mode: Default session mode (warmup, active_engage, etc.)
    """
    from .scheduler import MultiAccountScheduler
    from .config import load_fleet_config
    from pathlib import Path
    
    logger.info(f"Starting fleet scheduler with config: {fleet_config_path}")
    
    # Load fleet config
    fleet_config = load_fleet_config()
    
    if not fleet_config.devices:
        logger.warning("No devices configured in fleet. Scanning config/devices/...")
        from .config import load_all_devices
        devices = load_all_devices()
        fleet_config.devices = devices
    
    if not fleet_config.devices:
        logger.error("No devices found in fleet config or config/devices/")
        return
    
    logger.info(f"Fleet '{fleet_config.name}' has {len(fleet_config.devices)} devices")
    
    # Create scheduler
    scheduler = MultiAccountScheduler(fleet_config=fleet_config)
    
    # Set session callback
    async def session_callback(
        device_ip: str,
        account_id: str,
        username: str,
        mode: str,
        duration_seconds: int,
        config: dict,
    ):
        """Run an actual Instagram agent session."""
        logger.info(f"Running session: device={device_ip}, account={username}, mode={mode}")
        
        # Get device_id from config (passed through scheduler)
        device_id = config.get("device_id")
        
        # Apply and verify device isolation BEFORE creating agent
        if device_id:
            isolation_success, isolation_result = await setup_and_verify_isolation(
                device_ip=device_ip,
                device_id=device_id,
                required=True,  # Always required for fleet operations
            )
            
            if not isolation_success:
                logger.error(f"❌ Skipping session for {username} - isolation failed")
                yield "Session aborted: device isolation verification failed"
                return
        else:
            logger.warning(f"⚠️ No device_id for {device_ip} - skipping isolation")
        
        # Create app and runner for this session
        app, runner = await create_app(
            device_ip=device_ip,
            mode=mode,
        )
        
        # Set warmup mode flag so analyze_feed_posts() skips non-nurtured
        set_warmup_mode(mode == "warmup")
        
        # Build task
        duration_min = duration_seconds // 60
        if mode == "warmup":
            task = f"Warm up the account by scrolling feed and liking posts for {duration_min} minutes"
        else:
            task = f"Browse feed and engage with posts for {duration_min} minutes"
        
        # Run session
        async for msg in run_session(
            runner=runner,
            user_id=account_id,
            task=task,
            instagram_username=username,
            duration_seconds=duration_seconds,
            device_ip=device_ip,
            mode=mode,
            device_id=device_id,  # For periodic isolation verification
        ):
            yield msg
    
    scheduler.set_session_callback(session_callback)
    
    # Run all devices
    logger.info("Starting all device loops...")
    try:
        await scheduler.run_all_devices(default_mode="warmup")
    except KeyboardInterrupt:
        logger.info("Fleet scheduler interrupted")
        scheduler.stop()
    
    logger.info("Fleet scheduler stopped")


def _load_nurtured_accounts(
    memory: SyncAgentMemory,
    user_id: str,
    device_id: str | None = None,
) -> None:
    """Load nurtured accounts from YAML config into MongoDB.
    
    Args:
        memory: MongoDB memory instance
        user_id: Account identifier
    """
    import yaml
    from pathlib import Path
    
    config_path = Path("config/nurtured_accounts.yaml")
    if not config_path.exists():
        logger.debug("No nurtured_accounts.yaml found - skipping")
        return
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        if not config or not isinstance(config, dict):
            logger.warning("Invalid nurtured_accounts.yaml format - expected dict")
            return
        
        accounts = config.get("accounts", [])
        if not isinstance(accounts, list):
            logger.warning("Invalid 'accounts' field - expected list")
            return
        
        if not accounts:
            logger.debug("No accounts in nurtured_accounts.yaml")
            return
        
        loaded = 0
        skipped = 0
        failed = 0
        username_to_device: dict[str, str] = {}
        if device_id:
            from .config import load_all_accounts
            for cfg in load_all_accounts():
                username_to_device[cfg.account_id.lower()] = cfg.assigned_device
                username_to_device[cfg.instagram.username.lower()] = cfg.assigned_device

        for acc in accounts:
            username = acc.get("username")
            if not username:
                continue
            
            priority = acc.get("priority", "medium")
            notes = acc.get("notes", "")

            if device_id and str(priority).lower() != "vip":
                assigned = username_to_device.get(str(username).lower().lstrip("@"))
                if assigned and assigned != device_id:
                    skipped += 1
                    logger.debug(
                        f"Skipping nurtured @{username}: assigned_device={assigned}, current_device={device_id}"
                    )
                    continue
            
            # Add to MongoDB (upsert - won't duplicate)
            success = memory.add_nurtured_account(
                user_id=user_id,
                target_account=username,
                priority=priority,
                notes=notes,
            )
            if success:
                loaded += 1
            else:
                failed += 1
                logger.warning(f"Failed to add nurtured account: {username}")
        
        if failed > 0:
            logger.warning(f"Loaded {loaded} nurtured accounts, {failed} failed, {skipped} skipped")
        else:
            logger.info(f"Loaded {loaded} nurtured accounts from config (skipped={skipped})")
        
    except Exception as e:
        logger.warning(f"Failed to load nurtured accounts: {e}")


async def main():
    """Main entry point for the agent."""
    import argparse

    parser = argparse.ArgumentParser(description="Eidola Instagram Agent")
    parser.add_argument(
        "--account",
        "-a",
        default="account_1",
        help="Account identifier",
    )
    parser.add_argument(
        "--device",
        "-d",
        default=None,
        help="FIRERPA device IP (defaults to config)",
    )
    parser.add_argument(
        "--persona",
        "-p",
        default=None,
        help="Path to persona prompt file",
    )
    parser.add_argument(
        "--task",
        "-t",
        default="Check the home feed and engage with interesting posts",
        help="Task for the agent to perform",
    )
    parser.add_argument(
        "--username",
        "-u",
        default=None,
        help="Instagram @username (for detecting own comments). If not set, uses account ID.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging (verbose XML, full tool responses)",
    )
    parser.add_argument(
        "--save-xml",
        action="store_true",
        help="Save XML dumps to ./debug_xml/ folder for analysis",
    )
    parser.add_argument(
        "--log-dir",
        default="logs",
        help="Directory for log files (default: logs/)",
    )
    parser.add_argument(
        "--raw-xml",
        action="store_true",
        help="Disable XML compression (return raw XML for debugging)",
    )
    parser.add_argument(
        "--mode",
        "-m",
        default="active_engage",
        choices=[
            "feed_scroll", "active_engage", "nurture_accounts", "respond", "warmup",
            "login",  # Login with 2FA
            "system",  # Generic system agent for device tasks
            "setup-isolation",  # Setup device isolation (proxy, fingerprint, GPS)
            "fleet",  # Run fleet scheduler
        ],
        help="Agent mode (default: active_engage). Use 'warmup' for accounts with comment restrictions.",
    )
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="Use legacy 4-agent orchestrator (deprecated)",
    )
    # Fleet management options
    parser.add_argument(
        "--fleet-config",
        default="config/fleet.yaml",
        help="Path to fleet configuration (default: config/fleet.yaml)",
    )
    parser.add_argument(
        "--device-id",
        help="Device ID for device-specific operations (from config/devices/)",
    )
    parser.add_argument(
        "--country",
        default="us",
        help="Country code for setup-isolation (default: us)",
    )
    parser.add_argument(
        "--no-isolation",
        action="store_true",
        help="Skip device isolation (proxy, fingerprint, GPS) - for testing only",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="Session duration in seconds. Overrides task-based duration parsing. "
             "Used by fleet_scheduler to set exact session lengths.",
    )

    args = parser.parse_args()
    
    # Setup logging - file gets full details, console gets clean output
    log_file = setup_logging(account_id=args.account, log_dir=args.log_dir)
    logger.info(f"Logging to file: {log_file}")
    
    # Configure debug logging if requested
    if args.debug:
        logging.getLogger("eidola").setLevel(logging.DEBUG)
        logging.getLogger("eidola.tools").setLevel(logging.DEBUG)
        logging.getLogger("eidola.memory").setLevel(logging.DEBUG)
        logger.info("Debug logging enabled")
    
    # Configure XML dump saving and raw XML mode
    if args.save_xml or args.debug or args.raw_xml:
        from .tools.firerpa_tools import set_debug_config
        set_debug_config(
            save_xml=args.save_xml or args.debug,
            verbose=args.debug,
            xml_dir="./debug_xml",
            raw_xml=args.raw_xml,
        )
    
    # Instagram username for visual comment detection
    instagram_username = args.username or args.account

    # Resolve device_ip from device_id if provided
    device_id = args.device_id
    if device_id:
        from .config import load_device_config
        device_config = load_device_config(device_id)
        if device_config:
            device_ip = device_config.device_ip
            logger.info(f"Loaded device config for {device_id}: IP={device_ip}")
        else:
            logger.warning(f"Device config not found for {device_id}, using --device or default")
            device_ip = args.device or settings.firerpa_device_ip
    else:
        device_ip = args.device or settings.firerpa_device_ip
    
    logger.info(f"Starting Eidola for account: {args.account}")
    logger.info(f"Device IP: {device_ip}")
    logger.info(f"Device ID: {device_id or 'not specified'}")
    logger.info(f"Mode: {args.mode}")

    # =========================================================================
    # PRE-FLIGHT: Verify device connection BEFORE starting agent
    # =========================================================================
    logger.info("Checking device connection...")
    
    try:
        from lamda.client import Device
        
        # Try to connect with a timeout
        test_device = Device(device_ip)
        device_info = test_device.device_info()
        
        logger.info(f"Device connected: {getattr(device_info, 'productName', 'unknown')}")
        logger.info(f"Screen: {getattr(device_info, 'displayWidth', '?')}x{getattr(device_info, 'displayHeight', '?')}")
        logger.info(f"SDK: {getattr(device_info, 'sdkInt', '?')}")
        
    except ImportError:
        logger.warning("lamda package not installed - skipping device check")
    except Exception as e:
        logger.error(f"Device connection failed: {e}")
        logger.error("Please check:")
        logger.error(f"  1. Device IP is correct: {device_ip}")
        logger.error("  2. FIRERPA server is running on the device")
        logger.error("  3. Device and this machine are on the same network")
        logger.error("  4. No firewall blocking port 65000")
        raise SystemExit(1)
    
    logger.info("Device connection OK - starting agent...")
    
    # =========================================================================
    # SPECIAL MODES: Handle non-Instagram agent modes
    # =========================================================================
    
    if args.mode == "system":
        # Run system agent for generic device tasks
        await run_system_agent(device_ip, args.task, device_id)
        return
    
    if args.mode == "setup-isolation":
        # Setup device isolation (proxy, fingerprint, GPS)
        await run_setup_isolation(device_ip, args.device_id, args.country)
        return
    
    if args.mode == "fleet":
        # Run fleet scheduler
        await run_fleet_scheduler(args.fleet_config, args.mode)
        return
    
    # =========================================================================
    # DEVICE ISOLATION: Setup and verify proxy, fingerprint, GPS
    # =========================================================================
    if not args.no_isolation:
        # Apply device isolation for all Instagram agent modes
        isolation_required = True  # Always required for safety
        isolation_success, isolation_result = await setup_and_verify_isolation(
            device_ip=device_ip,
            device_id=device_id,
            required=isolation_required,
        )
        
        if not isolation_success:
            logger.error("❌ Cannot start session without device isolation")
            logger.error("   Use --no-isolation flag for testing without proxy (NOT RECOMMENDED)")
            raise SystemExit(1)
    else:
        logger.warning("=" * 60)
        logger.warning("⚠️  ISOLATION DISABLED - RUNNING WITHOUT PROXY/GPS")
        logger.warning("   This is for TESTING ONLY. Your real IP is exposed!")
        logger.warning("=" * 60)
    
    # =========================================================================
    # INITIALIZE MEMORY (MongoDB for long-term tracking)
    # =========================================================================
    memory = None
    try:
        import uuid as _uuid
        _session_id = str(_uuid.uuid4())
        memory = SyncAgentMemory()
        if memory.is_connected():
            logger.info("MongoDB memory connected")
            set_memory(
                memory, args.account, instagram_username,
                device_id=device_id, session_id=_session_id,
            )
            
            logger.info("Comment limits: daily (9-15/24h via MongoDB), per-session (3-5 via MongoDB)")
            
            # Load nurtured accounts from YAML config
            _load_nurtured_accounts(memory, args.account, device_id=device_id)
        else:
            logger.warning("MongoDB not reachable - memory features disabled")
            memory = None
    except Exception as e:
        logger.warning(f"MongoDB init failed: {e} - memory features disabled")
        memory = None
    
    # Set warmup mode flag so analyze_feed_posts() skips non-nurtured
    set_warmup_mode(args.mode == "warmup")
    
    app = None
    runner = None
    try:
        # Create App with ContextCacheConfig and Runner
        app, runner = await create_app(
            device_ip=device_ip,
            persona=args.persona,
            mode=args.mode,
            use_legacy=args.legacy,
        )

        # Adjust task based on mode
        task = args.task
        if args.mode == "login":
            # Login mode: use Instagram username (may differ from account_id)
            task = (
                f"Ensure Instagram account '{instagram_username}' is active on this device. "
                f"FIRST call detect_screen() to check the current state. "
                f"If you see ANY Instagram content (feed, profile, reels, stories, explore) — "
                f"the account is ALREADY logged in. Report success and proceed to browse the feed. "
                f"Only if you see a login screen or the app is not open, "
                f"use get_account_credentials('{args.account}') to log in."
            )
        
        logger.info(f"🎯 Task: {task[:100]}{'...' if len(task) > 100 else ''}")

        # Run the session (logging is handled inside run_session)
        async for _ in run_session(
            runner=runner,
            user_id=args.account,
            task=task,
            instagram_username=instagram_username,
            duration_seconds=args.duration,  # From --duration flag (fleet_scheduler sets this)
            device_ip=device_ip,
            mode=args.mode,
            device_id=device_id,  # For periodic isolation verification
        ):
            pass  # Events are logged and yielded by run_session

    except KeyboardInterrupt:
        logger.info("Session interrupted by user")
    except Exception as e:
        logger.error(f"Error running session: {e}", exc_info=True)
        raise
    finally:
        # Close Instagram at end of session (humans don't leave it open)
        try:
            from .tools.firerpa_tools import get_device_manager
            dm = get_device_manager()
            if dm and dm.device:
                dm.device.execute_script("am force-stop com.instagram.android 2>/dev/null || true")
                logger.info("Instagram closed (session ended)")
        except Exception as e:
            logger.debug(f"Could not close Instagram: {e}")
        
        # Cleanup resources
        if memory:
            memory.close()
            logger.debug("Memory connection closed")
        if runner and hasattr(runner, "session_service"):
            if hasattr(runner.session_service, "close"):
                await runner.session_service.close()
        logger.info("Session cleanup complete")


if __name__ == "__main__":
    asyncio.run(main())
