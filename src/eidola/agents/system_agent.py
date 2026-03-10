"""
System Agent - Generic agent for device-level tasks.

Handles arbitrary device tasks like:
- Login to Play Store with Gmail
- Install/update apps
- System settings configuration
- Any task given in natural language
"""

import logging
from typing import Any

from google.adk.agents import Agent
from google.adk.tools import FunctionTool
from google.genai import types

from ..config import get_settings

logger = logging.getLogger("eidola.agents.system")

settings = get_settings()


def _build_system_instruction() -> str:
    """Build the system agent instruction prompt."""
    return """# System Agent - Device Task Handler

You are a System Agent that performs device-level tasks on Android devices.
You have access to Android device controls via FIRERPA tools.

---

## Capabilities

You can:
- Navigate the Android device UI using tap, scroll, type, back, home
- Open and interact with any app (Play Store, Settings, browsers, etc.)
- Generate 2FA codes for account authentication
- Read screen content via XML dump for navigation
- Perform multi-step workflows

---

## Workflow

1. **Understand the task**: Read the task description carefully
2. **Analyze current screen**: Call `detect_screen()` and `get_elements_for_ai()` 
3. **Plan your approach**: Identify what steps are needed
4. **Execute step by step**: Perform actions with verification
5. **Handle obstacles**: If something unexpected happens, try alternative approaches

---

## Tools Available

**Navigation:**
- `tap(x, y)` - Tap at coordinates
- `type_text(text)` - Type text into focused field
- `scroll_feed(mode)` - Scroll the screen
- `press_back()` - Press back button
- `press_home()` - Press home button

**Screen Analysis:**
- `detect_screen()` - Identify current screen type
- `get_elements_for_ai(max_elements)` - Get clickable UI elements with coordinates
- `find_element(selector)` - Find specific element

**Authentication (Instagram):**
- `generate_2fa_code(account_id)` - Generate TOTP code for Instagram 2FA
- `get_account_credentials(account_id)` - Get Instagram login credentials

**Authentication (Gmail/Play Store):**
- `get_gmail_credentials(device_id)` - Get Gmail credentials for this device (e.g., "phone_01")
- `generate_gmail_2fa_code(device_id)` - Generate 2FA code for Gmail on this device
- `generate_2fa_code_raw(totp_secret)` - Generate 2FA code from raw secret

**App Control:**
- `open_app(package_name)` - Open an app by package name
- `close_app(package_name)` - Force stop an app

---

## Common Packages

- Play Store: `com.android.vending`
- Settings: `com.android.settings`
- Chrome: `com.android.chrome`
- Gmail: `com.google.android.gm`
- Instagram: `com.instagram.android`
- YouTube: `com.google.android.youtube`

---

## Device-Gmail Mapping

Each device (phone_01 to phone_10) has a dedicated Gmail account configured.
Use `get_gmail_credentials(device_id)` to get credentials for the current device.

Example for phone_01:
- `get_gmail_credentials("phone_01")` returns email, password
- `generate_gmail_2fa_code("phone_01")` returns 2FA code

---

## Example Tasks

### Login to Play Store with device's Gmail:
1. Get credentials: `get_gmail_credentials("phone_01")` → returns email, password
2. Open Play Store: `open_app("com.android.vending")`
3. Find "Sign in" button via `get_elements_for_ai()`
4. Enter email from credentials
5. Enter password
6. Handle 2FA: `generate_gmail_2fa_code("phone_01")` → enter the code
7. Verify success

### Enable Developer Options:
1. Open Settings
2. Find "About phone"
3. Tap "Build number" 7 times
4. Go back and find Developer options

---

## Important Rules

1. **Always verify**: After each action, check if it succeeded
2. **Be patient**: Wait for UI transitions (use `detect_screen()` to verify)
3. **Handle errors**: If an action fails, try alternative approaches
4. **Report progress**: Describe what you're doing at each step
5. **Ask if unclear**: If the task is ambiguous, ask for clarification

---

## Privacy & Security Rules (CRITICAL)

During ANY login flow (Play Store, Google, Gmail, etc.), ALWAYS:

1. **DECLINE all optional data collection:**
   - "Help improve..." → No / Decline / Skip
   - "Send usage data..." → No / Decline
   - "Share diagnostics..." → No / Decline
   - "Personalize ads..." → No / Decline
   - "Help Google improve..." → No thanks

2. **DECLINE location/GPS requests:**
   - "Enable location..." → No / Skip / Not now
   - "Allow access to location..." → Deny / Don't allow
   - "Improve location accuracy..." → No thanks

3. **SKIP phone number:**
   - "Add phone number..." → Skip / Not now / Later
   - "Verify phone..." → Skip if possible
   - "Recovery phone..." → Skip

4. **SKIP backup/sync options:**
   - "Back up to Google Drive..." → Skip / Not now
   - "Sync contacts..." → Skip / No
   - "Restore from backup..." → Set up as new / Skip

5. **DECLINE biometrics setup:**
   - "Set up fingerprint..." → Skip / Later
   - "Face unlock..." → Skip / Later

6. **DECLINE Google Assistant:**
   - "Set up Google Assistant..." → No thanks / Skip
   - "Hey Google..." → Skip

7. **General rule**: If given a choice between "Yes/Accept" and "No/Skip/Later/Decline", choose the NEGATIVE option for anything related to:
   - Data collection
   - Telemetry
   - Location
   - Phone number
   - Personalization
   - Additional services

**Look for these button texts:** Skip, No thanks, Not now, Later, Decline, Don't allow, No, Maybe later

---

## Error Recovery

If something goes wrong:
1. Press back to go to previous screen
2. Try the action again with slight variation
3. If stuck, press Home and start over
4. Report what went wrong

Execute the task given to you step by step, verifying success at each stage.
"""


def create_system_agent(device_ip: str) -> Agent:
    """
    Create a System Agent for device-level tasks.
    
    The System Agent can handle arbitrary device tasks via natural language:
    - Login to Play Store
    - Install/update apps
    - Configure settings
    - Any device interaction task
    
    Args:
        device_ip: IP address of the FIRERPA device
        
    Returns:
        Agent configured for system tasks
    """
    # Import tool factories
    from ..tools.firerpa_tools import create_firerpa_tools, get_device_manager
    from ..tools.auth_tools import create_auth_tools
    
    # Get base FIRERPA tools (tap, scroll, type, etc.)
    firerpa_tools = create_firerpa_tools(device_ip)
    
    # Get auth tools (2FA, credentials, etc.)
    auth_tools = create_auth_tools()
    
    # Additional system tools
    def open_app(package_name: str) -> dict[str, Any]:
        """
        Open an app by package name.
        
        Args:
            package_name: Android package name (e.g., "com.android.vending")
            
        Returns:
            dict with success status
        """
        from ..tools.firerpa_tools import get_device_manager
        
        dm = get_device_manager()
        try:
            app = dm.device.application(package_name)
            app.start()
            dm.device.wait_for_idle(timeout=3000)
            
            return {
                "success": True,
                "package": package_name,
                "message": f"Opened {package_name}",
            }
        except Exception as e:
            logger.error(f"Failed to open app {package_name}: {e}")
            return {
                "success": False,
                "error": str(e),
            }
    
    def close_app(package_name: str) -> dict[str, Any]:
        """
        Force stop an app.
        
        Args:
            package_name: Android package name
            
        Returns:
            dict with success status
        """
        from ..tools.firerpa_tools import get_device_manager
        
        dm = get_device_manager()
        try:
            dm.device.execute_script(f"am force-stop {package_name}")
            
            return {
                "success": True,
                "package": package_name,
                "message": f"Closed {package_name}",
            }
        except Exception as e:
            logger.error(f"Failed to close app {package_name}: {e}")
            return {
                "success": False,
                "error": str(e),
            }
    
    def get_current_app() -> dict[str, Any]:
        """
        Get the currently focused app.
        
        Returns:
            dict with current app info
        """
        from ..tools.firerpa_tools import get_device_manager
        
        dm = get_device_manager()
        try:
            result = dm.device.execute_script(
                "dumpsys window windows | grep -E 'mCurrentFocus'"
            )
            
            return {
                "success": True,
                "current_focus": result.strip() if result else None,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }
    
    # Build tools list from factories + system-specific tools
    tools = (
        firerpa_tools +  # Navigation, screen analysis from create_firerpa_tools()
        auth_tools +     # 2FA, credentials from create_auth_tools()
        [
            # System-specific app control tools
            FunctionTool(open_app),
            FunctionTool(close_app),
            FunctionTool(get_current_app),
        ]
    )
    
    from .callbacks import compress_tool_response

    agent = Agent(
        name="SystemAgent",
        model=settings.default_model,
        instruction=_build_system_instruction(),
        tools=tools,
        generate_content_config=types.GenerateContentConfig(
            temperature=1.0,
            top_p=0.95,
            thinking_config=types.ThinkingConfig(
                thinking_level="MINIMAL",
            ),
        ),
        after_tool_callback=compress_tool_response,
    )
    
    return agent


def create_system_agent_runner(device_ip: str):
    """
    Create a runner for executing system agent tasks.
    
    Provides a simple interface to run tasks and get results.
    
    Args:
        device_ip: Device IP address
        
    Returns:
        SystemAgentRunner instance
    """
    return SystemAgentRunner(device_ip)


class SystemAgentRunner:
    """
    Runner for executing system agent tasks.
    
    Usage:
        runner = SystemAgentRunner("192.168.1.100")
        result = await runner.run_task("Login to Play Store with test@gmail.com")
    """
    
    def __init__(self, device_ip: str):
        """
        Initialize system agent runner.
        
        Args:
            device_ip: FIRERPA device IP
        """
        self.device_ip = device_ip
        self.agent = None
    
    async def run_task(self, task: str, max_turns: int = 50) -> dict[str, Any]:
        """
        Run a task using the system agent.
        
        Args:
            task: Natural language task description
            max_turns: Maximum conversation turns
            
        Returns:
            dict with task results
        """
        from google.adk.apps import App
        from google.adk.apps.app import EventsCompactionConfig
        from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
        from google.adk.models import Gemini
        from google.adk.runners import Runner
        from ..memory.windowed_session import WindowedSessionService
        
        # Create agent on first run
        if not self.agent:
            self.agent = create_system_agent(self.device_ip)
        
        # Set up runner with windowing + compaction (was InMemorySessionService — unbounded)
        session_service = WindowedSessionService(
            max_events=50,
            compress_xml=True,
        )
        app = App(
            name="system_agent",
            root_agent=self.agent,
            events_compaction_config=EventsCompactionConfig(
                compaction_interval=5,
                overlap_size=1,
                summarizer=LlmEventSummarizer(
                    llm=Gemini(model="gemini-2.5-flash"),
                ),
            ),
        )
        runner = Runner(app=app, session_service=session_service)
        
        # Create session
        session = await session_service.create_session(
            user_id="system",
            app_name="system_agent",
            state={"task": task},
        )
        
        # Build task message
        user_message = types.Content(
            role="user",
            parts=[types.Part(text=task)],
        )
        
        # Run agent
        results = []
        turn_count = 0
        last_author = None
        task_completed = False
        
        try:
            while turn_count < max_turns and not task_completed:
                turn_count += 1
                model_responded = False
                
                async for event in runner.run_async(
                    user_id="system",
                    session_id=session.id,
                    new_message=user_message,
                ):
                    author = getattr(event, "author", "unknown")
                    last_author = author
                    
                    # Log events
                    if hasattr(event, 'content') and event.content:
                        for part in event.content.parts:
                            if hasattr(part, 'text') and part.text:
                                logger.info(f"[{author}] {part.text[:200]}")
                                results.append(part.text)
                                # Check for explicit completion phrases from agent
                                text_lower = part.text.lower()
                                if any(phrase in text_lower for phrase in [
                                    "task completed", "successfully logged in",
                                    "login successful", "sign-in complete",
                                    "i have completed", "the task is done"
                                ]):
                                    task_completed = True
                            if hasattr(part, 'function_call') and part.function_call:
                                fc = part.function_call
                                logger.info(f"🔧 [{author}] {fc.name}({dict(fc.args) if fc.args else ''})")
                    
                    if author == self.agent.name:
                        model_responded = True
                
                # If model didn't respond or gave up, stop
                if not model_responded:
                    logger.warning("Model didn't respond, stopping")
                    break
                
                # Continue with "continue" prompt for next turn
                user_message = types.Content(
                    role="user",
                    parts=[types.Part(text="continue")],
                )
                
                logger.info(f"--- Turn {turn_count} complete ---")
        
        except Exception as e:
            logger.error(f"Task failed: {e}", exc_info=True)
            return {
                "success": False,
                "task": task,
                "error": str(e),
                "turns": turn_count,
                "results": results,
            }
        
        return {
            "success": True,
            "task": task,
            "turns": turn_count,
            "results": results,
            "message": "Max turns reached",
        }
