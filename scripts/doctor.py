#!/usr/bin/env python3
"""
Doctor Command - System diagnostics for Instagram automation.

Usage:
    python -m scripts.doctor [--device IP] [--verbose]
    
Checks:
- MongoDB connection
- FIRERPA device(s) connectivity
- Instagram app status
- Configuration validity
- Nurtured accounts loaded
"""

import argparse
import sys
import time
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class Colors:
    """ANSI color codes for terminal output."""
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


def ok(msg: str) -> str:
    return f"{Colors.GREEN}✓{Colors.RESET} {msg}"


def fail(msg: str) -> str:
    return f"{Colors.RED}✗{Colors.RESET} {msg}"


def warn(msg: str) -> str:
    return f"{Colors.YELLOW}⚠{Colors.RESET} {msg}"


def info(msg: str) -> str:
    return f"{Colors.BLUE}ℹ{Colors.RESET} {msg}"


def check_mongodb(verbose: bool = False) -> tuple[bool, str]:
    """Check MongoDB connection."""
    try:
        from eidola.memory.sync_memory import SyncAgentMemory
        
        start = time.monotonic()
        memory = SyncAgentMemory()
        memory.db.command("ping")
        latency = (time.monotonic() - start) * 1000
        
        # Count nurtured accounts
        nurtured_count = memory.db["nurtured_accounts"].count_documents({})
        
        details = f"latency: {latency:.0f}ms, nurtured accounts: {nurtured_count}"
        return True, details
        
    except ImportError as e:
        return False, f"import error: {e}"
    except Exception as e:
        return False, str(e)


def check_device(ip: str, verbose: bool = False) -> tuple[bool, dict]:
    """Check FIRERPA device connectivity."""
    try:
        from firerpa import Device
        
        start = time.monotonic()
        device = Device(ip)
        info = device.device_info()
        latency = (time.monotonic() - start) * 1000
        
        if info is None:
            return False, {"error": "device_info returned None"}
        
        result = {
            "latency_ms": round(latency),
            "screen": f"{device.displayWidth}x{device.displayHeight}",
            "sdk": getattr(device, 'sdkInt', 'unknown'),
        }
        
        # Check Instagram
        try:
            app_info = device.app_info("com.instagram.android")
            result["instagram"] = "installed" if app_info else "not found"
        except Exception:
            result["instagram"] = "check failed"
        
        return True, result
        
    except ImportError as e:
        return False, {"error": f"import error: {e}"}
    except Exception as e:
        return False, {"error": str(e)}


def check_config(verbose: bool = False) -> tuple[bool, str]:
    """Check configuration files."""
    config_dir = Path(__file__).parent.parent / "config"
    modes_dir = config_dir / "modes"
    
    issues = []
    
    # Check modes exist
    required_modes = ["feed_scroll.yaml", "active_engage.yaml"]
    for mode_file in required_modes:
        path = modes_dir / mode_file
        if not path.exists():
            issues.append(f"missing: {mode_file}")
        elif verbose:
            import yaml
            with open(path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
                temp = cfg.get("temperature", "not set")
                model = cfg.get("model", "not set")
                print(f"      {mode_file}: model={model}, temp={temp}")
    
    # Check .env
    env_file = Path(__file__).parent.parent / ".env"
    env_example = Path(__file__).parent.parent / ".env.example"
    
    if not env_file.exists() and env_example.exists():
        issues.append(".env not found (copy from .env.example)")
    
    if issues:
        return False, ", ".join(issues)
    return True, f"modes: {len(list(modes_dir.glob('*.yaml')))}"


def check_global_state(verbose: bool = False) -> tuple[bool, str]:
    """Check for global state issues in memory_tools.py."""
    memory_tools_path = (
        Path(__file__).parent.parent 
        / "src" / "eidola" / "tools" / "memory_tools.py"
    )
    
    if not memory_tools_path.exists():
        return False, "memory_tools.py not found"
    
    with open(memory_tools_path, encoding="utf-8") as f:
        content = f.read()
    
    # Check for global state variables
    globals_found = []
    for line in content.split("\n"):
        if line.strip().startswith("_memory:") or \
           line.strip().startswith("_current_account:") or \
           line.strip().startswith("_instagram_username:"):
            globals_found.append(line.strip().split(":")[0].strip())
    
    if globals_found:
        return False, f"global state: {', '.join(globals_found)} (blocks multi-account)"
    return True, "no blocking globals"


def check_scroll_tracking(verbose: bool = False) -> tuple[bool, str]:
    """Check if scroll loop tracking is implemented."""
    firerpa_tools_path = (
        Path(__file__).parent.parent 
        / "src" / "eidola" / "tools" / "firerpa_tools.py"
    )
    
    if not firerpa_tools_path.exists():
        return False, "firerpa_tools.py not found"
    
    with open(firerpa_tools_path, encoding="utf-8") as f:
        content = f.read()
    
    if "_scroll_tracker" in content and "_increment_scroll_tracker" in content:
        return True, "scroll loop detection enabled"
    return False, "scroll loop detection NOT implemented"


def run_doctor(device_ips: list[str] = None, verbose: bool = False):
    """Run all diagnostic checks."""
    print(f"\n{Colors.BOLD}🩺 Eidola Doctor{Colors.RESET}")
    print("=" * 50)
    
    all_ok = True
    
    # MongoDB
    print(f"\n{Colors.BOLD}MongoDB:{Colors.RESET}")
    success, details = check_mongodb(verbose)
    if success:
        print(f"  {ok(f'connected ({details})')}")
    else:
        print(f"  {fail(f'connection failed: {details}')}")
        all_ok = False
    
    # Devices
    if device_ips:
        print(f"\n{Colors.BOLD}FIRERPA Devices:{Colors.RESET}")
        for ip in device_ips:
            success, result = check_device(ip, verbose)
            if success:
                screen = result.get('screen', '?')
                latency = result.get('latency_ms', '?')
                instagram = result.get('instagram', '?')
                print(f"  {ok(f'{ip} - {screen}, {latency}ms, IG: {instagram}')}")
            else:
                error_msg = result.get("error", "unknown error")
                print(f"  {fail(f'{ip} - {error_msg}')}")
                all_ok = False
    else:
        print(f"\n{Colors.BOLD}FIRERPA Devices:{Colors.RESET}")
        print(f"  {info('no device IPs provided (use --device IP)')}")
    
    # Config
    print(f"\n{Colors.BOLD}Configuration:{Colors.RESET}")
    success, details = check_config(verbose)
    if success:
        print(f"  {ok(details)}")
    else:
        print(f"  {fail(details)}")
        all_ok = False
    
    # Global state check
    print(f"\n{Colors.BOLD}Code Quality:{Colors.RESET}")
    success, details = check_global_state(verbose)
    if success:
        print(f"  {ok(details)}")
    else:
        print(f"  {warn(details)}")
    
    # Scroll tracking
    success, details = check_scroll_tracking(verbose)
    if success:
        print(f"  {ok(details)}")
    else:
        print(f"  {warn(details)}")
    
    # Summary
    print("\n" + "=" * 50)
    if all_ok:
        print(f"{Colors.GREEN}{Colors.BOLD}All critical checks passed!{Colors.RESET}")
    else:
        print(f"{Colors.RED}{Colors.BOLD}Some checks failed - fix before running agent{Colors.RESET}")
    print()
    
    return 0 if all_ok else 1


def main():
    parser = argparse.ArgumentParser(
        description="Diagnose Eidola system health",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/doctor.py
  python scripts/doctor.py --device 192.168.1.100
  python scripts/doctor.py --device 192.168.1.100 --device 192.168.1.101 --verbose
        """,
    )
    parser.add_argument(
        "--device", "-d",
        action="append",
        dest="devices",
        help="FIRERPA device IP (can specify multiple)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed output",
    )
    
    args = parser.parse_args()
    
    sys.exit(run_doctor(args.devices, args.verbose))


if __name__ == "__main__":
    main()
