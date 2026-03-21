#!/usr/bin/env python3
"""
Start redsocks transparent proxy on ALL fleet devices (phone_01 through phone_09).

Uses the same proven redsocks + iptables logic from test_redsocks_proxy.py.

Usage:
    python scripts/fleet_proxy_start.py                    # all 9 devices
    python scripts/fleet_proxy_start.py --device phone_04  # single device
    python scripts/fleet_proxy_start.py --stop             # stop all proxies
    python scripts/fleet_proxy_start.py --status           # check all statuses
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

_project_root = Path(__file__).parent.parent
load_dotenv(_project_root / ".env")
sys.path.insert(0, str(_project_root / "src"))

from lamda.client import Device
from eidola.config import load_device_config

# =============================================================================
# CONSTANTS (same as test_redsocks_proxy.py — confirmed working)
# =============================================================================
REDSOCKS_PORT = 31338
REDSOCKS_BIN = "/data/server/bin/redsocks"
REDSOCKS_CONF = "/data/local/tmp/redsocks.conf"
REDSOCKS_LOG = "/data/local/tmp/redsocks.log"

ALL_DEVICES = [f"phone_{i:02d}" for i in range(1, 10)] + ["phone_10"]

APPS_TO_STOP = [
    "com.instagram.android",
    "com.android.chrome",
    "com.android.browser",
    "mark.via",
    "org.chromium.webview_shell",
    "com.sec.android.app.sbrowser",
]


# =============================================================================
# SHELL HELPER (same as test_redsocks_proxy.py)
# =============================================================================
def shell(device: Device, cmd: str) -> str:
    """Execute shell command on device and return stdout."""
    result = device.execute_script(cmd)
    if result is None:
        return ""
    output = None
    if hasattr(result, "stdout") and result.stdout:
        output = result.stdout
    elif hasattr(result, "output") and result.output:
        output = result.output
    else:
        output = result
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    if not isinstance(output, str):
        output = str(output)
    return output.strip()


# =============================================================================
# PROXY FUNCTIONS (extracted from test_redsocks_proxy.py — proven working)
# =============================================================================
def resolve_host(device: Device, hostname: str) -> str:
    """Resolve proxy hostname to IP address on device."""
    raw = shell(device, f"ping -c1 -W3 {hostname} 2>&1 | head -1")
    if "(" in raw and ")" in raw:
        ip = raw[raw.index("(") + 1 : raw.index(")")]
        if "." in ip and len(ip) <= 15:
            return ip
    # Fallback for known hosts
    known = {"isp.decodo.com": "185.111.111.38", "gate.decodo.com": "185.111.111.38"}
    if hostname in known:
        return known[hostname]
    return ""


def write_redsocks_config(
    device: Device, proxy_ip: str, proxy_port: int, username: str, password: str
) -> bool:
    """Write redsocks config file on device (confirmed working format)."""
    config = (
        f"base {{\n"
        f'    log_debug = off;\n'
        f'    log_info = on;\n'
        f'    log = "file:{REDSOCKS_LOG}";\n'
        f'    daemon = on;\n'
        f'    redirector = iptables;\n'
        f"}}\n\n"
        f"redsocks {{\n"
        f'    bind = "127.0.0.1:{REDSOCKS_PORT}";\n'
        f'    relay = "{proxy_ip}:{proxy_port}";\n'
        f'    type = http-connect;\n'
        f'    login = "{username}";\n'
        f'    password = "{password}";\n'
        f"}}\n"
    )

    shell(device, f"rm -f {REDSOCKS_CONF}")
    shell(device, f"cat > {REDSOCKS_CONF} << 'REDSOCKS_EOF'\n{config}REDSOCKS_EOF")
    shell(device, f"chmod 600 {REDSOCKS_CONF}")

    written = shell(device, f"cat {REDSOCKS_CONF}")
    return "redsocks" in written


def start_redsocks_daemon(device: Device) -> bool:
    """Start redsocks daemon on device."""
    shell(device, "killall redsocks 2>/dev/null || true")
    time.sleep(0.5)

    # Test config
    test = shell(device, f"{REDSOCKS_BIN} -t -c {REDSOCKS_CONF} 2>&1")
    if test and ("error" in test.lower() or "unknown" in test.lower()):
        return False

    # Start daemon
    result = shell(device, f"{REDSOCKS_BIN} -c {REDSOCKS_CONF} 2>&1")
    if result and "error" in result.lower():
        return False

    time.sleep(1)
    pid = shell(device, "pidof redsocks 2>/dev/null")
    return bool(pid and pid.split()[0].isdigit())


def setup_iptables(device: Device, proxy_ip: str) -> bool:
    """Set up iptables: TCP redirect + DNS DNAT + UDP drop."""
    # === TCP REDIRECT ===
    shell(device, "iptables -t nat -N REDSOCKS 2>/dev/null || true")

    for net in [
        "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
        "224.0.0.0/4", "240.0.0.0/4",
    ]:
        shell(device, f"iptables -t nat -A REDSOCKS -d {net} -j RETURN")

    shell(device, f"iptables -t nat -A REDSOCKS -d {proxy_ip} -j RETURN")
    shell(device, f"iptables -t nat -A REDSOCKS -p tcp -j REDIRECT --to-ports {REDSOCKS_PORT}")
    shell(device, "iptables -t nat -A OUTPUT -j REDSOCKS")

    # === DNS LEAK PREVENTION (DNAT to Google DNS) ===
    shell(device, "iptables -t nat -A OUTPUT -p udp --dport 53 -j DNAT --to-destination 8.8.8.8:53")
    shell(device, "iptables -t nat -A OUTPUT -p tcp --dport 53 -j DNAT --to-destination 8.8.8.8:53")
    shell(device, "setprop net.dns1 8.8.8.8")
    shell(device, "setprop net.dns2 8.8.4.4")

    # === UDP DROP (blocks QUIC/WebRTC) ===
    shell(device, "iptables -A OUTPUT -p udp -d 10.0.0.0/8 -j ACCEPT")
    shell(device, "iptables -A OUTPUT -p udp -d 192.168.0.0/16 -j ACCEPT")
    shell(device, "iptables -A OUTPUT -p udp -d 127.0.0.0/8 -j ACCEPT")
    shell(device, "iptables -A OUTPUT -p udp -d 8.8.8.8 --dport 53 -j ACCEPT")
    shell(device, "iptables -A OUTPUT -p udp -d 8.8.4.4 --dport 53 -j ACCEPT")
    shell(device, "iptables -A OUTPUT -p udp -j DROP")

    # Verify
    rules = shell(device, "iptables -t nat -L REDSOCKS -n 2>/dev/null")
    return "REDIRECT" in rules


def remove_iptables(device: Device):
    """Clean up all iptables rules."""
    shell(device, "iptables -t nat -D OUTPUT -j REDSOCKS 2>/dev/null || true")
    shell(device, "iptables -t nat -F REDSOCKS 2>/dev/null || true")
    shell(device, "iptables -t nat -X REDSOCKS 2>/dev/null || true")
    shell(device, "iptables -t nat -D OUTPUT -p udp --dport 53 -j DNAT --to-destination 8.8.8.8:53 2>/dev/null || true")
    shell(device, "iptables -t nat -D OUTPUT -p tcp --dport 53 -j DNAT --to-destination 8.8.8.8:53 2>/dev/null || true")
    shell(device, "iptables -D OUTPUT -p udp -j DROP 2>/dev/null || true")
    shell(device, "iptables -D OUTPUT -p udp -d 8.8.4.4 --dport 53 -j ACCEPT 2>/dev/null || true")
    shell(device, "iptables -D OUTPUT -p udp -d 8.8.8.8 --dport 53 -j ACCEPT 2>/dev/null || true")
    shell(device, "iptables -D OUTPUT -p udp -d 127.0.0.0/8 -j ACCEPT 2>/dev/null || true")
    shell(device, "iptables -D OUTPUT -p udp -d 192.168.0.0/16 -j ACCEPT 2>/dev/null || true")
    shell(device, "iptables -D OUTPUT -p udp -d 10.0.0.0/8 -j ACCEPT 2>/dev/null || true")


def stop_proxy(device: Device):
    """Stop redsocks and clean iptables on a device."""
    shell(device, "killall redsocks 2>/dev/null || true")
    shell(device, "killall dnsmasq 2>/dev/null || true")
    remove_iptables(device)
    shell(device, f"rm -f {REDSOCKS_CONF} {REDSOCKS_LOG} 2>/dev/null || true")
    shell(device, "sysctl -w net.ipv6.conf.all.disable_ipv6=0 2>/dev/null || true")
    try:
        device.stop_gproxy()
    except Exception:
        pass


def check_ip(device: Device) -> dict:
    """Check the device's external IP via curl."""
    raw = shell(device, "curl -s --connect-timeout 10 http://ip-api.com/json/ 2>/dev/null")
    try:
        data = json.loads(raw)
        return {
            "success": True,
            "ip": data.get("query", "?"),
            "country": data.get("countryCode", "?"),
            "isp": data.get("isp", "?"),
        }
    except Exception:
        return {"success": False, "raw": raw[:150]}


def force_stop_apps(device: Device):
    """Force-stop apps so they reconnect through proxy."""
    for app in APPS_TO_STOP:
        shell(device, f"am force-stop {app} 2>/dev/null || true")


# =============================================================================
# MAIN FLEET PROXY LOGIC
# =============================================================================
def start_proxy_on_device(device_id: str, username: str, password: str) -> dict:
    """Start transparent proxy on a single device.
    
    Returns dict with result info for summary table.
    """
    result = {
        "device_id": device_id,
        "ip": "?",
        "proxy_port": "?",
        "status": "FAIL",
        "external_ip": "?",
        "country": "?",
        "error": None,
    }

    # Load config
    config = load_device_config(device_id)
    if not config:
        result["error"] = "Config not found"
        return result

    result["ip"] = config.device_ip
    result["proxy_port"] = config.proxy.port
    account = config.accounts[0] if config.accounts else "none"

    print(f"\n{'='*60}")
    print(f"  {device_id} | {config.device_ip} | port {config.proxy.port} | {account}")
    print(f"{'='*60}")

    try:
        # Connect
        print(f"  Connecting to {config.device_ip}...")
        device = Device(config.device_ip)

        # Clean previous state
        print("  Cleaning previous proxy state...")
        stop_proxy(device)
        time.sleep(1)

        # Disable IPv6
        shell(device, "sysctl -w net.ipv6.conf.all.disable_ipv6=1 2>/dev/null || true")
        shell(device, "sysctl -w net.ipv6.conf.wlan0.disable_ipv6=1 2>/dev/null || true")

        # Set timezone + locale to match geo
        tz = getattr(config.geo, 'timezone', None) or "America/New_York"
        # Use service call for more reliable timezone change on Android 10
        shell(device, f"setprop persist.sys.timezone '{tz}'")
        shell(device, f"service call alarm 3 s16 '{tz}'")  # AlarmManager.setTimeZone()
        shell(device, f"settings put global time_zone '{tz}' 2>/dev/null || true")
        shell(device, "am broadcast -a android.intent.action.TIMEZONE_CHANGED 2>/dev/null || true")
        shell(device, "setprop persist.sys.locale en-US 2>/dev/null || true")
        shell(device, "settings put system time_12_24 12 2>/dev/null || true")
        # Verify
        actual_tz = shell(device, "getprop persist.sys.timezone")
        if tz in actual_tz:
            print(f"  Timezone: {tz} ✓")
        else:
            print(f"  Timezone: FAILED (wanted {tz}, got {actual_tz})")

        # Resolve proxy host
        proxy_host = config.proxy.host
        print(f"  Resolving {proxy_host}...")
        proxy_ip = resolve_host(device, proxy_host)
        if not proxy_ip:
            result["error"] = f"Cannot resolve {proxy_host}"
            print(f"  ERROR: {result['error']}")
            return result
        print(f"  Proxy IP: {proxy_ip}")

        # Write config
        print("  Writing redsocks config...")
        if not write_redsocks_config(device, proxy_ip, config.proxy.port, username, password):
            result["error"] = "Config write failed"
            print(f"  ERROR: {result['error']}")
            return result

        # Start redsocks
        print("  Starting redsocks daemon...")
        if not start_redsocks_daemon(device):
            result["error"] = "redsocks failed to start"
            print(f"  ERROR: {result['error']}")
            return result

        pid = shell(device, "pidof redsocks 2>/dev/null")
        print(f"  redsocks running (PID: {pid})")

        # Setup iptables
        print("  Setting up iptables (TCP + DNS + UDP drop)...")
        if not setup_iptables(device, proxy_ip):
            result["error"] = "iptables setup failed"
            stop_proxy(device)
            print(f"  ERROR: {result['error']}")
            return result

        # Force-stop apps
        print("  Force-stopping apps...")
        force_stop_apps(device)
        time.sleep(2)

        # Verify with curl
        print("  Verifying with curl...")
        ip_check = check_ip(device)
        if ip_check["success"]:
            result["external_ip"] = ip_check["ip"]
            result["country"] = ip_check["country"]
            result["status"] = "OK"
            print(f"  IP: {ip_check['ip']} ({ip_check['country']}) — {ip_check.get('isp', '?')}")
        else:
            result["status"] = "CURL_FAIL"
            result["error"] = "curl verification failed"
            print(f"  WARNING: curl failed but proxy may still work: {ip_check.get('raw', '')[:80]}")

    except Exception as e:
        result["error"] = str(e)[:80]
        print(f"  EXCEPTION: {e}")

    return result


def stop_proxy_on_device(device_id: str) -> dict:
    """Stop proxy on a single device."""
    result = {"device_id": device_id, "status": "FAIL", "error": None}

    config = load_device_config(device_id)
    if not config:
        result["error"] = "Config not found"
        return result

    try:
        device = Device(config.device_ip)
        stop_proxy(device)
        result["status"] = "STOPPED"
        print(f"  {device_id}: proxy stopped")
    except Exception as e:
        result["error"] = str(e)[:80]
        print(f"  {device_id}: error — {e}")

    return result


def check_status_on_device(device_id: str) -> dict:
    """Check proxy status on a single device."""
    result = {
        "device_id": device_id,
        "ip": "?",
        "redsocks": "?",
        "iptables": "?",
        "external_ip": "?",
        "country": "?",
    }

    config = load_device_config(device_id)
    if not config:
        result["redsocks"] = "NO CONFIG"
        return result

    result["ip"] = config.device_ip

    try:
        device = Device(config.device_ip)

        pid = shell(device, "pidof redsocks 2>/dev/null || echo 'none'")
        result["redsocks"] = pid if pid != "none" else "not running"

        rules = shell(device, "iptables -t nat -L REDSOCKS -n 2>/dev/null || echo 'no chain'")
        result["iptables"] = "active" if "REDIRECT" in rules else "inactive"

        ip_check = check_ip(device)
        if ip_check["success"]:
            result["external_ip"] = ip_check["ip"]
            result["country"] = ip_check["country"]

    except Exception as e:
        result["redsocks"] = f"ERR: {str(e)[:40]}"

    return result


def print_summary_table(results: list[dict], mode: str = "start"):
    """Print a formatted summary table."""
    print(f"\n{'='*80}")
    print(f"  FLEET PROXY SUMMARY ({mode.upper()})")
    print(f"{'='*80}")

    if mode == "start":
        print(f"  {'Device':<12} {'IP':<16} {'Port':<7} {'Status':<12} {'External IP':<18} {'CC':<4}")
        print(f"  {'-'*12} {'-'*16} {'-'*7} {'-'*12} {'-'*18} {'-'*4}")
        for r in results:
            status_icon = "✅" if r["status"] == "OK" else "⚠️ " if r["status"] == "CURL_FAIL" else "❌"
            print(
                f"  {r['device_id']:<12} {r['ip']:<16} {str(r['proxy_port']):<7} "
                f"{status_icon} {r['status']:<9} {r['external_ip']:<18} {r['country']:<4}"
            )
            if r.get("error"):
                print(f"  {'':>12} └─ {r['error']}")
    elif mode == "status":
        print(f"  {'Device':<12} {'IP':<16} {'Redsocks':<14} {'IPTables':<10} {'External IP':<18} {'CC':<4}")
        print(f"  {'-'*12} {'-'*16} {'-'*14} {'-'*10} {'-'*18} {'-'*4}")
        for r in results:
            print(
                f"  {r['device_id']:<12} {r['ip']:<16} {r['redsocks']:<14} "
                f"{r['iptables']:<10} {r['external_ip']:<18} {r['country']:<4}"
            )
    elif mode == "stop":
        print(f"  {'Device':<12} {'Status':<12}")
        print(f"  {'-'*12} {'-'*12}")
        for r in results:
            status_icon = "✅" if r["status"] == "STOPPED" else "❌"
            print(f"  {r['device_id']:<12} {status_icon} {r['status']}")

    ok = sum(1 for r in results if r.get("status") in ("OK", "STOPPED"))
    total = len(results)
    print(f"\n  Result: {ok}/{total} devices successful")
    print(f"{'='*80}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Start/stop/check redsocks proxy on fleet devices"
    )
    parser.add_argument(
        "--device",
        help="Single device ID (e.g. phone_04). If omitted, operates on all 9 devices.",
    )
    parser.add_argument("--stop", action="store_true", help="Stop proxies on all devices")
    parser.add_argument("--status", action="store_true", help="Check proxy status on all devices")
    args = parser.parse_args()

    devices = [args.device] if args.device else ALL_DEVICES

    # --- STOP ---
    if args.stop:
        print(f"\n🛑 Stopping proxy on {len(devices)} device(s)...")
        results = []
        for d in devices:
            results.append(stop_proxy_on_device(d))
        print_summary_table(results, mode="stop")
        return

    # --- STATUS ---
    if args.status:
        print(f"\n📊 Checking proxy status on {len(devices)} device(s)...")
        results = []
        for d in devices:
            results.append(check_status_on_device(d))
        print_summary_table(results, mode="status")
        return

    # --- START ---
    # Get proxy credentials from .env
    username = os.environ.get("DECODO_USERNAME", "")
    password = os.environ.get("DECODO_PASSWORD", "")
    if not username or not password:
        print("ERROR: Missing DECODO_USERNAME or DECODO_PASSWORD in .env")
        sys.exit(1)

    print(f"\n🚀 Starting proxy on {len(devices)} device(s)...")
    print(f"   Credentials: {username[:4]}***")

    results = []
    for d in devices:
        results.append(start_proxy_on_device(d, username, password))

    print_summary_table(results, mode="start")

    # Return exit code based on results
    ok = sum(1 for r in results if r["status"] == "OK")
    if ok == 0:
        sys.exit(1)
    elif ok < len(results):
        sys.exit(2)  # Partial success


if __name__ == "__main__":
    main()
