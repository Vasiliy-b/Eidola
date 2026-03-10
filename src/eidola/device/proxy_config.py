"""
Proxy configuration module for device isolation.

Uses iptables + redsocks to transparently route all device TCP traffic
through an HTTP CONNECT proxy (Decodo/Smartproxy). Also prevents DNS
and UDP leaks via dnstc and iptables DROP rules.
"""

import logging
import os
import time
from typing import Any

from lamda.client import Device

from ..config import ProxyConfig

logger = logging.getLogger("eidola.device.proxy")


def _get_shell_output(result) -> str | None:
    """Extract string output from ShellResult object with proper type handling."""
    if result is None:
        return None
    
    output = None
    
    # Try stdout first (FIRERPA ShellResult), then output
    if hasattr(result, 'stdout') and result.stdout:
        output = result.stdout
    elif hasattr(result, 'output') and result.output:
        output = result.output
    else:
        output = result
    
    # Convert to string
    if output is None:
        return None
    if isinstance(output, bytes):
        output = output.decode('utf-8', errors='replace')
    elif not isinstance(output, str):
        output = str(output)
    
    output = output.strip()
    
    # Remove "stdout: " prefix if present
    if output.startswith("stdout:"):
        output = output[7:].strip()
    if output.startswith("stdout: "):
        output = output[8:].strip()
    
    return output if output else None


class ProxyManager:
    """
    Manages proxy configuration for Android devices via iptables + redsocks.
    
    Uses redsocks (transparent proxy) with iptables NAT REDIRECT rules
    to route all device TCP traffic through an HTTP CONNECT proxy.
    
    This replaces FIRERPA's built-in proxy which was confirmed non-functional
    (API returns success but doesn't actually route traffic).
    """
    
    # redsocks paths on FIRERPA devices
    REDSOCKS_BIN = "/data/server/bin/redsocks"
    REDSOCKS_CONF = "/data/local/tmp/redsocks.conf"
    REDSOCKS_LOG = "/data/local/tmp/redsocks.log"
    REDSOCKS_PORT = 31338  # Uncommon port to avoid collisions
    DNSTC_PORT = 5300      # Port for DNS-to-TCP converter
    
    # Mapping from config type strings to redsocks type
    REDSOCKS_TYPE_MAP = {
        "http-connect": "http-connect",
        "http": "http-connect",
        "https-connect": "http-connect",  # redsocks handles TLS
        "https": "http-connect",
        "socks5": "socks5",
    }
    
    def __init__(self, device: Device):
        """
        Initialize proxy manager for a device.
        
        Args:
            device: FIRERPA Device instance
        """
        self.device = device
        self._current_proxy: ProxyConfig | None = None
        # Store device IP early for reconnection after restart
        self._device_ip: str | None = None
        if hasattr(device, '_host'):
            self._device_ip = device._host
        elif hasattr(device, 'host'):
            self._device_ip = device.host
    
    def start_proxy(self, config: ProxyConfig, **kwargs) -> dict[str, Any]:
        """
        Start global proxy on the device using iptables + redsocks.
        
        This transparent proxy approach:
        1. Starts redsocks daemon (forwards TCP through HTTP CONNECT proxy)
        2. Sets iptables NAT rules to redirect all TCP to redsocks
        3. Excludes local/private networks from redirect
        
        Args:
            config: ProxyConfig with proxy settings
            
        Returns:
            dict with success status and details
        """
        if not config.enabled:
            return {"success": True, "message": "Proxy disabled in config"}
        
        logger.info("=" * 50)
        logger.info("PROXY CONFIG (redsocks method):")
        logger.info(f"  host: {config.host}")
        logger.info(f"  port: {config.port}")
        logger.info(f"  type: {config.type}")
        logger.info(f"  target_apps: {config.target_apps}")
        logger.info("=" * 50)
        
        try:
            # Step 1: Stop any existing proxy (clean state)
            logger.info("Cleaning previous proxy state...")
            self.stop_proxy()
            time.sleep(1.0)
            
            # Step 2: Fix DNS if needed
            self._fix_dns()
            
            # Step 3: Disable IPv6 (redsocks only routes IPv4)
            logger.info("Disabling IPv6...")
            self._disable_ipv6()
            
            # Step 4: Stop target apps before proxy starts
            logger.info("Stopping target apps before proxy start...")
            self._stop_target_apps(config)
            
            # Step 5: Resolve proxy hostname to IP
            proxy_ip = self._resolve_host(config.host)
            if not proxy_ip:
                return {"success": False, "error": f"Cannot resolve proxy host: {config.host}"}
            logger.info(f"Proxy IP resolved: {proxy_ip}")
            
            # Step 6: Get credentials
            username = self._build_decodo_login(config)
            password = self._get_proxy_password(config)
            if not password:
                return {"success": False, "error": "Missing proxy password"}
            
            # Step 7: Write redsocks config
            redsocks_type = self.REDSOCKS_TYPE_MAP.get(config.type.lower(), "http-connect")
            self._write_redsocks_config(proxy_ip, config.port, username, password, redsocks_type)
            
            # Step 8: Start redsocks daemon
            if not self._start_redsocks():
                return {"success": False, "error": "Failed to start redsocks daemon"}
            
            # Step 9: Set up iptables redirect
            if not self._setup_iptables(proxy_ip):
                self._stop_redsocks()
                return {"success": False, "error": "Failed to set up iptables rules"}
            
            # Step 10: Wait for routing to stabilize
            logger.info("Waiting for routing to stabilize...")
            time.sleep(3.0)
            
            self._current_proxy = config
            
            logger.info(f"Proxy started: {config.host}:{config.port} via redsocks")
            
            return {
                "success": True,
                "host": config.host,
                "port": config.port,
                "type": config.type,
                "method": "redsocks",
                "proxy_ip": proxy_ip,
            }
            
        except Exception as e:
            logger.error(f"Failed to start proxy: {e}")
            # Try to clean up on failure
            try:
                self._stop_redsocks()
                self._remove_iptables()
            except Exception:
                pass
            return {"success": False, "error": str(e)}
    
    # NOTE: Legacy proxy methods removed (properties.local, TUN fixes, diagnostics).
    # See git history before commit "Replace broken proxy with redsocks" for old code.
    
    def stop_proxy(self) -> dict[str, Any]:
        """
        Stop the device proxy (redsocks + iptables cleanup).
        
        Returns:
            dict with success status
        """
        try:
            self._remove_iptables()
            self._stop_redsocks()
            # Also stop legacy proxy in case it was running from previous attempts
            try:
                self.device.stop_gproxy()
            except Exception:
                pass
            self._current_proxy = None
            logger.info("Stopped device proxy (redsocks + iptables cleaned)")
            return {"success": True}
        except Exception as e:
            logger.error(f"Failed to stop proxy: {e}")
            return {"success": False, "error": str(e)}
    
    def is_proxy_healthy(self) -> bool:
        """
        Quick health check: is redsocks running and iptables rules in place?
        
        Call this before each session to detect post-reboot state.
        If returns False, caller should re-run start_proxy().
        """
        # Check redsocks process
        pid = _get_shell_output(
            self.device.execute_script("pidof redsocks 2>/dev/null")
        ) or ""
        if not pid or not pid.split()[0].isdigit():
            logger.warning("Proxy health check FAILED: redsocks not running")
            return False
        
        # Check iptables rules
        rules = _get_shell_output(
            self.device.execute_script("iptables -t nat -L REDSOCKS -n 2>/dev/null")
        ) or ""
        if "REDIRECT" not in rules:
            logger.warning("Proxy health check FAILED: iptables rules missing")
            return False
        
        logger.debug(f"Proxy healthy: redsocks PID={pid}")
        return True
    
    def ensure_proxy_active(self, config: ProxyConfig) -> dict[str, Any]:
        """
        Ensure proxy is active — restart if needed (e.g. after reboot).
        
        Call this before each session to handle device reboots gracefully.
        """
        if self.is_proxy_healthy():
            return {"success": True, "action": "already_running"}
        
        logger.warning("Proxy not active, restarting...")
        return self.start_proxy(config)
    
    # =========================================================================
    # REDSOCKS METHODS (transparent proxy via iptables)
    # =========================================================================
    
    def _resolve_host(self, hostname: str) -> str | None:
        """Resolve hostname to IP address on device."""
        # Method 1: ping (extract IP between parentheses)
        raw = _get_shell_output(
            self.device.execute_script(f"ping -c1 -W3 {hostname} 2>&1 | head -1")
        ) or ""
        if "(" in raw and ")" in raw:
            ip = raw[raw.index("(") + 1:raw.index(")")]
            if "." in ip and len(ip) <= 15:
                return ip
        
        # Method 2: nslookup
        raw = _get_shell_output(
            self.device.execute_script(f"nslookup {hostname} 8.8.8.8 2>&1")
        ) or ""
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("Address") and "#" not in line and ":" in line:
                ip = line.split(":")[-1].strip()
                if "." in ip and len(ip) <= 15:
                    return ip
        
        # Method 3: Known hosts fallback
        known = {"isp.decodo.com": "185.111.111.38", "gate.decodo.com": "185.111.111.38"}
        if hostname in known:
            logger.warning(f"Using known IP for {hostname}: {known[hostname]}")
            return known[hostname]
        
        return None
    
    def _write_redsocks_config(self, proxy_ip: str, proxy_port: int, 
                                 username: str, password: str, 
                                 proxy_type: str = "http-connect"):
        """Write redsocks config file to device.
        
        Note: FIRERPA's redsocks build does NOT support dnstc section.
        DNS leak prevention is handled via iptables DNAT to Google DNS.
        """
        config = (
            f'base {{\n'
            f'    log_debug = off;\n'
            f'    log_info = on;\n'
            f'    log = "file:{self.REDSOCKS_LOG}";\n'
            f'    daemon = on;\n'
            f'    redirector = iptables;\n'
            f'}}\n\n'
            f'redsocks {{\n'
            f'    bind = "127.0.0.1:{self.REDSOCKS_PORT}";\n'
            f'    relay = "{proxy_ip}:{proxy_port}";\n'
            f'    type = {proxy_type};\n'
            f'    login = "{username}";\n'
            f'    password = "{password}";\n'
            f'}}\n'
        )
        
        # Write config using heredoc
        self.device.execute_script(f"rm -f {self.REDSOCKS_CONF}")
        self.device.execute_script(
            f"cat > {self.REDSOCKS_CONF} << 'REDSOCKS_EOF'\n{config}REDSOCKS_EOF"
        )
        
        # Set restrictive permissions (credentials in file!)
        self.device.execute_script(f"chmod 600 {self.REDSOCKS_CONF}")
        
        # Verify
        written = _get_shell_output(self.device.execute_script(f"cat {self.REDSOCKS_CONF}")) or ""
        if "redsocks" not in written:
            raise RuntimeError(f"Failed to write redsocks config to {self.REDSOCKS_CONF}")
        
        logger.info(f"Redsocks config written to {self.REDSOCKS_CONF} (chmod 600)")
    
    def _start_redsocks(self) -> bool:
        """Start redsocks daemon."""
        # Kill any existing instance
        self.device.execute_script("killall redsocks 2>/dev/null || true")
        time.sleep(0.5)
        
        # Start daemon
        result = _get_shell_output(
            self.device.execute_script(f"{self.REDSOCKS_BIN} -c {self.REDSOCKS_CONF} 2>&1")
        ) or ""
        
        if result and ("error" in result.lower() or "unknown" in result.lower()):
            logger.error(f"Redsocks start failed: {result}")
            return False
        
        time.sleep(1.0)
        
        # Verify running
        pid = _get_shell_output(
            self.device.execute_script("pidof redsocks 2>/dev/null")
        ) or ""
        
        if pid and pid.isdigit():
            logger.info(f"Redsocks running (PID: {pid})")
            return True
        
        logger.error("Redsocks failed to start (no PID)")
        log = _get_shell_output(self.device.execute_script(f"cat {self.REDSOCKS_LOG} 2>/dev/null"))
        if log:
            logger.error(f"Redsocks log: {log[:300]}")
        return False
    
    def _stop_redsocks(self):
        """Stop redsocks daemon and clean up files."""
        self.device.execute_script("killall redsocks 2>/dev/null || true")
        self.device.execute_script(
            f"rm -f {self.REDSOCKS_CONF} {self.REDSOCKS_LOG} 2>/dev/null || true"
        )
    
    def _setup_iptables(self, proxy_ip: str) -> bool:
        """Set up iptables NAT + filter rules for transparent proxy.
        
        This sets up:
        1. TCP redirect through redsocks (REDSOCKS chain)
        2. DNS leak prevention (UDP:53 -> dnstc -> TCP -> proxy)
        3. UDP drop (prevents QUIC/WebRTC IP leaks)
        """
        # Clean existing rules
        self._remove_iptables()
        
        # === TCP REDIRECT via REDSOCKS chain ===
        self.device.execute_script("iptables -t nat -N REDSOCKS")
        
        # Exclude local/private networks
        for network in [
            "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", 
            "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
            "224.0.0.0/4", "240.0.0.0/4"
        ]:
            self.device.execute_script(f"iptables -t nat -A REDSOCKS -d {network} -j RETURN")
        
        # CRITICAL: Exclude proxy server itself (prevent routing loop)
        self.device.execute_script(f"iptables -t nat -A REDSOCKS -d {proxy_ip} -j RETURN")
        
        # Redirect all other TCP to redsocks
        self.device.execute_script(
            f"iptables -t nat -A REDSOCKS -p tcp -j REDIRECT --to-ports {self.REDSOCKS_PORT}"
        )
        
        # Apply REDSOCKS chain to OUTPUT
        self.device.execute_script("iptables -t nat -A OUTPUT -j REDSOCKS")
        
        # === DNS LEAK PREVENTION ===
        # DNAT all DNS to Google DNS (prevents ISP DNS leak)
        # Note: Google responds from nearest PoP (Spain for PT devices)
        # This is a minor mismatch, but Google DNS is neutral (not ISP DNS)
        # TODO: implement DNS-over-TCP proxy for full US DNS geo matching
        self.device.execute_script(
            "iptables -t nat -A OUTPUT -p udp --dport 53 -j DNAT --to-destination 8.8.8.8:53"
        )
        self.device.execute_script(
            "iptables -t nat -A OUTPUT -p tcp --dport 53 -j DNAT --to-destination 8.8.8.8:53"
        )
        self.device.execute_script("setprop net.dns1 8.8.8.8")
        self.device.execute_script("setprop net.dns2 8.8.4.4")
        
        # === UDP DROP (prevents QUIC/WebRTC IP leaks) ===
        # Allow local network UDP (mDNS, DHCP, etc.)
        self.device.execute_script("iptables -A OUTPUT -p udp -d 10.0.0.0/8 -j ACCEPT")
        self.device.execute_script("iptables -A OUTPUT -p udp -d 192.168.0.0/16 -j ACCEPT")
        self.device.execute_script("iptables -A OUTPUT -p udp -d 127.0.0.0/8 -j ACCEPT")
        # CRITICAL: Allow DNS UDP to 8.8.8.8 AFTER DNAT rewrites destination
        # Without this, DNAT changes dest to 8.8.8.8 but DROP catches it!
        self.device.execute_script("iptables -A OUTPUT -p udp -d 8.8.8.8 --dport 53 -j ACCEPT")
        self.device.execute_script("iptables -A OUTPUT -p udp -d 8.8.4.4 --dport 53 -j ACCEPT")
        # DROP all other UDP (blocks QUIC, WebRTC STUN, etc.)
        self.device.execute_script("iptables -A OUTPUT -p udp -j DROP")
        
        logger.info("iptables rules applied: TCP redirect + DNS leak prevention + UDP drop")
        
        # Verify TCP redirect
        rules = _get_shell_output(
            self.device.execute_script("iptables -t nat -L REDSOCKS -n 2>/dev/null")
        ) or ""
        
        if "REDIRECT" in rules:
            logger.info("iptables REDSOCKS chain verified")
            return True
        
        logger.error("Failed to apply iptables rules")
        return False
    
    def _remove_iptables(self):
        """Remove all redsocks iptables rules (NAT + filter)."""
        # Remove NAT rules
        self.device.execute_script("iptables -t nat -D OUTPUT -j REDSOCKS 2>/dev/null || true")
        self.device.execute_script("iptables -t nat -F REDSOCKS 2>/dev/null || true")
        self.device.execute_script("iptables -t nat -X REDSOCKS 2>/dev/null || true")
        # Remove DNS DNAT
        self.device.execute_script("iptables -t nat -D OUTPUT -p udp --dport 53 -j DNAT --to-destination 8.8.8.8:53 2>/dev/null || true")
        self.device.execute_script("iptables -t nat -D OUTPUT -p tcp --dport 53 -j DNAT --to-destination 8.8.8.8:53 2>/dev/null || true")
        # Remove UDP drop rules (remove in reverse order)
        self.device.execute_script("iptables -D OUTPUT -p udp -j DROP 2>/dev/null || true")
        self.device.execute_script("iptables -D OUTPUT -p udp -d 8.8.8.8 --dport 53 -j ACCEPT 2>/dev/null || true")
        self.device.execute_script("iptables -D OUTPUT -p udp -d 8.8.4.4 --dport 53 -j ACCEPT 2>/dev/null || true")
        self.device.execute_script("iptables -D OUTPUT -p udp -d 127.0.0.0/8 -j ACCEPT 2>/dev/null || true")
        self.device.execute_script("iptables -D OUTPUT -p udp -d 192.168.0.0/16 -j ACCEPT 2>/dev/null || true")
        self.device.execute_script("iptables -D OUTPUT -p udp -d 10.0.0.0/8 -j ACCEPT 2>/dev/null || true")
    
    def _fix_dns(self):
        """Ensure DNS is configured on device."""
        result = _get_shell_output(self.device.execute_script("getprop net.dns1")) or ""
        if not result:
            logger.warning("DNS not configured, setting Google DNS...")
            self.device.execute_script("setprop net.dns1 8.8.8.8")
            self.device.execute_script("setprop net.dns2 8.8.4.4")
            self.device.execute_script("ndc resolver setnetdns 100 '' 8.8.8.8 8.8.4.4 2>/dev/null || true")
    
    def _disable_ipv6(self) -> None:
        """
        Disable IPv6 on the device to force all traffic through IPv4.
        
        CRITICAL: redsocks only routes IPv4 traffic. If IPv6 is enabled,
        connections may bypass the proxy tunnel entirely, causing:
        - SSL_ERROR_SYSCALL errors on HTTPS
        - Apps unable to connect while curl works
        - DNS leaks through IPv6
        
        Note: FIRERPA execute_script already runs as root (uid=0), no su needed.
        """
        try:
            # Disable IPv6 globally (no su -c needed, execute_script runs as root)
            result = self.device.execute_script(
                "sysctl -w net.ipv6.conf.all.disable_ipv6=1"
            )
            exit_code = getattr(result, 'exitstatus', None)
            output = _get_shell_output(result)
            if exit_code == 0:
                logger.debug(f"Disabled IPv6 globally: {output}")
            else:
                logger.warning(f"IPv6 disable (all) returned exit {exit_code}: {output}")
            
            # Disable IPv6 on wlan0 specifically
            result = self.device.execute_script(
                "sysctl -w net.ipv6.conf.wlan0.disable_ipv6=1"
            )
            exit_code = getattr(result, 'exitstatus', None)
            output = _get_shell_output(result)
            if exit_code == 0:
                logger.debug(f"Disabled IPv6 on wlan0: {output}")
            else:
                logger.warning(f"IPv6 disable (wlan0) returned exit {exit_code}: {output}")
            
            logger.info("IPv6 disabled - all traffic will use IPv4 through proxy")
            
        except Exception as e:
            # Non-fatal - log warning but continue
            logger.warning(f"Failed to disable IPv6: {e}")
    
    def verify_proxy_ip(self) -> dict[str, Any]:
        """
        Verify the proxy is working by checking external IP.
        
        IMPORTANT: With redsocks, shell curl IS routed through the proxy.
        We also support explicit proxy URL for verification.
        
        Uses ip-api.com/json to get the current external IP address.
        
        Returns:
            dict with IP address and verification status
        """
        try:
            import json
            
            # Build explicit proxy URL for curl
            # With redsocks, curl is routed through proxy automatically
            proxy_url = self._build_proxy_url()
            
            if proxy_url:
                # Use explicit proxy flag (-x)
                # Use HTTP (not HTTPS) for ip-api - SOCKS5 proxies handle this better
                curl_cmd = f'curl -s --connect-timeout 15 -x "{proxy_url}" http://ip-api.com/json/'
                logger.debug(f"Verifying proxy with explicit -x flag: {self._current_proxy.host}:{self._current_proxy.port}")
            else:
                # No proxy configured - direct connection
                curl_cmd = "curl -s --connect-timeout 10 http://ip-api.com/json/"
                logger.debug("Verifying IP without proxy (no proxy configured)")
            
            result = self.device.execute_script(curl_cmd)
            output = _get_shell_output(result)
            
            if output:
                try:
                    data = json.loads(output)
                    if data.get("status") == "success":
                        ip = data.get("query", "")
                        logger.info(f"Verified proxy IP: {ip} ({data.get('countryCode', '?')})")
                        return {
                            "success": True,
                            "ip": ip,
                            "country_code": data.get("countryCode"),
                            "proxy_active": self._current_proxy is not None,
                        }
                    else:
                        return {
                            "success": False,
                            "error": data.get("message", "IP lookup failed"),
                        }
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse IP response: {output[:200]}")
                    return {
                        "success": False,
                        "error": f"JSON parse error: {e}",
                        "raw_response": output[:200] if output else None,
                    }
            else:
                return {
                    "success": False,
                    "error": "Empty response from IP check service",
                }
                
        except Exception as e:
            logger.error(f"Failed to verify proxy IP: {e}")
            return {"success": False, "error": str(e)}
    
    def verify_proxy_routing(self, expected_country: str = "US") -> dict[str, Any]:
        """
        Verify that proxy is routing traffic via redsocks.
        
        With redsocks + iptables, ALL TCP traffic (including root curl)
        gets redirected through the proxy. So curl without -x WILL show
        the proxy IP (redsocks redirects all TCP including root processes).
        
        Also checks that redsocks process is running and iptables rules exist.
        
        Args:
            expected_country: Expected country code (e.g., "US", "DE")
            
        Returns:
            dict with routing status and IP info
        """
        try:
            import json
            
            # Check 1: Is redsocks running?
            pid = _get_shell_output(
                self.device.execute_script("pidof redsocks 2>/dev/null")
            ) or ""
            if not pid or not pid.split()[0].isdigit():
                logger.warning("Redsocks process not running!")
                return {"success": False, "routing_works": False, "error": "redsocks not running"}
            
            # Check 2: Are iptables rules in place?
            rules = _get_shell_output(
                self.device.execute_script("iptables -t nat -L REDSOCKS -n 2>/dev/null")
            ) or ""
            if "REDIRECT" not in rules:
                logger.warning("iptables REDSOCKS rules not found!")
                return {"success": False, "routing_works": False, "error": "iptables rules missing"}
            
            # Check 3: Test actual IP (redsocks redirects ALL TCP including root curl)
            curl_cmd = "curl -s --connect-timeout 10 http://ip-api.com/json/"
            logger.info("Testing proxy routing (curl through redsocks)...")
            
            result = self.device.execute_script(curl_cmd)
            output = _get_shell_output(result)
            
            if not output:
                return {"success": False, "routing_works": False, "error": "Empty response"}
            
            try:
                data = json.loads(output)
                if data.get("status") == "success":
                    ip = data.get("query", "")
                    country = data.get("countryCode", "")
                    
                    routing_works = country.upper() == expected_country.upper()
                    
                    if routing_works:
                        logger.info(f"✅ PROXY ROUTING WORKS: IP={ip}, Country={country}")
                    else:
                        logger.warning(f"⚠️ Unexpected country: {country} (expected {expected_country})")
                    
                    return {
                        "success": True,
                        "routing_works": routing_works,
                        "ip": ip,
                        "country_code": country,
                        "expected_country": expected_country,
                        "isp": data.get("isp", ""),
                        "redsocks_pid": pid,
                    }
                else:
                    return {"success": False, "routing_works": False, "error": "IP lookup failed"}
            except json.JSONDecodeError:
                return {"success": False, "routing_works": False, "error": f"Bad response: {output[:100]}"}
                
        except Exception as e:
            logger.error(f"Failed to verify proxy routing: {e}")
            return {"success": False, "routing_works": False, "error": str(e)}
    
    # Keep old name as alias for compatibility
    def verify_gproxy_routing(self, expected_country: str = "US") -> dict[str, Any]:
        """Deprecated alias for verify_proxy_routing (backward compatibility)."""
        return self.verify_proxy_routing(expected_country)
    
    def get_ip_geolocation(self) -> dict[str, Any]:
        """
        Get geolocation of current IP address.
        
        IMPORTANT: With redsocks, shell curl IS routed through the proxy.
        We also support explicit proxy URL for verification.
        
        Uses ip-api.com for free geolocation lookup.
        
        Returns:
            dict with location details (country, city, lat, lon)
        """
        try:
            import json
            
            # Build explicit proxy URL for curl
            # With redsocks, curl is routed through proxy automatically
            proxy_url = self._build_proxy_url()
            
            if proxy_url:
                # Use explicit proxy flag (-x)
                curl_cmd = f'curl -s --connect-timeout 15 -x "{proxy_url}" http://ip-api.com/json/'
            else:
                curl_cmd = "curl -s --connect-timeout 10 http://ip-api.com/json/"
            
            result = self.device.execute_script(curl_cmd)
            output = _get_shell_output(result)
            
            if output:
                try:
                    data = json.loads(output)
                    
                    if data.get("status") == "success":
                        return {
                            "success": True,
                            "ip": data.get("query"),
                            "country": data.get("country"),
                            "country_code": data.get("countryCode"),
                            "city": data.get("city"),
                            "region": data.get("regionName"),
                            "latitude": data.get("lat"),
                            "longitude": data.get("lon"),
                            "isp": data.get("isp"),
                            "timezone": data.get("timezone"),
                        }
                    else:
                        return {
                            "success": False,
                            "error": data.get("message", "Geolocation lookup failed"),
                        }
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse geolocation response: {output[:200]}")
                    return {
                        "success": False,
                        "error": f"JSON parse error: {e}",
                        "raw_response": output[:200] if output else None,
                    }
            
            return {"success": False, "error": "Empty response from ip-api"}
            
        except Exception as e:
            logger.error(f"Failed to get IP geolocation: {e}")
            return {"success": False, "error": str(e)}
    
    def _build_decodo_login(self, config: ProxyConfig) -> str:
        """
        Build Decodo proxy login string.
        
        For Dedicated ISP proxies (isp.decodo.com), each port is a dedicated IP,
        so we just use the base username without sticky session parameters.
        
        For Residential proxies (gate.decodo.com), we add sticky session params:
        Format: {username}-country-{country}-session-{session_id}-sessionduration-{minutes}
        
        Args:
            config: ProxyConfig with proxy settings
            
        Returns:
            Formatted login string
        """
        base_username = self._get_proxy_username(config)
        
        # For Dedicated ISP proxies (isp.decodo.com), just use base username
        # Each port (10001-10010) is already a dedicated static IP
        if config.host and "isp.decodo.com" in config.host:
            logger.debug(f"Using Dedicated ISP login: {base_username}")
            return base_username
        
        # For Residential proxies, build sticky session string
        # Only add country if not already in username
        country = getattr(config, 'country', 'us')
        if hasattr(config, 'geo') and config.geo:
            country = config.geo.country_code.lower()
        
        # Check if country already in username to avoid duplication
        if f"-country-{country}" in base_username.lower():
            login = base_username
        else:
            login = f"{base_username}-country-{country}"
        
        logger.debug(f"Built Decodo login: {login}")
        return login
    
    def _get_proxy_username(self, config: ProxyConfig) -> str:
        """Get proxy username from environment variable."""
        return os.environ.get(config.username_env, "")
    
    def _get_proxy_password(self, config: ProxyConfig) -> str:
        """Get proxy password from environment variable."""
        return os.environ.get(config.password_env, "")
    
    def _build_proxy_url(self, config: ProxyConfig | None = None) -> str | None:
        """
        Build proxy URL for curl -x flag.
        
        Shell commands (curl) via execute_script are routed through redsocks.
        This method also supports building explicit proxy URLs for curl -x.
        
        Args:
            config: ProxyConfig to use, or current proxy if None
            
        Returns:
            Proxy URL in format: protocol://user:pass@host:port
            or None if no proxy configured
        """
        cfg = config or self._current_proxy
        if not cfg or not cfg.enabled:
            return None
        
        # Determine protocol for curl
        proxy_type = cfg.type.lower()
        if proxy_type in ("socks5", "shadowsocks"):
            protocol = "socks5"
        else:
            protocol = "http"
        
        username = self._build_decodo_login(cfg)
        password = self._get_proxy_password(cfg)
        
        if username and password:
            # URL-encode special characters in password (@ : etc)
            import urllib.parse
            safe_password = urllib.parse.quote(password, safe='')
            return f"{protocol}://{username}:{safe_password}@{cfg.host}:{cfg.port}"
        else:
            return f"{protocol}://{cfg.host}:{cfg.port}"
    
    def _stop_target_apps(self, config: ProxyConfig):
        """
        Stop target apps before proxy configuration.
        
        CRITICAL: Apps MUST be stopped BEFORE proxy starts.
        This prevents IP leaks from existing TCP connections.
        """
        apps_to_stop = config.target_apps or ["com.instagram.android"]
        
        for app_package in apps_to_stop:
            try:
                self.device.execute_script(f"am force-stop {app_package}")
                logger.info(f"Stopped app: {app_package}")
            except Exception as e:
                logger.warning(f"Failed to stop {app_package}: {e}")
        
        # Wait for apps to fully stop
        time.sleep(1.0)
    


def create_proxy_manager(device_ip: str) -> ProxyManager:
    """
    Create a ProxyManager for a device.
    
    Args:
        device_ip: Device IP address
        
    Returns:
        ProxyManager instance
    """
    device = Device(device_ip)
    return ProxyManager(device)
