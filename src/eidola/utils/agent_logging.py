"""Human-readable agent logging utilities.

Provides:
- HumanReadableFormatter: Clean console output with emoji prefixes
- AgentActivityLogger: Semantic logging for agent actions
- Suppression filters for verbose loggers (pymongo, etc.)
"""

import logging
from datetime import datetime
from typing import Any

# Loggers to suppress on console (WARNING+ only)
SUPPRESSED_LOGGERS = [
    "pymongo",
    "pymongo.topology",
    "pymongo.connection",
    "pymongo.command",
    "pymongo.serverSelection",
    "lamda.client",
    "httpx",
    "httpcore",
    "google.adk",
    "google.adk.runners",
    "google.adk.agents",
    "google_adk",
    "google.genai",
]


class SuppressFilter(logging.Filter):
    """Filter that only allows WARNING+ for specified loggers."""
    
    def __init__(self, suppressed_names: list[str]):
        super().__init__()
        self.suppressed_names = suppressed_names
    
    def filter(self, record: logging.LogRecord) -> bool:
        for name in self.suppressed_names:
            if record.name.startswith(name):
                return record.levelno >= logging.WARNING
        return True


class HumanReadableFormatter(logging.Formatter):
    """Formatter with emoji prefixes for different action types."""
    
    # Emoji prefixes for different message patterns
    PATTERNS = {
        "session start": "🚀",
        "session end": "🏁",
        "session completed": "📊",
        "screen:": "👁",
        "scroll": "📜",
        "open": "📱",
        "tap": "👆",
        "like": "❤️",
        "comment": "💬",
        "save": "🔖",
        "nurtured": "⭐",
        "skip": "⏭️",
        "engage": "🎯",
        "cta": "📣",
        "error": "❌",
        "warning": "⚠️",
        "carousel": "🎠",
        "caption": "📝",
        "post:": "📰",
        "decision": "💭",
    }
    
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        
        msg = record.getMessage()
        
        # Skip repeated noise messages
        if any(noise in msg for noise in ("AFC is enabled", "RESTORED first user TEXT")):
            return ""
        
        msg_lower = msg.lower()
        prefix = ""
        indent = ""
        
        # Detect indentation from message content
        if msg.startswith("│") or msg.startswith("   "):
            indent = "    "
        
        # Skip emoji prefix if message already has ANSI codes or emoji
        has_ansi = "\033[" in msg
        has_emoji = any(msg.startswith(e) for e in [
            "🚀", "📱", "👁", "📜", "❤️", "💬", "⭐", "📰", "🔧", "💭",
            "┌", "│", "└", "━", "📸", "🔍", "🎯", "📊", "⌨️", "👆",
        ])
        
        if not has_ansi and not has_emoji:
            for pattern, emoji in self.PATTERNS.items():
                if pattern in msg_lower:
                    prefix = f"{emoji} "
                    break
        
        if record.levelno >= logging.ERROR:
            prefix = "❌ "
        elif record.levelno >= logging.WARNING and "⚠️" not in msg:
            prefix = "⚠️ "
        
        return f"{timestamp} {indent}{prefix}{msg}"


class AgentActivityLogger:
    """High-level semantic logging for agent activities."""
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.current_post = None
        self.stats = {
            "posts_seen": 0,
            "liked": 0,
            "commented": 0,
            "skipped": 0,
            "nurtured_engaged": 0,
            "cta_followed": 0,
        }
    
    def session_start(self, account: str, mode: str, device: str):
        """Log session start with banner."""
        self.logger.info("━" * 60)
        self.logger.info(f"🚀 Session started | account: {account} | mode: {mode}")
        self.logger.info(f"   Device: {device}")
        self.logger.info("━" * 60)
    
    def session_end(self, duration_or_status: float | str = 0.0):
        """Log session end with stats.
        
        Args:
            duration_or_status: Either duration in seconds (float) or status string
        """
        self.logger.info("━" * 60)
        
        if isinstance(duration_or_status, str):
            # Status string (error, interrupted, etc.)
            self.logger.info(f"📊 Session ended: {duration_or_status}")
        else:
            # Duration in seconds
            mins = int(duration_or_status // 60)
            secs = int(duration_or_status % 60)
            self.logger.info("📊 Session completed")
            self.logger.info(f"   Duration: {mins}m {secs}s")
        
        self.logger.info(
            f"   Posts seen: {self.stats['posts_seen']} | "
            f"Liked: {self.stats['liked']} | "
            f"Commented: {self.stats['commented']} | "
            f"Skipped: {self.stats['skipped']}"
        )
        self.logger.info(
            f"   Nurtured engaged: {self.stats['nurtured_engaged']} | "
            f"CTA followed: {self.stats['cta_followed']}"
        )
        self.logger.info("━" * 60)
    
    def navigation(self, action: str):
        """Log navigation action."""
        self.logger.info(f"📱 {action}")
    
    def scroll(self, direction: str = "down", mode: str = "normal"):
        """Log scroll action."""
        self.logger.info(f"📜 Scroll {direction} ({mode})")
    
    def agent_thought(self, thought: str):
        """Log agent's reasoning/thought."""
        # Truncate long thoughts
        if len(thought) > 120:
            thought = thought[:120] + "..."
        self.logger.info(f"💭 {thought}")
    
    def tool_response(self, tool_name: str, result: dict | str, success: bool = True):
        """Log tool response with key info."""
        icon = "✅" if success else "❌"
        if isinstance(result, dict):
            # Extract key fields for display
            key_fields = ["success", "found", "error", "screen_context", "is_nurtured", "tapped"]
            summary = {k: v for k, v in result.items() if k in key_fields}
            if summary:
                self.logger.info(f"    {icon} {tool_name} → {summary}")
            else:
                # Show first 80 chars of result
                result_str = str(result)[:80]
                self.logger.info(f"    {icon} {tool_name} → {result_str}...")
        else:
            self.logger.info(f"    {icon} {tool_name} → {str(result)[:80]}")
    
    def model_response(self, text: str):
        """Log model thinking/response text (compact)."""
        text = text.strip().replace("\n", " ")
        if text:
            self.logger.info(f"\033[2m💭 {text[:150]}{'...' if len(text) > 150 else ''}\033[0m")
    
    def post_nurtured(self, is_nurtured: bool, priority: str = "medium"):
        """Log nurtured status of current post."""
        if is_nurtured:
            self.logger.info(f"│  ⭐ NURTURED ({priority})")
        else:
            self.logger.info(f"│  Regular account")
    
    def post_carousel(self, page_count: int):
        """Log carousel detection."""
        self.logger.info(f"│  🎠 Carousel: {page_count} pages")
    
    def post_caption(self, caption: str, has_cta: bool = False, cta_word: str = ""):
        """Log caption info."""
        short_caption = caption[:50] + "..." if len(caption) > 50 else caption
        self.logger.info(f"│  📝 Caption: {short_caption}")
        if has_cta and cta_word:
            self.logger.info(f"│  📣 CTA detected: \"{cta_word}\"")
    
    def post_start(self, username: str, is_nurtured: bool = False, time_ago: str = ""):
        """Log start of post analysis."""
        self.current_post = username
        self.stats["posts_seen"] += 1
        
        nurtured_tag = " ⭐ NURTURED (VIP)" if is_nurtured else ""
        time_str = f" ({time_ago})" if time_ago else ""
        
        self.logger.info(f"┌─ POST: @{username}{time_str}")
        if is_nurtured:
            self.logger.info(f"│  {nurtured_tag}")
    
    def post_detail(self, detail: str):
        """Log post detail (carousel, caption, etc)."""
        self.logger.info(f"│  {detail}")
    
    def post_decision(self, decision: str, reason: str = ""):
        """Log engagement decision."""
        reason_str = f" ({reason})" if reason else ""
        self.logger.info(f"│  💭 Decision: {decision}{reason_str}")
    
    def post_end(self, success: bool = True):
        """Log end of post processing."""
        status = "✅ Done" if success else "⏭️ Skipped"
        self.logger.info(f"└─ {status}")
    
    def action_like(self, username: str):
        """Log like action."""
        self.stats["liked"] += 1
        self.logger.info(f"❤️ Liked post by @{username}")
    
    def action_comment(self, username: str, comment: str, is_cta: bool = False):
        """Log comment action."""
        self.stats["commented"] += 1
        if is_cta:
            self.stats["cta_followed"] += 1
            self.logger.info(f"💬 Commented \"{comment}\" on @{username}'s post (CTA)")
        else:
            self.logger.info(f"💬 Commented \"{comment}\" on @{username}'s post")
    
    def action_skip(self, username: str, reason: str):
        """Log skip action."""
        self.stats["skipped"] += 1
        self.logger.info(f"⏭️ Skipped @{username}: {reason}")
    
    def action_save(self, username: str):
        """Log save action."""
        self.stats["saved"] = self.stats.get("saved", 0) + 1
        self.logger.info(f"🔖 Saved post by @{username}")
    
    def action_scroll(self, direction: str = "down", mode: str = "normal"):
        """Log scroll action."""
        self.logger.info(f"📜 Scrolling {direction} ({mode})")
    
    def screen_detected(self, screen_name: str):
        """Log screen detection."""
        self.logger.info(f"👁 Screen: {screen_name}")
    
    def tool_call(self, tool_name: str, args: dict[str, Any] | None = None):
        """Log tool call (only important ones)."""
        # Only log important tools to console
        important_tools = {
            "is_nurtured_account", "tap", "type_text", 
            "is_post_liked", "is_post_saved", "screenshot"
        }
        if tool_name in important_tools:
            args_str = f" → {args}" if args else ""
            self.logger.info(f"🔧 {tool_name}{args_str}")
    
    def nurtured_engaged(self):
        """Track nurtured engagement."""
        self.stats["nurtured_engaged"] += 1


def setup_console_filter(console_handler: logging.Handler):
    """Add suppression filter to console handler."""
    console_handler.addFilter(SuppressFilter(SUPPRESSED_LOGGERS))


# =============================================================================
# CONTEXT-BASED ACTIVITY LOGGER (replaces global variable)
# =============================================================================
# Using contextvars for thread-safe, async-safe per-session logging.
# This supports multiple accounts running in parallel.

from contextvars import ContextVar

_activity_logger_ctx: ContextVar[AgentActivityLogger | None] = ContextVar(
    "activity_logger", default=None
)


def get_activity_logger() -> AgentActivityLogger | None:
    """Get the activity logger for current context."""
    return _activity_logger_ctx.get()


def set_activity_logger(logger: AgentActivityLogger):
    """Set the activity logger for current context."""
    _activity_logger_ctx.set(logger)


def reset_activity_logger():
    """Reset the activity logger for current context."""
    _activity_logger_ctx.set(None)


class Emoji:
    """Emoji constants for logging."""
    SESSION_START = "🚀"
    SESSION_END = "🏁"
    SCREEN = "👁"
    SCROLL = "📜"
    LIKE = "❤️"
    COMMENT = "💬"
    SAVE = "🔖"
    NURTURED = "⭐"
    SKIP = "⏭️"
    TAP = "👆"
    ERROR = "❌"
    WARNING = "⚠️"
    SUCCESS = "✅"
    POST = "📰"
    DECISION = "💭"


class PostCard:
    """Buffers post analysis data and flushes as a structured ASCII block.
    
    Designed for clear console output. Uses ASCII box-drawing characters
    compatible with Windows CMD (no Unicode box chars).
    
    Usage:
        card = PostCard(logger)
        card.set_author("fashionista_99", is_nurtured=True)
        card.set_screenshot(True, 85.2)
        card.set_caption("Beach vibes today #summer", has_cta=False)
        card.set_comments([{"username": "user1", "text": "nice!"}])
        card.set_our_recent(["the colors!", "beach day"])
        card.set_action("comment", "that turquoise water tho")
        card.flush()  # Prints formatted block
    """
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self._reset()
    
    def _reset(self):
        self.author: str = ""
        self.is_nurtured: bool = False
        self.time_ago: str = ""
        self.screenshot_ok: bool = False
        self.screenshot_kb: float = 0
        self.caption: str = ""
        self.caption_expanded: bool = False
        self.has_cta: bool = False
        self.cta_word: str = ""
        self.visible_comments: list[dict] = []
        self.total_comments_hint: str = ""
        self.our_recent: list[str] = []
        self.action: str = ""       # "like", "comment", "skip", "save"
        self.action_detail: str = "" # comment text, skip reason, etc.
        self.decision_reason: str = ""
    
    def set_author(self, username: str, is_nurtured: bool = False, time_ago: str = ""):
        self.author = username
        self.is_nurtured = is_nurtured
        self.time_ago = time_ago
    
    def set_screenshot(self, ok: bool, size_kb: float = 0):
        self.screenshot_ok = ok
        self.screenshot_kb = size_kb
    
    def set_caption(self, text: str, expanded: bool = False, has_cta: bool = False, cta_word: str = ""):
        self.caption = text
        self.caption_expanded = expanded
        self.has_cta = has_cta
        self.cta_word = cta_word
    
    def set_comments(self, comments: list[dict], total_hint: str = ""):
        self.visible_comments = comments
        self.total_comments_hint = total_hint
    
    def set_our_recent(self, comments: list[str]):
        self.our_recent = comments
    
    def set_action(self, action: str, detail: str = "", reason: str = ""):
        self.action = action
        self.action_detail = detail
        self.decision_reason = reason
    
    def flush(self):
        """Output the buffered post card to the logger and reset."""
        w = 58  # card inner width
        
        def line(text: str = ""):
            """Left-aligned line inside the card."""
            return f"| {text:<{w}} |"
        
        def sep():
            return "+" + "-" * (w + 2) + "+"
        
        lines = []
        lines.append(sep())
        
        # Header: author + nurtured tag
        nurtured_tag = " [VIP]" if self.is_nurtured else ""
        time_tag = f" ({self.time_ago})" if self.time_ago else ""
        lines.append(line(f"POST: @{self.author}{nurtured_tag}{time_tag}"))
        lines.append(sep())
        
        # Screenshot status
        if self.screenshot_ok:
            lines.append(line(f"Screenshot: OK ({self.screenshot_kb:.0f}KB)"))
        else:
            lines.append(line("Screenshot: MISSING"))
        
        # Caption
        if self.caption:
            short = self.caption[:45] + "..." if len(self.caption) > 45 else self.caption
            short = short.replace("\n", " ")
            exp_tag = " [expanded]" if self.caption_expanded else ""
            lines.append(line(f"Caption{exp_tag}: {short}"))
        else:
            lines.append(line("Caption: (none)"))
        
        # CTA
        if self.has_cta:
            lines.append(line(f"CTA: \"{self.cta_word}\" << MANDATORY"))
        
        # Visible comments from others
        if self.visible_comments:
            hint = f" (of {self.total_comments_hint})" if self.total_comments_hint else ""
            lines.append(line(f"Others' comments{hint}:"))
            for c in self.visible_comments[:5]:
                u = c.get("username", "?")[:12]
                t = c.get("text", "")[:35]
                lines.append(line(f"  @{u}: {t}"))
        else:
            lines.append(line("Others' comments: (none visible)"))
        
        # Our recent comments
        if self.our_recent:
            lines.append(line(f"Our recent ({len(self.our_recent)}):"))
            for c in self.our_recent[:3]:
                lines.append(line(f"  \"{c[:40]}\""))
        
        # Action taken
        lines.append(sep())
        action_icons = {
            "comment": ">>",
            "like": "<3",
            "save": "[S]",
            "skip": ">>|",
        }
        icon = action_icons.get(self.action, "?")
        if self.action == "comment":
            lines.append(line(f"{icon} COMMENT: \"{self.action_detail}\""))
        elif self.action == "skip":
            lines.append(line(f"{icon} SKIP: {self.action_detail}"))
        elif self.action == "like":
            lines.append(line(f"{icon} LIKED"))
        elif self.action:
            lines.append(line(f"{icon} {self.action.upper()}: {self.action_detail}"))
        
        if self.decision_reason:
            lines.append(line(f"    Reason: {self.decision_reason}"))
        
        lines.append(sep())
        
        # Output all at once
        for l in lines:
            self.logger.info(l)
        
        self._reset()


class SessionProgressBar:
    """Simple ASCII progress bar for session status.
    
    Compatible with Windows CMD. Updates inline.
    
    Usage:
        bar = SessionProgressBar(logger, total_posts=15, comment_budget=3)
        bar.update(posts_seen=1, liked=1)
        bar.update(posts_seen=5, liked=3, commented=1)
    """
    
    def __init__(self, logger: logging.Logger, total_posts: int = 15, comment_budget: int = 5):
        self.logger = logger
        self.total_posts = total_posts
        self.comment_budget = comment_budget
    
    def update(self, posts_seen: int = 0, liked: int = 0, commented: int = 0, 
               skipped: int = 0, elapsed_min: float = 0):
        """Log a progress line."""
        # Progress bar [=====>          ] 33%
        pct = min(100, int(posts_seen / max(self.total_posts, 1) * 100))
        filled = pct // 5  # 20 chars max
        bar = "=" * filled + ">" + " " * (20 - filled - 1)
        
        comment_str = f"{commented}/{self.comment_budget}"
        time_str = f"{elapsed_min:.0f}m" if elapsed_min else ""
        
        self.logger.info(
            f"[{bar}] {pct}% | "
            f"Posts:{posts_seen} Like:{liked} Comment:{comment_str} Skip:{skipped}"
            f"{' | ' + time_str if time_str else ''}"
        )
