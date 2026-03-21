"""Tools for Eidola agents.

XML-FIRST NAVIGATION SYSTEM:
- Screen detection via XML analysis
- Smart element finding with fallbacks
- Human-like gestures
- Automatic recovery workflows
"""

__all__ = []

# =============================================================================
# Core Data Models (no external dependencies)
# =============================================================================

from .action_models import (
    ActionType,
    ScreenContext,
    ScrollDirection,
    ElementBasedAction,
    VerificationResult,
    NavigationGoal,
    DialogAction,
)
__all__.extend([
    "ActionType",
    "ScreenContext",
    "ScrollDirection",
    "ElementBasedAction",
    "VerificationResult",
    "NavigationGoal",
    "DialogAction",
])

from .selectors import (
    INSTAGRAM_SELECTORS,
    SCREEN_SIGNATURES,
    SELECTOR_VERSION,
    get_selector,
    get_all_selectors,
    get_screen_signature,
)
__all__.extend([
    "INSTAGRAM_SELECTORS",
    "SCREEN_SIGNATURES",
    "SELECTOR_VERSION",
    "get_selector",
    "get_all_selectors",
    "get_screen_signature",
])

from .timeouts import (
    Stage,
    StageConfig,
    STAGE_CONFIGS,
    ThrottleConfig,
    DEFAULT_THROTTLE,
    get_config,
    get_throttle_delay,
    RetryContext,
    MIN_XML_SIZE,
)
__all__.extend([
    "Stage",
    "StageConfig",
    "STAGE_CONFIGS",
    "ThrottleConfig",
    "DEFAULT_THROTTLE",
    "get_config",
    "get_throttle_delay",
    "RetryContext",
    "MIN_XML_SIZE",
])

# =============================================================================
# XML Navigation Components (no external dependencies)
# =============================================================================

from .screen_detector import (
    ScreenDetectionResult,
    detect_screen,
    is_in_instagram,
    needs_recovery,
)
__all__.extend([
    "ScreenDetectionResult",
    "detect_screen",
    "is_in_instagram",
    "needs_recovery",
])

from .element_finder import (
    FoundElement,
    SmartElementFinder,
    create_finder,
)
__all__.extend([
    "FoundElement",
    "SmartElementFinder",
    "create_finder",
])

from .state_verifier import (
    StateVerifier,
    VerificationConfig,
    VerificationStatus,
    StateSnapshot,
    create_verifier,
)
__all__.extend([
    "StateVerifier",
    "VerificationConfig",
    "VerificationStatus",
    "StateSnapshot",
    "create_verifier",
])

from .dialog_handler import (
    DialogHandler,
    DialogType,
    DialogActionType,
    DialogConfig,
    DetectedDialog,
    create_dialog_handler,
    KNOWN_DIALOGS,
)
__all__.extend([
    "DialogHandler",
    "DialogType",
    "DialogActionType",
    "DialogConfig",
    "DetectedDialog",
    "create_dialog_handler",
    "KNOWN_DIALOGS",
])

from .escape_workflows import (
    EscapeWorkflows,
    EscapeAction,
    EscapeResult,
    WorkflowStep,
    STATE_WORKFLOWS,
    create_escape_workflows,
)
__all__.extend([
    "EscapeWorkflows",
    "EscapeAction",
    "EscapeResult",
    "WorkflowStep",
    "STATE_WORKFLOWS",
    "create_escape_workflows",
])

# =============================================================================
# Gesture Systems (requires lamda)
# =============================================================================

# Simple gestures (requires lamda only, no ADK)
try:
    from .simple_gestures import (
        SimpleGestures,
        GestureStats,
        create_simple_gestures,
    )
    __all__.extend([
        "SimpleGestures",
        "GestureStats", 
        "create_simple_gestures",
    ])
except ImportError:
    pass

# Legacy gesture generator (no external dependencies)
from .gesture_generator import (
    HumanGestureGenerator,
    SwipeGesture,
    GestureSequence,
    Point,
)
__all__.extend([
    "HumanGestureGenerator",
    "SwipeGesture",
    "GestureSequence",
    "Point",
])

# =============================================================================
# SDK-based tools (requires google-adk and lamda)
# =============================================================================

try:
    from .firerpa_tools import (
        DeviceManager,
        create_firerpa_tools,
        create_navigator_tools,
        create_observer_tools,
        create_engager_tools,
    )
    __all__.extend([
        "DeviceManager",
        "create_firerpa_tools",
        "create_navigator_tools",
        "create_observer_tools",
        "create_engager_tools",
    ])
except ImportError:
    pass

# =============================================================================
# Action Budget Tool - DEPRECATED
# =============================================================================
# action_budget.py has been removed.
# Sessions now use the scheduler with real-time budget tracking.
# See: src/eidola/scheduler/session_runner.py
