#!/usr/bin/env python3
"""
Fleet Launch — start proxies and run agents on all devices in parallel.

Usage:
    python scripts/fleet_launch.py                    # all devices
    python scripts/fleet_launch.py --device phone_04  # single device
    python scripts/fleet_launch.py --proxy-only       # only start proxies
    python scripts/fleet_launch.py --no-proxy         # skip proxy, just agents
    python scripts/fleet_launch.py --mode warmup      # override mode for all

Workflow:
    1. Start redsocks proxy on all target devices (fleet_proxy_start logic)
    2. For each device in parallel:
       a. If device needs login → run agent with --mode login
       b. After login (or if already logged in) → run agent with warmup mode
    3. Handle Ctrl+C gracefully (stop all agents)
    4. Print status table
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_project_root = Path(__file__).parent.parent
load_dotenv(_project_root / ".env")
sys.path.insert(0, str(_project_root / "src"))

from eidola.config import load_device_config

# Import fleet_proxy_start functions
sys.path.insert(0, str(_project_root / "scripts"))
from fleet_proxy_start import (
    start_proxy_on_device,
    stop_proxy_on_device,
    print_summary_table,
    ALL_DEVICES,
)


# =============================================================================
# DEVICE REGISTRY — accounts and login state
# =============================================================================
# Devices that are already logged in (no login step needed)
# All devices get --mode login (agent auto-detects if already logged in via detect_screen)
ALREADY_LOGGED_IN = set()  # Empty = all devices attempt login (safe: agent skips if already logged in)


@dataclass
class DeviceState:
    """Track state for a single device during fleet launch."""
    device_id: str
    ip: str = ""
    proxy_port: int = 0
    account: str = ""
    proxy_status: str = "pending"      # pending | ok | fail
    login_status: str = "pending"      # pending | skipped | running | ok | fail
    agent_status: str = "pending"      # pending | running | ok | fail | stopped
    agent_process: subprocess.Popen | None = None
    error: str = ""
    pid: int = 0


# =============================================================================
# AGENT RUNNER
# =============================================================================
def run_agent_subprocess(
    device_id: str,
    account: str,
    mode: str = "warmup",
    extra_args: list[str] | None = None,
) -> subprocess.Popen:
    """Launch an agent as a subprocess.
    
    Runs: python run.py --device-id <device_id> --account <account> --mode <mode>
    """
    cmd = [
        sys.executable, str(_project_root / "run.py"),
        "--device-id", device_id,
        "--account", account,
        "--mode", mode,
        "--no-isolation",  # Proxy already set up by fleet_proxy_start
    ]
    if extra_args:
        cmd.extend(extra_args)

    # Start process with stdout/stderr going to log file
    log_dir = _project_root / "logs"
    log_dir.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"fleet_{device_id}_{account}_{timestamp}.log"

    print(f"  [{device_id}] Starting agent: {account} mode={mode}")
    print(f"  [{device_id}] Log: {log_file}")

    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=subprocess.STDOUT,
            cwd=str(_project_root),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    return proc


def run_device_pipeline(
    state: DeviceState,
    mode: str,
    skip_login: bool = False,
):
    """Run the full pipeline for a single device (login if needed, then warmup).
    
    Called from a thread for parallel execution.
    """
    device_id = state.device_id
    account = state.account

    if not account:
        state.agent_status = "fail"
        state.error = "No account assigned"
        print(f"  [{device_id}] SKIP — no account assigned")
        return

    # Step 1: Login if needed
    needs_login = device_id not in ALREADY_LOGGED_IN and not skip_login

    if needs_login:
        print(f"\n  [{device_id}] LOGIN PHASE — {account}")
        state.login_status = "running"

        login_proc = None
        try:
            login_proc = run_agent_subprocess(device_id, account, mode="login")
            state.agent_process = login_proc
            state.pid = login_proc.pid

            # Wait for login to complete (max 3 minutes)
            # Login should exit quickly after detecting feed or completing login
            try:
                ret = login_proc.wait(timeout=180)  # 3 minutes timeout
                if ret == 0:
                    state.login_status = "ok"
                    print(f"  [{device_id}] Login complete")
                else:
                    state.login_status = "fail"
                    state.error = f"Login exit code: {ret}"
                    print(f"  [{device_id}] Login FAILED (exit {ret}) — continuing to warmup anyway")
                    # Continue to warmup even if login failed
            except subprocess.TimeoutExpired:
                # Login process didn't exit in time (likely still running in login mode)
                print(f"  [{device_id}] Login TIMEOUT (3min) — killing login process")
                if login_proc.poll() is None:  # Process still running
                    login_proc.kill()
                    try:
                        login_proc.wait(timeout=5)  # Wait for kill to complete
                    except subprocess.TimeoutExpired:
                        pass  # Process already dead
                state.login_status = "timeout"
                state.error = "Login timeout (3min)"
                print(f"  [{device_id}] Login process killed — starting warmup")
                # Continue to warmup even after timeout
        except Exception as e:
            state.login_status = "fail"
            state.error = str(e)[:60]
            print(f"  [{device_id}] Login ERROR: {e} — continuing to warmup anyway")
            # Continue to warmup even if login had an error
        finally:
            # Clear login process reference so we can start warmup process
            # Ensure process is terminated if still running
            if login_proc and login_proc.poll() is None:
                try:
                    login_proc.kill()
                    login_proc.wait(timeout=2)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    pass
            state.agent_process = None
            state.pid = 0
    else:
        state.login_status = "skipped"
        if device_id in ALREADY_LOGGED_IN:
            print(f"  [{device_id}] Login skipped (already logged in)")
        else:
            print(f"  [{device_id}] Login skipped (--skip-login)")

    # Step 2: Run agent (warmup or specified mode)
    # This starts a NEW subprocess with --mode warmup (or specified mode)
    print(f"\n  [{device_id}] WARMUP PHASE — {account} mode={mode}")
    state.agent_status = "running"

    try:
        proc = run_agent_subprocess(device_id, account, mode=mode)
        state.agent_process = proc
        state.pid = proc.pid
        state.agent_status = "running"
        print(f"  [{device_id}] Warmup process started (PID {proc.pid})")

        # Don't wait — let it run in background
        # The main thread will monitor all processes

    except Exception as e:
        state.agent_status = "fail"
        state.error = str(e)[:60]
        print(f"  [{device_id}] Agent ERROR: {e}")


# =============================================================================
# STATUS TABLE
# =============================================================================
def print_fleet_status(states: list[DeviceState]):
    """Print a formatted status table for all devices."""
    print(f"\n{'='*90}")
    print(f"  FLEET STATUS")
    print(f"{'='*90}")
    print(
        f"  {'Device':<12} {'IP':<16} {'Account':<24} "
        f"{'Proxy':<8} {'Login':<10} {'Agent':<10} {'PID':<8}"
    )
    print(
        f"  {'-'*12} {'-'*16} {'-'*24} "
        f"{'-'*8} {'-'*10} {'-'*10} {'-'*8}"
    )

    status_icons = {
        "ok": "✅",
        "running": "🔄",
        "pending": "⏳",
        "fail": "❌",
        "skipped": "⏭️ ",
        "stopped": "🛑",
    }

    for s in states:
        proxy_icon = status_icons.get(s.proxy_status, "?")
        login_icon = status_icons.get(s.login_status, "?")
        agent_icon = status_icons.get(s.agent_status, "?")

        print(
            f"  {s.device_id:<12} {s.ip:<16} {s.account:<24} "
            f"{proxy_icon:<8} {login_icon:<10} {agent_icon:<10} {s.pid or '-':<8}"
        )
        if s.error:
            print(f"  {'':>12} └─ {s.error}")

    running = sum(1 for s in states if s.agent_status == "running")
    ok = sum(1 for s in states if s.agent_status == "ok")
    failed = sum(1 for s in states if s.agent_status == "fail")
    print(f"\n  Running: {running}  |  OK: {ok}  |  Failed: {failed}")
    print(f"{'='*90}\n")


# =============================================================================
# MAIN
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="Fleet Launch — proxy + agents on all devices")
    parser.add_argument(
        "--device",
        help="Single device ID (e.g. phone_04). If omitted, runs all 9 devices.",
    )
    parser.add_argument(
        "--proxy-only",
        action="store_true",
        help="Only start proxies, don't launch agents.",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Skip proxy setup, only launch agents.",
    )
    parser.add_argument(
        "--skip-login",
        action="store_true",
        help="Skip login step for all devices.",
    )
    parser.add_argument(
        "--mode",
        default="warmup",
        choices=["warmup", "active_engage", "feed_scroll", "nurture_accounts"],
        help="Agent mode after login (default: warmup).",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Seconds between status checks (default: 30).",
    )
    args = parser.parse_args()

    device_ids = [args.device] if args.device else ALL_DEVICES

    print(f"\n🚀 FLEET LAUNCH — {len(device_ids)} device(s)")
    print(f"   Mode: {args.mode}")
    print(f"   Proxy: {'skip' if args.no_proxy else 'yes'}")
    print(f"   Login: {'skip' if args.skip_login else 'auto-detect'}")
    print()

    # Build device states
    states: list[DeviceState] = []
    for did in device_ids:
        config = load_device_config(did)
        if not config:
            print(f"  WARNING: Config not found for {did} — skipping")
            continue
        account = config.accounts[0] if config.accounts else ""
        states.append(DeviceState(
            device_id=did,
            ip=config.device_ip,
            proxy_port=config.proxy.port,
            account=account,
        ))

    if not states:
        print("ERROR: No valid devices found.")
        sys.exit(1)

    # =========================================================================
    # STEP 1: START PROXIES
    # =========================================================================
    if not args.no_proxy:
        print(f"\n{'='*60}")
        print("  STEP 1: STARTING PROXIES")
        print(f"{'='*60}")

        username = os.environ.get("DECODO_USERNAME", "")
        password = os.environ.get("DECODO_PASSWORD", "")
        if not username or not password:
            print("ERROR: Missing DECODO_USERNAME or DECODO_PASSWORD in .env")
            sys.exit(1)

        proxy_results = []
        for state in states:
            result = start_proxy_on_device(state.device_id, username, password)
            proxy_results.append(result)
            state.proxy_status = "ok" if result["status"] == "OK" else "fail"
            if result.get("error"):
                state.error = result["error"]

        print_summary_table(proxy_results, mode="start")

        # Check if any proxies failed
        proxy_ok = sum(1 for s in states if s.proxy_status == "ok")
        if proxy_ok == 0:
            print("ERROR: All proxies failed. Aborting.")
            sys.exit(1)

        if args.proxy_only:
            print("--proxy-only: done.")
            return
    else:
        for s in states:
            s.proxy_status = "skipped"

    # =========================================================================
    # STEP 2: LAUNCH AGENTS IN PARALLEL
    # =========================================================================
    print(f"\n{'='*60}")
    print("  STEP 2: LAUNCHING AGENTS")
    print(f"{'='*60}")

    # Filter to devices with OK proxy (or skipped proxy)
    launchable = [s for s in states if s.proxy_status in ("ok", "skipped")]
    if not launchable:
        print("ERROR: No devices ready for agent launch.")
        sys.exit(1)

    # Launch each device pipeline in a thread
    threads: list[threading.Thread] = []
    for state in launchable:
        t = threading.Thread(
            target=run_device_pipeline,
            args=(state, args.mode, args.skip_login),
            name=f"pipeline-{state.device_id}",
            daemon=True,
        )
        threads.append(t)
        t.start()
        time.sleep(1)  # Stagger starts slightly

    # Wait for all pipeline threads to finish login + start agents
    for t in threads:
        t.join(timeout=360)  # 6 minute timeout for login phase

    print_fleet_status(states)

    # =========================================================================
    # STEP 3: MONITOR RUNNING AGENTS
    # =========================================================================
    running_procs = [s for s in states if s.agent_process and s.agent_status == "running"]
    if not running_procs:
        print("No agents running. Done.")
        return

    print(f"\n📡 Monitoring {len(running_procs)} running agent(s)...")
    print(f"   Press Ctrl+C to stop all agents.\n")

    # Graceful shutdown handler
    shutdown_event = threading.Event()

    def signal_handler(sig, frame):
        print(f"\n\n🛑 Ctrl+C received — stopping all agents...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        while not shutdown_event.is_set():
            # Check all running processes
            all_done = True
            for state in states:
                if state.agent_process and state.agent_status == "running":
                    ret = state.agent_process.poll()
                    if ret is not None:
                        state.agent_status = "ok" if ret == 0 else "fail"
                        if ret != 0:
                            state.error = f"Agent exit code: {ret}"
                        print(f"  [{state.device_id}] Agent finished (exit {ret})")
                    else:
                        all_done = False

            if all_done:
                print("\nAll agents finished.")
                break

            # Sleep with interrupt check
            shutdown_event.wait(timeout=args.poll_interval)

    except KeyboardInterrupt:
        shutdown_event.set()

    # Cleanup: kill all remaining processes
    if shutdown_event.is_set():
        for state in states:
            if state.agent_process and state.agent_process.poll() is None:
                print(f"  [{state.device_id}] Killing agent (PID {state.agent_process.pid})...")
                try:
                    state.agent_process.terminate()
                    state.agent_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    state.agent_process.kill()
                state.agent_status = "stopped"

    # Final status
    print_fleet_status(states)


if __name__ == "__main__":
    main()
