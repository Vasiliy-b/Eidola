"""
Device fingerprint randomization module.

Modifies Android device identifiers to prevent fingerprinting across accounts.

=== IMPORTANT: Android 10+ Fingerprinting Facts ===

1. WiFi MAC Address: Instagram CANNOT read this on Android 10+.
   The OS blocks access and randomizes MAC by default.
   We skip MAC changes - they're unnecessary and require root.

2. Critical identifiers for Instagram isolation:
   - MediaDRM ID: Most persistent (survives factory reset) - REQUIRES ROOT
   - Android ID: Per-app identifier - CAN change without root ✓
   - IP Address: Via proxy - handled separately ✓
   - Build Fingerprint/Model: Visible to apps - requires root to change
   
3. What we CAN change without root:
   - Android ID (Settings.Secure.ANDROID_ID) ✓
   - GPS Location (via mock location service) ✓
   - Network IP (via proxy) ✓

4. What we CANNOT change without root:
   - MediaDRM ID (most critical for device bans)
   - WiFi MAC (but Instagram can't see it anyway)
   - Build properties (ro.product.model, etc.)

WiFi MAC changes use ADB (via USB) but are OPTIONAL - they don't affect
Instagram fingerprinting. We treat MAC change failures as soft warnings.
"""

import logging
import random
import secrets
import string
import subprocess
import time
from typing import Any

from lamda.client import Device

from ..config import FingerprintConfig

logger = logging.getLogger("eidola.device.fingerprint")


def _run_adb_command(serial: str, command: str, timeout: int = 30) -> tuple[bool, str]:
    """
    Run ADB command on device via USB connection.
    
    This is used for operations that would break WiFi (like MAC change).
    ADB over USB remains connected even when WiFi is disabled.
    
    Args:
        serial: ADB device serial number
        command: Shell command to run
        timeout: Command timeout in seconds
        
    Returns:
        Tuple of (success, output/error)
    """
    try:
        full_cmd = ["adb", "-s", serial, "shell", command]
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        
        output = result.stdout.strip() if result.stdout else ""
        error = result.stderr.strip() if result.stderr else ""
        
        if result.returncode == 0:
            return True, output
        else:
            return False, error or output or f"Exit code: {result.returncode}"
            
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s"
    except FileNotFoundError:
        return False, "ADB not found in PATH"
    except Exception as e:
        return False, str(e)


def _get_shell_output(result) -> str | None:
    """
    Extract string output from shell execution result.
    
    FIRERPA execute_script returns ShellResult object, not string.
    This helper safely extracts the output with proper type handling.
    
    Handles formats like:
    - Direct string output
    - ShellResult with .output or .stdout attribute
    - String with "stdout: " prefix (FIRERPA format)
    """
    if result is None:
        return None
    
    output = None
    
    # Try to get stdout attribute first (FIRERPA ShellResult)
    if hasattr(result, 'stdout') and result.stdout:
        output = result.stdout
    # Then try output attribute
    elif hasattr(result, 'output') and result.output:
        output = result.output
    else:
        output = result
    
    # Convert to string if needed
    if output is None:
        return None
    if isinstance(output, bytes):
        output = output.decode('utf-8', errors='replace')
    elif not isinstance(output, str):
        output = str(output)
    
    # Strip whitespace
    output = output.strip()
    
    # Remove "stdout: " prefix if present (FIRERPA format)
    if output.startswith("stdout:"):
        output = output[7:].strip()
    if output.startswith("stdout: "):
        output = output[8:].strip()
    
    return output if output else None


class FingerprintManager:
    """
    Manages device fingerprint randomization.
    
    Can modify:
    - Android ID (secure settings) - via FIRERPA
    - WiFi MAC address - via ADB (to avoid breaking FIRERPA connection)
    - Build properties (model, brand, manufacturer) - via ADB
    
    Most operations require root access.
    WiFi operations use ADB over USB to keep FIRERPA connection alive.
    """
    
    # Common device models for spoofing
    DEVICE_MODELS = [
        {"model": "SM-G998B", "brand": "samsung", "manufacturer": "samsung"},
        {"model": "SM-S908B", "brand": "samsung", "manufacturer": "samsung"},
        {"model": "Pixel 7 Pro", "brand": "google", "manufacturer": "Google"},
        {"model": "Pixel 8", "brand": "google", "manufacturer": "Google"},
        {"model": "22021211RG", "brand": "Xiaomi", "manufacturer": "Xiaomi"},
        {"model": "2201116SG", "brand": "Xiaomi", "manufacturer": "Xiaomi"},
        {"model": "LE2125", "brand": "OnePlus", "manufacturer": "OnePlus"},
        {"model": "CPH2451", "brand": "OPPO", "manufacturer": "OPPO"},
    ]
    
    def __init__(self, device: Device, adb_serial: str | None = None):
        """
        Initialize fingerprint manager.
        
        Args:
            device: FIRERPA Device instance
            adb_serial: ADB serial for USB operations (required for WiFi MAC changes)
        """
        self.device = device
        self.adb_serial = adb_serial
    
    def change_wifi_mac_via_adb(self, new_mac: str | None = None) -> dict[str, Any]:
        """
        Change WiFi MAC address using ADB over USB.
        
        This method uses ADB instead of FIRERPA to avoid breaking the
        network connection when WiFi is disabled.
        
        Args:
            new_mac: New MAC address (random if not specified)
            
        Returns:
            dict with success status and new MAC
        """
        if not self.adb_serial:
            return {
                "success": False,
                "error": "ADB serial not configured - cannot change WiFi MAC safely"
            }
        
        # Generate random MAC if not specified
        if not new_mac:
            # Generate locally administered MAC (bit 1 of first byte set)
            first_byte = random.randint(0, 255) | 0x02  # Set locally administered bit
            first_byte = first_byte & 0xFE  # Clear multicast bit
            mac_bytes = [first_byte] + [random.randint(0, 255) for _ in range(5)]
            new_mac = ":".join(f"{b:02x}" for b in mac_bytes)
        
        logger.info(f"Changing WiFi MAC via ADB to: {new_mac}")
        
        try:
            # 1. Disable WiFi
            success, output = _run_adb_command(self.adb_serial, "svc wifi disable")
            if not success:
                return {"success": False, "error": f"Failed to disable WiFi: {output}"}
            
            time.sleep(1)
            
            # 2. Change MAC address (requires root - may fail on non-rooted devices)
            success, output = _run_adb_command(self.adb_serial, f"ip link set wlan0 address {new_mac}")
            if not success:
                # MAC change failed (needs root) - re-enable WiFi and continue
                # This is a soft failure - WiFi is more important than MAC change
                logger.warning(f"MAC change failed (needs root): {output}")
                _run_adb_command(self.adb_serial, "svc wifi enable")
                time.sleep(3)  # Wait for WiFi to reconnect
                return {
                    "success": True,  # Soft success - WiFi is back
                    "wifi_mac": new_mac,
                    "warning": f"MAC change requires root: {output}",
                    "applied": False
                }
            
            # 3. Re-enable WiFi
            success, output = _run_adb_command(self.adb_serial, "svc wifi enable")
            if not success:
                return {"success": False, "error": f"Failed to re-enable WiFi: {output}"}
            
            # 4. Wait for WiFi to reconnect
            logger.info("Waiting for WiFi to reconnect...")
            time.sleep(5)
            
            # 5. Verify (optional - may fail if device IP changed)
            success, current_mac = _run_adb_command(
                self.adb_serial, 
                "cat /sys/class/net/wlan0/address 2>/dev/null"
            )
            
            if success and new_mac.lower() in current_mac.lower():
                logger.info(f"✅ WiFi MAC changed successfully: {new_mac}")
                return {"success": True, "wifi_mac": new_mac}
            else:
                # MAC might not have changed (needs root), but WiFi is back
                logger.warning(f"MAC change may not have worked (needs root), current: {current_mac}")
                return {
                    "success": True,  # WiFi is back, that's what matters
                    "wifi_mac": new_mac,
                    "warning": "MAC change requires root, may not have applied",
                    "current_mac": current_mac if success else "unknown"
                }
                
        except Exception as e:
            # Try to recover WiFi
            _run_adb_command(self.adb_serial, "svc wifi enable")
            logger.error(f"WiFi MAC change failed: {e}")
            return {"success": False, "error": str(e)}
    
    def get_current_fingerprint(self) -> dict[str, Any]:
        """
        Get current device fingerprint values.
        
        Returns:
            dict with current fingerprint identifiers and success status
        """
        fingerprint = {"success": False}
        
        try:
            # Android ID
            result = self.device.execute_script(
                "settings get secure android_id"
            )
            fingerprint["android_id"] = _get_shell_output(result)
            
            # WiFi MAC
            result = self.device.execute_script(
                "cat /sys/class/net/wlan0/address 2>/dev/null || ip link show wlan0 | grep ether | awk '{print $2}'"
            )
            fingerprint["wifi_mac"] = _get_shell_output(result)
            
            # Build properties
            props = ["ro.product.model", "ro.product.brand", "ro.product.manufacturer", 
                     "ro.build.fingerprint", "ro.serialno"]
            
            for prop in props:
                result = self.device.execute_script(f"getprop {prop}")
                key = prop.replace("ro.product.", "").replace("ro.build.", "").replace("ro.", "")
                fingerprint[key] = _get_shell_output(result)
            
            # Mark as success if we got android_id
            if fingerprint.get("android_id"):
                fingerprint["success"] = True
                logger.info(f"Got current fingerprint: android_id={fingerprint.get('android_id', 'N/A')[:8]}...")
            else:
                fingerprint["error"] = "Could not read android_id"
            
        except Exception as e:
            logger.error(f"Failed to get fingerprint: {e}")
            fingerprint["error"] = str(e)
        
        return fingerprint
    
    def randomize_android_id(self) -> dict[str, Any]:
        """
        Generate and set a random Android ID.
        
        Android ID is a 16-character hex string stored in secure settings.
        
        Returns:
            dict with new Android ID
        """
        try:
            # Generate random 16-char hex string
            new_id = secrets.token_hex(8)  # 8 bytes = 16 hex chars
            
            # Set the new Android ID
            result = self.device.execute_script(
                f"settings put secure android_id {new_id}"
            )
            
            # Verify it was set
            verify = self.device.execute_script("settings get secure android_id")
            verify_output = _get_shell_output(verify)
            
            if verify_output == new_id:
                logger.info(f"Set new Android ID: {new_id}")
                return {"success": True, "android_id": new_id}
            else:
                return {
                    "success": False, 
                    "error": "Failed to verify Android ID change",
                    "expected": new_id,
                    "actual": verify_output,
                }
                
        except Exception as e:
            logger.error(f"Failed to randomize Android ID: {e}")
            return {"success": False, "error": str(e)}
    
    def randomize_wifi_mac(self) -> dict[str, Any]:
        """
        Generate and set a random WiFi MAC address.
        
        Uses locally administered MAC (bit 1 of first byte set).
        Requires root and may need WiFi to be disabled/re-enabled.
        
        Returns:
            dict with new MAC address
        """
        try:
            # Generate random MAC with locally administered bit set
            # First byte: x2, x6, xA, or xE (locally administered)
            first_byte = random.choice(["02", "06", "0a", "0e"])
            rest_bytes = [secrets.token_hex(1) for _ in range(5)]
            new_mac = f"{first_byte}:{':'.join(rest_bytes)}"
            
            # Need to disable WiFi, change MAC, re-enable
            commands = [
                "svc wifi disable",
                f"ip link set wlan0 address {new_mac}",
                "svc wifi enable",
            ]
            
            for cmd in commands:
                self.device.execute_script(cmd)
            
            # Wait for WiFi to reconnect
            import time
            time.sleep(3)
            
            # Verify
            result = self.device.execute_script(
                "cat /sys/class/net/wlan0/address 2>/dev/null"
            )
            result_output = _get_shell_output(result)
            
            if result_output and new_mac.lower() in result_output.lower():
                logger.info(f"Set new WiFi MAC: {new_mac}")
                return {"success": True, "wifi_mac": new_mac}
            else:
                return {
                    "success": False,
                    "error": "MAC may not have changed (requires root)",
                    "attempted": new_mac,
                    "current": result_output,
                }
                
        except Exception as e:
            logger.error(f"Failed to randomize WiFi MAC: {e}")
            return {"success": False, "error": str(e)}
    
    def modify_build_prop(
        self,
        model: str | None = None,
        brand: str | None = None,
        manufacturer: str | None = None,
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """
        Modify build.prop values for device spoofing.
        
        CAUTION: Modifying build.prop can cause issues. Always backup first.
        Requires remounting /system as read-write (root required).
        Changes take effect after reboot.
        
        Args:
            model: Device model (ro.product.model)
            brand: Device brand (ro.product.brand)
            manufacturer: Device manufacturer (ro.product.manufacturer)
            fingerprint: Build fingerprint (ro.build.fingerprint)
            
        Returns:
            dict with modification status
        """
        try:
            # Backup build.prop first
            self.device.execute_script(
                "cp /system/build.prop /system/build.prop.backup 2>/dev/null || true"
            )
            
            # Remount /system as read-write
            self.device.execute_script("mount -o rw,remount /system")
            
            changes = []
            
            if model:
                self.device.execute_script(
                    f"sed -i 's/ro.product.model=.*/ro.product.model={model}/' /system/build.prop"
                )
                changes.append(f"model={model}")
            
            if brand:
                self.device.execute_script(
                    f"sed -i 's/ro.product.brand=.*/ro.product.brand={brand}/' /system/build.prop"
                )
                changes.append(f"brand={brand}")
            
            if manufacturer:
                self.device.execute_script(
                    f"sed -i 's/ro.product.manufacturer=.*/ro.product.manufacturer={manufacturer}/' /system/build.prop"
                )
                changes.append(f"manufacturer={manufacturer}")
            
            if fingerprint:
                self.device.execute_script(
                    f"sed -i 's/ro.build.fingerprint=.*/ro.build.fingerprint={fingerprint}/' /system/build.prop"
                )
                changes.append("fingerprint=<modified>")
            
            # Remount as read-only
            self.device.execute_script("mount -o ro,remount /system")
            
            logger.info(f"Modified build.prop: {', '.join(changes)}")
            
            return {
                "success": True,
                "changes": changes,
                "reboot_required": True,
                "message": "Reboot device for changes to take effect",
            }
            
        except Exception as e:
            logger.error(f"Failed to modify build.prop: {e}")
            # Try to remount as read-only for safety
            try:
                self.device.execute_script("mount -o ro,remount /system")
            except:
                pass
            return {"success": False, "error": str(e)}
    
    def apply_fingerprint(self, config: FingerprintConfig) -> dict[str, Any]:
        """
        Apply a complete fingerprint configuration.
        
        WiFi MAC changes use ADB over USB to avoid breaking FIRERPA connection.
        If ADB serial is not configured, WiFi MAC change is skipped.
        
        Args:
            config: FingerprintConfig with desired values
            
        Returns:
            dict with results for each component
        """
        results = {}
        
        # Android ID (if not specified, generate random)
        if config.android_id:
            try:
                self.device.execute_script(
                    f"settings put secure android_id {config.android_id}"
                )
                results["android_id"] = {"success": True, "value": config.android_id}
            except Exception as e:
                results["android_id"] = {"success": False, "error": str(e)}
        else:
            results["android_id"] = self.randomize_android_id()
        
        # WiFi MAC - OPTIONAL: Instagram cannot read MAC on Android 10+
        # We attempt change via ADB if available, but failures are non-critical
        if self.adb_serial:
            logger.info("Attempting WiFi MAC change via ADB (optional - Instagram can't see MAC)...")
            results["wifi_mac"] = self.change_wifi_mac_via_adb(config.wifi_mac)
            if results["wifi_mac"].get("warning"):
                logger.info(f"ℹ️ WiFi MAC not changed (needs root) - this is OK for Instagram")
        else:
            # No ADB serial - skip WiFi MAC change entirely
            results["wifi_mac"] = {
                "success": True, 
                "skipped": True,
                "reason": "WiFi MAC change skipped - Instagram cannot see MAC on Android 10+"
            }
            logger.info("⏭️ Skipping WiFi MAC change (not needed for Instagram on Android 10+)")
        
        # Build properties - skip for now, requires system remount and is risky
        if any([config.build_model, config.build_brand, config.build_manufacturer]):
            results["build_prop"] = {
                "success": True,
                "skipped": True,
                "reason": "build.prop modification skipped - risky operation"
            }
            logger.info("Skipping build.prop modification")
        
        # Summary - collect any errors and warnings
        errors = []
        warnings = []
        
        for key, r in results.items():
            if isinstance(r, dict):
                if not r.get("success", False):
                    errors.append(f"{key}: {r.get('error', 'Unknown error')}")
                if r.get("warning"):
                    warnings.append(f"{key}: {r.get('warning')}")
        
        # Consider it success if critical components worked (android_id)
        # WiFi MAC failure is acceptable (needs root)
        android_id_ok = results.get("android_id", {}).get("success", False)
        
        return {
            "success": android_id_ok,  # Main requirement: android_id changed
            "results": results,
            "errors": errors if errors else None,
            "warnings": warnings if warnings else None,
            "reboot_required": results.get("build_prop", {}).get("reboot_required", False),
        }
    
    def apply_random_device_profile(self) -> dict[str, Any]:
        """
        Apply a random device profile from preset list.
        
        Useful for quick device spoofing without manual configuration.
        
        Returns:
            dict with applied profile details
        """
        profile = random.choice(self.DEVICE_MODELS)
        
        logger.info(f"Applying random device profile: {profile['model']}")
        
        # Apply build properties
        build_result = self.modify_build_prop(
            model=profile["model"],
            brand=profile["brand"],
            manufacturer=profile["manufacturer"],
        )
        
        # Also randomize Android ID
        id_result = self.randomize_android_id()
        
        return {
            "success": build_result["success"] and id_result["success"],
            "profile": profile,
            "android_id": id_result.get("android_id"),
            "reboot_required": build_result.get("reboot_required", False),
        }


def create_fingerprint_manager(device_ip: str) -> FingerprintManager:
    """
    Create a FingerprintManager for a device.
    
    Args:
        device_ip: Device IP address
        
    Returns:
        FingerprintManager instance
    """
    device = Device(device_ip)
    return FingerprintManager(device)
