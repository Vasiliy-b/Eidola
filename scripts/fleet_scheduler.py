#!/usr/bin/env python3
"""
Fleet Scheduler Daemon — runs agents on human-like daily plans.

Generates per-device daily plans (or loads existing from MongoDB),
then executes sessions at their scheduled times. Plans survive restarts:
if restarted mid-day, already-completed sessions are skipped.

Usage:
    python scripts/fleet_scheduler.py                         # all devices
    python scripts/fleet_scheduler.py --device phone_04       # single device
    python scripts/fleet_scheduler.py --dry-run               # show plans, don't execute
    python scripts/fleet_scheduler.py --regenerate-plan       # force new plans for today

Workflow:
    1. Start redsocks proxy on all target devices
    2. For each device, generate or load today's plan from MongoDB
    3. Launch per-device async tasks in parallel
    4. Each task: find next pending session → sleep until start → run agent → mark done → loop
    5. At midnight: generate tomorrow's plan and continue
    6. Handle Ctrl+C gracefully (terminate agent subprocesses)
"""

import argparse
import asyncio
import os
import signal
import subprocess
import os
import sys
import time as time_module
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Suppress noisy gRPC C-core keepalive warnings (too_many_pings GOAWAY).
# These are non-fatal: gRPC auto-throttles keepalive interval and reconnects.
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

from dotenv import load_dotenv

# ============================================================================
# PATH SETUP
# ============================================================================
_project_root = Path(__file__).parent.parent
load_dotenv(_project_root / ".env")
sys.path.insert(0, str(_project_root / "src"))
sys.path.insert(0, str(_project_root / "scripts"))

from eidola.config import load_device_config

# Import fleet proxy logic
from fleet_proxy_start import (
    ALL_DEVICES,
    start_proxy_on_device,
    print_summary_table,
)

# Import daily plan module
from eidola.scheduler.daily_plan import (
    DailyPlanGenerator,
    DailyPlan,
    PlannedSession,
    get_or_generate_plan,
    get_or_generate_device_plan,
    find_next_pending,
    format_plan_table,
)

# MongoDB
from pymongo import MongoClient


# ============================================================================
# CONSTANTS
# ============================================================================
DEFAULT_TIMEZONE = ZoneInfo("America/New_York")
PROCESS_TIMEOUT_BUFFER = 300  # 5 min buffer after session duration for subprocess timeout


# ============================================================================
# AGENT SUBPROCESS
# ============================================================================

def run_agent_subprocess(
    device_id: str,
    account: str,
    mode: str = "active_engage",
    duration: int | None = None,
    extra_args: list[str] | None = None,
    username: str | None = None,
) -> subprocess.Popen:
    """Launch an agent as a subprocess.

    Args:
        device_id: Device identifier.
        account: Instagram account ID.
        mode: Agent mode (warmup, active_engage, etc.).
        duration: Session duration in seconds.
        extra_args: Additional CLI arguments.
        username: Instagram username (may differ from account_id, e.g. _jessdiazz vs jessdiazz).
    """
    cmd = [
        sys.executable, str(_project_root / "run.py"),
        "--device-id", device_id,
        "--account", account,
        "--mode", mode,
        "--no-isolation",
    ]
    if username:
        cmd.extend(["--username", username])
    if duration is not None:
        cmd.extend(["--duration", str(duration)])
    if extra_args:
        cmd.extend(extra_args)

    # Log to file
    log_dir = _project_root / "logs"
    log_dir.mkdir(exist_ok=True)
    timestamp = time_module.strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"sched_{device_id}_{account}_{timestamp}.log"

    lf = open(log_file, "w")
    proc = subprocess.Popen(
        cmd,
        stdout=lf,
        stderr=subprocess.STDOUT,
        cwd=str(_project_root),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    # Keep reference to the log file handle so it stays open while the
    # subprocess is running. The handle will be closed when the process
    # object is garbage-collected or explicitly via proc._log_file.close().
    proc._log_file = lf  # type: ignore[attr-defined]

    print(f"  [{device_id}] Agent PID {proc.pid} | log: {log_file.name}")
    return proc


def _close_proc_log(proc: subprocess.Popen) -> None:
    """Close the log file handle attached to a subprocess, if any."""
    lf = getattr(proc, "_log_file", None)
    if lf is not None:
        try:
            lf.close()
        except Exception:
            pass


async def wait_for_process(proc: subprocess.Popen, timeout: int) -> int:
    """Wait for a subprocess to complete with timeout.

    Uses asyncio-friendly polling so other tasks keep running.
    Closes the attached log file handle when the process exits.

    Returns:
        Process return code (or -1 if killed).
    """
    start = time_module.monotonic()
    while True:
        ret = proc.poll()
        if ret is not None:
            _close_proc_log(proc)
            return ret

        elapsed = time_module.monotonic() - start
        if elapsed > timeout:
            print(f"  [PID {proc.pid}] TIMEOUT ({timeout}s) — killing")
            try:
                proc.terminate()
                await asyncio.sleep(3)
                if proc.poll() is None:
                    proc.kill()
            except ProcessLookupError:
                pass
            _close_proc_log(proc)
            return -1

        await asyncio.sleep(5)  # Poll every 5 seconds


# ============================================================================
# HELPER: SLEEP UNTIL TARGET TIME
# ============================================================================

async def sleep_until(target: datetime, device_id: str) -> bool:
    """Sleep until target datetime, logging periodic status.

    Returns:
        True if reached the target time. False if midnight passed (day changed).
    """
    while True:
        now = datetime.now(target.tzinfo)
        remaining = (target - now).total_seconds()

        if remaining <= 0:
            return True

        # Check if day changed (past midnight)
        if now.date() > target.date():
            return False

        # Log every ~10 minutes while waiting
        if remaining > 600:
            print(f"  [{device_id}] Sleeping {remaining / 60:.0f} min until {target.strftime('%H:%M')}")
            await asyncio.sleep(min(remaining, 600))
        elif remaining > 60:
            await asyncio.sleep(min(remaining, 60))
        else:
            await asyncio.sleep(remaining)
            return True


async def sleep_until_midnight(tz: ZoneInfo, device_id: str):
    """Sleep until midnight in the given timezone."""
    now = datetime.now(tz)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    remaining = (tomorrow - now).total_seconds()
    print(f"  [{device_id}] All sessions done. Sleeping {remaining / 3600:.1f}h until midnight.")
    await asyncio.sleep(remaining)


# ============================================================================
# PRE-SESSION ACCOUNT SWITCHING
# ============================================================================

def _cleanup_instagram_state(dm) -> None:
    """Reset Instagram to a clean state after failed switch/login.
    
    Press Back 3x to dismiss any open sheets/dialogs, then restart Instagram.
    This prevents leaving the app on switcher/login screen which blocks next sessions.
    """
    import time as _time
    from lamda.client import Keys
    
    try:
        for _ in range(3):
            dm.device.press_key(Keys.KEY_BACK)
            _time.sleep(0.5)
        
        # Force restart Instagram to get a clean feed state
        app = dm.device.application("com.instagram.android")
        app.stop()
        _time.sleep(1.0)
        app.start()
        _time.sleep(3.0)
        print("    [cleanup] Instagram restarted to clean state")
    except Exception as e:
        print(f"    [cleanup] Warning: {e}")


def _ensure_clean_instagram_state(dm, device_id: str, max_attempts: int = 4) -> bool:
    """Normalize Instagram UI before launching agent tools.

    Guarantees we are not stuck on account switcher/login/add-account screens.
    """
    import time as _time
    from lamda.client import Keys
    from eidola.tools.screen_detector import detect_screen

    def _read_context() -> str:
        dm.invalidate_xml_cache()
        xml_bytes = dm.device.dump_window_hierarchy()
        xml_str = xml_bytes.getvalue().decode("utf-8")
        return detect_screen(xml_str).context.value

    try:
        for attempt in range(max_attempts):
            context = _read_context()
            if context in {
                "instagram_feed",
                "instagram_profile",
                "instagram_reels",
                "instagram_post_detail",
                "instagram_comments",
                "instagram_other",
            }:
                feed_tab = dm.device(resourceId="com.instagram.android:id/feed_tab")
                if feed_tab.exists():
                    feed_tab.click()
                context_after = _read_context()
                if context_after not in {"instagram_account_switcher", "instagram_add_account", "instagram_login"}:
                    return True

            if context in {"instagram_account_switcher", "instagram_add_account", "instagram_login", "instagram_search"}:
                dm.device.press_key(Keys.KEY_BACK)
                _time.sleep(0.3)
                continue

            app = dm.device.application("com.instagram.android")
            if not app.is_foreground():
                app.start()
                dm.device.wait_for_idle(timeout=2000)

            feed_tab = dm.device(resourceId="com.instagram.android:id/feed_tab")
            if feed_tab.exists():
                feed_tab.click()

        final_context = _read_context()
        print(f"  [{device_id}] ✗ Could not stabilize Instagram context: {final_context}")
        return False
    except Exception as e:
        print(f"  [{device_id}] ✗ Context stabilization error: {e}")
        return False


def switch_account_on_device(
    device_ip: str,
    device_id: str,
    target_account_id: str,
    current_account_id: str | None,
) -> dict:
    """Switch Instagram account on device before starting a session.
    
    Connects to the device via FIRERPA, checks current account,
    and switches if needed. Handles login for accounts not yet on device.
    
    Args:
        device_ip: Device IP address for FIRERPA connection
        device_id: Device identifier
        target_account_id: Account ID to switch to
        current_account_id: Account that was used in the previous session (or None)
        
    Returns:
        dict with success, switched (bool), error
    """
    # Always verify on device — DB/caller state may be stale
    print(f"  [{device_id}] Verifying account: {target_account_id} (caller thinks: {current_account_id or '?'})")
    
    try:
        # Connect to device via singleton registry and set as current
        from eidola.tools.firerpa_tools import DeviceManager
        dm = DeviceManager.get(device_ip)
        DeviceManager.set_current(device_ip)
        
        # Ensure Instagram is open (proxy setup force-stops all apps)
        import time as _time
        app = dm.device.application("com.instagram.android")
        if not app.is_foreground():
            print(f"  [{device_id}] Opening Instagram...")
            app.start()
            dm.device.wait_for_idle(timeout=3000)
        
        # Load account config to get username
        from eidola.config import load_account_config
        config = load_account_config(target_account_id)
        if not config:
            return {"success": False, "error": f"Account config not found: {target_account_id}"}
        
        target_username = config.instagram.username
        
        # Import switching tools
        from eidola.tools.auth_tools import (
            switch_instagram_account, mark_account_on_device,
            get_logged_in_accounts, _navigate_to_login_screen,
        )
        
        # Check which account is active — first try current screen, then profile tab
        current = get_logged_in_accounts()
        current_name = current.get("current_account", "")
        
        # If current screen doesn't show username (e.g. feed), go to profile tab
        if not current_name:
            from lamda.client import Point
            profile_tab = dm.device(resourceId="com.instagram.android:id/profile_tab")
            if profile_tab.exists():
                profile_tab.click()
                dm.device.wait_for_idle(timeout=1500)
                current = get_logged_in_accounts()
                current_name = current.get("current_account", "")
        
        if current_name and current_name.lower() == target_username.lower():
            # Go to feed tab for clean state before agent launch
            feed_tab = dm.device(resourceId="com.instagram.android:id/feed_tab")
            if feed_tab.exists():
                feed_tab.click()
            print(f"  [{device_id}] Already on @{target_username} (verified)")
            mark_account_on_device(device_id, target_account_id, target_username)
            return {"success": True, "switched": False, "did_login": False}
        
        if current_name:
            print(f"  [{device_id}] Active: @{current_name}, need: @{target_username} — switching...")
        else:
            print(f"  [{device_id}] Could not detect active account — switching...")
        
        # switch_instagram_account opens switcher → find account → tap or return need_login
        result = switch_instagram_account(target_username)
        
        if result.get("success"):
            mark_account_on_device(device_id, target_account_id, target_username)
            # Ensure clean state: dismiss any leftover UI, go to feed
            from lamda.client import Keys
            dm.device.press_key(Keys.KEY_BACK)
            feed_tab = dm.device(resourceId="com.instagram.android:id/feed_tab")
            if feed_tab.exists():
                feed_tab.click()
                dm.device.wait_for_idle(timeout=1000)
            if not _ensure_clean_instagram_state(dm, device_id):
                return {"success": False, "error": "Post-switch UI not stable (switcher/login still present)"}
            print(f"  [{device_id}] ✓ Switched to @{target_username}")
            return {"success": True, "switched": True, "did_login": False}
        
        if result.get("need_login"):
            print(f"  [{device_id}] Account @{target_username} not on device — need agent login")
            # Navigate to login screen: tap "Add Instagram account" → "Log into existing"
            from eidola.tools.auth_tools import _navigate_to_login_screen
            nav_result = _navigate_to_login_screen()
            if not nav_result.get("success"):
                print(f"  [{device_id}] ✗ Could not navigate to login screen: {nav_result.get('error')}")
                _cleanup_instagram_state(dm)
                return {"success": False, "error": f"Nav to login failed: {nav_result.get('error')}"}
            
            # Launch agent in login mode — it handles credentials + 2FA + dialogs
            print(f"  [{device_id}] Launching login agent for @{target_username}...")

            # Disconnect scheduler's gRPC channel so the login subprocess
            # doesn't compete for pings on the same device (too_many_pings).
            try:
                dm.disconnect()
            except Exception:
                pass

            login_proc = run_agent_subprocess(
                device_id=device_id,
                account=target_account_id,
                mode="login",
                duration=180,  # 3 min for login
                username=target_username,
            )
            ret = login_proc.wait(timeout=240)
            
            if ret == 0:
                mark_account_on_device(device_id, target_account_id, target_username)
                if not _ensure_clean_instagram_state(dm, device_id):
                    return {"success": False, "error": "Post-login UI not stable (switcher/login still present)"}
                print(f"  [{device_id}] ✓ Logged in @{target_username} via agent")
                return {"success": True, "switched": True, "did_login": True}
            else:
                print(f"  [{device_id}] ✗ Login agent failed (exit code {ret})")
                _cleanup_instagram_state(dm)
                return {"success": False, "error": f"Login agent exit code {ret}"}
        
        err = result.get("error", "Switch failed")
        print(f"  [{device_id}] ✗ Switch failed: {err}")
        _cleanup_instagram_state(dm)
        return {"success": False, "error": err}
    
    except Exception as e:
        print(f"  [{device_id}] ✗ Switch error: {e}")
        try:
            _cleanup_instagram_state(dm)
        except Exception:
            pass
        return {"success": False, "error": str(e)}


# ============================================================================
# PER-DEVICE LOOP
# ============================================================================

# Task passed to run.py when --skip-login: agent assumes already logged in
SKIP_LOGIN_TASK = (
    "You are already logged in. Do not attempt login or check login state. "
    "Open Instagram if needed and browse the home feed, engage with posts according to your mode for the scheduled duration."
)


async def _staggered_device_loop(stagger_delay: int = 0, **kwargs):
    """Wrapper that delays device_loop start to prevent thundering herd."""
    if stagger_delay > 0:
        await asyncio.sleep(stagger_delay)
    await device_loop(**kwargs)


async def device_loop(
    device_id: str,
    accounts: list[str],
    device_ip: str,
    plan_generator: DailyPlanGenerator,
    mongo_db,
    timezone: ZoneInfo,
    shutdown_event: asyncio.Event,
    dry_run: bool = False,
    force_regenerate: bool = False,
    skip_login: bool = False,
    start_now: bool = False,
):
    """Main loop for one device: run today's plan, sleep, repeat.

    Supports multiple accounts per device. If multiple accounts are assigned,
    generates a merged daily plan with staggered sessions per account,
    and switches accounts on the device before each session.

    Args:
        device_id: Device identifier.
        accounts: List of Instagram account IDs assigned to this device.
        device_ip: Device IP for FIRERPA connection (used for switching).
        plan_generator: DailyPlanGenerator instance.
        mongo_db: pymongo Database.
        timezone: Local timezone for this device.
        shutdown_event: Fires on Ctrl+C to abort.
        dry_run: If True, show plan but don't execute.
        force_regenerate: Force regenerate plan even if one exists.
        skip_login: If True, pass task so agent does not attempt login.
        start_now: If True, skip wait for first session (for testing).
    """
    multi = len(accounts) > 1
    accts_str = ", ".join(accounts)
    print(f"\n  [{device_id}] Starting device loop for {'accounts' if multi else 'account'}: {accts_str}")

    # Initialize current_account: single-account uses accounts[0], multi reads from MongoDB
    current_account: str | None = None
    if not multi:
        current_account = accounts[0]
    else:
        try:
            from eidola.tools.auth_tools import get_accounts_on_device
            on_device = get_accounts_on_device(device_id)
            for acc in on_device:
                if acc.get("logged_in") and acc.get("account_id") in accounts:
                    current_account = acc["account_id"]
                    print(f"  [{device_id}] Detected active account from DB: {current_account}")
                    break
        except Exception:
            pass

    while not shutdown_event.is_set():
        today = date.today()

        # ---- Get or generate today's plan ----
        try:
            if multi:
                plan = get_or_generate_device_plan(
                    device_id=device_id,
                    account_ids=accounts,
                    target_date=today,
                    mongo_db=mongo_db,
                    generator=plan_generator,
                    timezone=timezone,
                    force_regenerate=force_regenerate,
                )
            else:
                plan = get_or_generate_plan(
                    device_id=device_id,
                    account_id=accounts[0],
                    target_date=today,
                    mongo_db=mongo_db,
                    generator=plan_generator,
                    timezone=timezone,
                    force_regenerate=force_regenerate,
                )
        except Exception as e:
            import traceback
            print(f"\n  [{device_id}] ERROR generating plan: {e}")
            traceback.print_exc()
            return
        force_regenerate = False  # Only force-regenerate once

        print(f"\n{format_plan_table(plan)}\n")

        if dry_run:
            print(f"  [{device_id}] --dry-run: not executing sessions")
            return

        # ---- Execute sessions ----
        while not shutdown_event.is_set():
            next_session = find_next_pending(plan)

            if not next_session:
                # All done for today
                await sleep_until_midnight(timezone, device_id)
                break  # Will loop back and generate tomorrow's plan

            now = datetime.now(timezone)
            wait_seconds = (next_session.start_time - now).total_seconds()

            # Determine which account this session is for
            session_account = next_session.account_id if next_session.account_id and next_session.account_id.strip() else accounts[0]

            if wait_seconds > 0 and not start_now:
                acct_tag = f" @{session_account}" if multi else ""
                print(
                    f"  [{device_id}] Next: {next_session.label}{acct_tag} at "
                    f"{next_session.start_time.strftime('%H:%M')} "
                    f"({wait_seconds / 60:.0f} min away)"
                )

                reached = await sleep_until(next_session.start_time, device_id)
                if not reached:
                    break
                if shutdown_event.is_set():
                    break
            elif start_now:
                start_now = False  # Only skip wait for the FIRST session

            # ---- Pre-session: verify/switch account ----
            # Always verify for multi-account (don't trust current_account state)
            did_fresh_login = False
            if multi:
                switch_result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    switch_account_on_device,
                    device_ip,
                    device_id,
                    session_account,
                    current_account,
                )

                if switch_result.get("success"):
                    current_account = session_account
                    did_fresh_login = switch_result.get("did_login", False)
                else:
                    err = switch_result.get("error", "unknown")
                    print(f"  [{device_id}] Skipping session — switch failed: {err}")
                    DailyPlanGenerator.mark_session_status(
                        plan, next_session.label, "failed", mongo_db,
                        error=f"Account switch failed: {err}",
                    )
                    continue

            # Global guard: always stabilize IG context before any agent run.
            try:
                from eidola.tools.firerpa_tools import DeviceManager
                dm = DeviceManager.get(device_ip)
                DeviceManager.set_current(device_ip)
                if not _ensure_clean_instagram_state(dm, device_id):
                    err = "Instagram state is unstable before session start"
                    print(f"  [{device_id}] Skipping session — {err}")
                    DailyPlanGenerator.mark_session_status(
                        plan, next_session.label, "failed", mongo_db,
                        error=err,
                    )
                    continue
            except Exception as e:
                err = f"Cannot verify Instagram pre-session state: {e}"
                print(f"  [{device_id}] Skipping session — {err}")
                DailyPlanGenerator.mark_session_status(
                    plan, next_session.label, "failed", mongo_db,
                    error=err,
                )
                continue

            # ---- Pre-session: check for pending content to post ----
            posting_prepared = False
            try:
                from eidola.content.posting_scheduler import has_pending_post, prepare_device_for_posting
                if has_pending_post(session_account):
                    print(f"  [{device_id}] 📦 Content pending for @{session_account} — uploading to device...")
                    from eidola.tools.firerpa_tools import DeviceManager as _PostDM
                    _post_dm = _PostDM.get(device_ip)

                    def _do_upload():
                        return prepare_device_for_posting(_post_dm.device, session_account)

                    posting_info = await asyncio.get_event_loop().run_in_executor(None, _do_upload)
                    if posting_info:
                        posting_prepared = True
                        print(f"  [{device_id}] ✓ Content uploaded: {posting_info['posting_flow']} ({posting_info['media_count']} files)")
                    else:
                        print(f"  [{device_id}] ✗ Content upload failed, proceeding without posting")
            except Exception as e:
                print(f"  [{device_id}] Content check error (non-fatal): {e}")

            # ---- Start session ----
            acct_tag = f" @{session_account}" if multi else ""
            post_tag = " + POST" if posting_prepared else ""
            print(
                f"  [{device_id}] ▶ Starting: {next_session.label}{acct_tag}{post_tag} "
                f"({next_session.duration_minutes} min, mode={next_session.mode})"
            )

            DailyPlanGenerator.mark_session_status(
                plan, next_session.label, "running", mongo_db,
            )

            duration_secs = next_session.duration_minutes * 60
            # Don't skip login check if account was just logged in fresh
            use_skip_login = skip_login and not did_fresh_login
            extra = ["--task", SKIP_LOGIN_TASK] if use_skip_login else None
            # Load Instagram username (may differ from account_id)
            session_username = None
            try:
                from eidola.config import load_account_config
                _acfg = load_account_config(session_account)
                if _acfg:
                    session_username = _acfg.instagram.username
            except Exception:
                pass
            # Disconnect scheduler's gRPC channel before subprocess launch.
            # Safe now: set_current() is thread-local, so no cross-thread interference.
            # The subprocess creates its own channel; two channels = too_many_pings.
            try:
                from eidola.tools.firerpa_tools import DeviceManager as _DM
                _dm_inst = _DM._instances.get(device_ip)
                if _dm_inst:
                    _dm_inst.disconnect()
            except Exception:
                pass
            
            proc = run_agent_subprocess(
                device_id=device_id,
                account=session_account,
                mode=next_session.mode,
                duration=duration_secs,
                extra_args=extra,
                username=session_username,
            )

            timeout = duration_secs + PROCESS_TIMEOUT_BUFFER
            ret = await wait_for_process(proc, timeout=timeout)

            if shutdown_event.is_set():
                if proc.poll() is None:
                    proc.terminate()
                DailyPlanGenerator.mark_session_status(
                    plan, next_session.label, "skipped", mongo_db,
                    error="Shutdown requested",
                )
                break

            # Exit code interpretation:
            # 0 = clean exit, -15/3221225786 = SIGTERM/Ctrl+C, other = real failure
            sigterm_codes = {-15, -2, 3221225786, 3221225794}  # SIGTERM, SIGINT, Windows equivalents
            if ret == 0 or ret in sigterm_codes or shutdown_event.is_set():
                DailyPlanGenerator.mark_session_status(
                    plan, next_session.label, "completed", mongo_db,
                )
                if ret == 0:
                    print(f"  [{device_id}] ✓ Completed: {next_session.label}{acct_tag}")
                else:
                    print(f"  [{device_id}] ✓ Completed: {next_session.label}{acct_tag} (interrupted)")
            else:
                DailyPlanGenerator.mark_session_status(
                    plan, next_session.label, "failed", mongo_db,
                    error=f"Exit code {ret}",
                )
                print(f"  [{device_id}] ✗ Failed: {next_session.label}{acct_tag} (exit {ret})")

            # Small buffer between sessions (30-120 seconds of human hesitation)
            if not shutdown_event.is_set():
                import random
                pause = random.randint(30, 120)
                print(f"  [{device_id}] Pause {pause}s before next session...")
                await asyncio.sleep(pause)


# ============================================================================
# PROXY STARTUP
# ============================================================================

def start_all_proxies(device_ids: list[str]) -> dict[str, bool]:
    """Start proxies on all devices. Returns {device_id: success}."""
    username = os.environ.get("DECODO_USERNAME", "")
    password = os.environ.get("DECODO_PASSWORD", "")

    if not username or not password:
        print("ERROR: Missing DECODO_USERNAME or DECODO_PASSWORD in .env")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  STARTING PROXIES ON {len(device_ids)} DEVICE(S)")
    print(f"{'='*60}")

    results = []
    for did in device_ids:
        results.append(start_proxy_on_device(did, username, password))

    print_summary_table(results, mode="start")

    return {r["device_id"]: r["status"] == "OK" for r in results}


# ============================================================================
# MONGODB CONNECTION
# ============================================================================

def get_mongo_db(uri: str | None = None, db_name: str | None = None):
    """Get a pymongo Database instance.

    Args:
        uri: MongoDB URI. Defaults to MONGO_URI env var or localhost.
        db_name: Database name. Defaults to 'eidola'.

    Returns:
        (MongoClient, Database) tuple.
    """
    uri = uri or os.environ.get("MONGO_URI", "mongodb://localhost:27017")
    db_name = db_name or os.environ.get("MONGO_DB_NAME", "eidola")

    client = MongoClient(uri, serverSelectionTimeoutMS=5000)

    # Verify connection
    try:
        client.admin.command("ping")
        print(f"  MongoDB connected: {uri[:40]}... / {db_name}")
    except Exception as e:
        print(f"  WARNING: MongoDB connection failed: {e}")
        print(f"  Plans will be generated but NOT persisted.")

    return client, client[db_name]


# ============================================================================
# MAIN
# ============================================================================

async def async_main():
    parser = argparse.ArgumentParser(
        description="Fleet Scheduler — run agents on human-like daily plans"
    )
    parser.add_argument(
        "--device",
        help="Single device ID (e.g. phone_04). If omitted, runs all devices.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show generated plans without executing any sessions.",
    )
    parser.add_argument(
        "--regenerate-plan",
        action="store_true",
        help="Force-regenerate today's plan (discards existing).",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Skip proxy setup (assume proxies already running).",
    )
    parser.add_argument(
        "--skip-login",
        action="store_true",
        help="Assume devices already logged in. Pass task so agent does not attempt login.",
    )
    parser.add_argument(
        "--start-now",
        action="store_true",
        help="Skip waiting for the first session — start it immediately (for testing).",
    )
    args = parser.parse_args()

    device_ids = [args.device] if args.device else ALL_DEVICES

    print(f"\n{'='*60}")
    print(f"  FLEET SCHEDULER — {len(device_ids)} device(s)")
    print(f"  Date: {date.today().isoformat()}")
    print(f"  Dry run: {'yes' if args.dry_run else 'no'}")
    print(f"  Regenerate: {'yes' if args.regenerate_plan else 'no'}")
    print(f"  Skip login: {'yes' if args.skip_login else 'no'}")
    print(f"{'='*60}")

    # ---- Start proxies ----
    proxy_status: dict[str, bool] = {}
    if not args.no_proxy and not args.dry_run:
        proxy_status = start_all_proxies(device_ids)
        # Filter to devices with working proxies
        device_ids = [d for d in device_ids if proxy_status.get(d, False)]
        if not device_ids:
            print("ERROR: All proxies failed. Aborting.")
            sys.exit(1)
    else:
        if args.no_proxy:
            print("  Proxy setup skipped (--no-proxy)")

    # ---- Connect MongoDB ----
    mongo_client, mongo_db = get_mongo_db()
    DailyPlanGenerator.ensure_indexes(mongo_db)

    # ---- Build device → accounts mapping ----
    device_entries: list[tuple[str, list[str], str, ZoneInfo]] = []
    for did in device_ids:
        config = load_device_config(did)
        if not config:
            print(f"  WARNING: No config for {did} — skipping")
            continue
        accts = config.accounts if config.accounts else []
        if not accts:
            print(f"  WARNING: No accounts for {did} — skipping")
            continue

        tz_str = getattr(config.geo, "timezone", None)
        tz = ZoneInfo(tz_str) if tz_str else DEFAULT_TIMEZONE

        device_entries.append((did, accts, config.device_ip, tz))

    if not device_entries:
        print("ERROR: No valid device/account pairs found.")
        sys.exit(1)

    print(f"\n  Devices ready: {len(device_entries)}")
    for did, accts, ip, tz in device_entries:
        accts_str = ", ".join(accts)
        print(f"    {did:<12} [{accts_str}]  ip={ip}  tz={tz}")

    # ---- Plan generator ----
    generator = DailyPlanGenerator(
        config_path=str(_project_root / "config" / "schedule.yaml"),
    )

    # ---- Shutdown handling ----
    shutdown_event = asyncio.Event()
    loop = asyncio.get_event_loop()

    def _signal_handler():
        print(f"\n\n  Ctrl+C received — shutting down gracefully...")
        shutdown_event.set()

    # Signal handling: Unix uses add_signal_handler, Windows uses fallback
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler — Ctrl+C handled via KeyboardInterrupt
        pass

    # ---- Launch device loops (staggered to avoid thundering herd) ----
    tasks = []
    for i, (did, accts, ip, tz) in enumerate(device_entries):
        task = asyncio.create_task(
            _staggered_device_loop(
                stagger_delay=i * 3,  # 3 seconds between each device
                device_id=did,
                accounts=accts,
                device_ip=ip,
                plan_generator=generator,
                mongo_db=mongo_db,
                timezone=tz,
                shutdown_event=shutdown_event,
                dry_run=args.dry_run,
                force_regenerate=args.regenerate_plan,
                skip_login=args.skip_login,
                start_now=args.start_now,
            ),
            name=f"device-{did}",
        )
        tasks.append(task)

    print(f"\n  Launched {len(tasks)} device loop(s). Press Ctrl+C to stop.")
    print(f"\n  To watch agent activity live, open another terminal:")
    if sys.platform == "win32":
        print(r'     Get-Content sched_*.log -Tail 50 -Wait     (PowerShell 7+)')
        print(r'     type sched_*.log | more                    (CMD fallback)')
        print(f"  Full debug (single account):")
        print(r'     Get-Content <account>_*.log -Tail 100 -Wait')
        print(f"  (run from the logs\\ folder)")
    else:
        print(f"     tail -f logs/sched_*.log")
        print(f"  Full debug (single account):")
        print(f"     tail -f logs/<account>_*.log")
    print()

    # ---- Wait for all tasks ----
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # Print any exceptions that were swallowed
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                import traceback
                print(f"\n  ERROR in device loop {i}: {result}")
                traceback.print_exception(type(result), result, result.__traceback__)
    except Exception as e:
        print(f"  ERROR in gather: {e}")
    finally:
        # Cleanup
        mongo_client.close()
        print(f"\n{'='*60}")
        print(f"  FLEET SCHEDULER STOPPED")
        print(f"{'='*60}")


def main():
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass  # Already handled in async_main


if __name__ == "__main__":
    main()
