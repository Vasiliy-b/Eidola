"""
GPS spoofing module for device location isolation.

Uses io.appium.settings to inject mock GPS coordinates on Android devices.
This allows matching device location to proxy IP geolocation for authenticity.
"""

import logging
import math
import random
from typing import Any

from lamda.client import Device

from ..config import LocationConfig, GeoConfig

logger = logging.getLogger("eidola.device.location")


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


class LocationSpoofer:
    """
    Manages GPS location spoofing for Android devices.
    
    Uses io.appium.settings app to inject mock GPS coordinates.
    Requires io.appium.settings to be installed on the device and
    mock location permissions to be granted.
    
    Methods:
        - enable_mock_location(): Grant mock location permission
        - set_location(): Set specific GPS coordinates
        - set_location_from_geo(): Set location from GeoConfig
        - add_realistic_noise(): Add variance for authenticity
        - verify_location(): Check if mock location is active
    """
    
    APPIUM_SETTINGS_PACKAGE = "io.appium.settings"
    LOCATION_SERVICE = f"{APPIUM_SETTINGS_PACKAGE}/.LocationService"
    
    # Known city coordinates for fallback
    CITY_COORDINATES = {
        "nyc": (40.7128, -74.0060),
        "la": (34.0522, -118.2437),
        "chicago": (41.8781, -87.6298),
        "houston": (29.7604, -95.3698),
        "phoenix": (33.4484, -112.0740),
        "philadelphia": (39.9526, -75.1652),
        "san_antonio": (29.4241, -98.4936),
        "san_diego": (32.7157, -117.1611),
        "dallas": (32.7767, -96.7970),
        "austin": (30.2672, -97.7431),
        "miami": (25.7617, -80.1918),
        "seattle": (47.6062, -122.3321),
        "denver": (39.7392, -104.9903),
        "boston": (42.3601, -71.0589),
        "london": (51.5074, -0.1278),
        "paris": (48.8566, 2.3522),
        "berlin": (52.5200, 13.4050),
        "tokyo": (35.6762, 139.6503),
        "moscow": (55.7558, 37.6173),
    }
    
    def __init__(self, device: Device):
        """
        Initialize location spoofer for a device.
        
        Args:
            device: FIRERPA Device instance
        """
        self.device = device
        self._current_location: tuple[float, float] | None = None
        self._mock_enabled: bool = False
    
    def enable_mock_location(self) -> dict[str, Any]:
        """
        Enable mock location permission for io.appium.settings.
        
        This must be called before setting mock location.
        Requires the device to have io.appium.settings installed.
        
        Returns:
            dict with success status
        """
        try:
            # Grant mock location permission to appium settings
            result = self.device.execute_script(
                f"appops set {self.APPIUM_SETTINGS_PACKAGE} android:mock_location allow"
            )
            
            # Also try the newer permission method
            self.device.execute_script(
                f"pm grant {self.APPIUM_SETTINGS_PACKAGE} android.permission.ACCESS_FINE_LOCATION"
            )
            self.device.execute_script(
                f"pm grant {self.APPIUM_SETTINGS_PACKAGE} android.permission.ACCESS_COARSE_LOCATION"
            )
            
            self._mock_enabled = True
            logger.info("Enabled mock location permission for io.appium.settings")
            
            return {
                "success": True,
                "message": "Mock location enabled",
                "package": self.APPIUM_SETTINGS_PACKAGE,
            }
            
        except Exception as e:
            logger.error(f"Failed to enable mock location: {e}")
            return {"success": False, "error": str(e)}
    
    def set_location(
        self,
        latitude: float,
        longitude: float,
        altitude: float = 10.0,
        accuracy: float = 1.0,
    ) -> dict[str, Any]:
        """
        Set mock GPS location on the device.
        
        Uses io.appium.settings LocationService to inject coordinates.
        
        Args:
            latitude: GPS latitude (-90 to 90)
            longitude: GPS longitude (-180 to 180)
            altitude: Altitude in meters (default 10m)
            accuracy: Location accuracy in meters (default 1m)
            
        Returns:
            dict with success status and coordinates
        """
        # Validate coordinates
        if not -90 <= latitude <= 90:
            return {"success": False, "error": f"Invalid latitude: {latitude}"}
        if not -180 <= longitude <= 180:
            return {"success": False, "error": f"Invalid longitude: {longitude}"}
        
        try:
            # Enable mock location if not already done
            if not self._mock_enabled:
                enable_result = self.enable_mock_location()
                if not enable_result.get("success"):
                    return enable_result
            
            # Start the location service with coordinates
            # io.appium.settings uses --es for string extras
            cmd = (
                f"am start-foreground-service --user 0 "
                f"-n {self.LOCATION_SERVICE} "
                f"--es longitude {longitude} "
                f"--es latitude {latitude} "
                f"--es altitude {altitude}"
            )
            
            result = self.device.execute_script(cmd)
            
            self._current_location = (latitude, longitude)
            
            logger.info(f"Set mock location: lat={latitude}, lon={longitude}")
            
            return {
                "success": True,
                "latitude": latitude,
                "longitude": longitude,
                "altitude": altitude,
                "accuracy": accuracy,
            }
            
        except Exception as e:
            logger.error(f"Failed to set mock location: {e}")
            return {"success": False, "error": str(e)}
    
    def set_location_from_geo(
        self,
        geo: GeoConfig,
        add_noise: bool = True,
        variance_meters: int = 500,
    ) -> dict[str, Any]:
        """
        Set mock location from GeoConfig.
        
        Args:
            geo: GeoConfig with latitude and longitude
            add_noise: Whether to add realistic variance (default True)
            variance_meters: Maximum variance in meters (default 500m)
            
        Returns:
            dict with success status and actual coordinates
        """
        lat = geo.latitude
        lon = geo.longitude
        
        if add_noise:
            lat, lon = self.add_realistic_noise(lat, lon, variance_meters)
        
        return self.set_location(lat, lon)
    
    def set_location_from_city(
        self,
        city: str,
        add_noise: bool = True,
        variance_meters: int = 5000,  # Larger variance for city-level
    ) -> dict[str, Any]:
        """
        Set mock location from city name.
        
        Uses predefined city coordinates with optional variance.
        
        Args:
            city: City name (lowercase, e.g., 'nyc', 'la')
            add_noise: Whether to add realistic variance (default True)
            variance_meters: Maximum variance in meters (default 5km)
            
        Returns:
            dict with success status and coordinates
        """
        city_lower = city.lower().replace(" ", "_")
        
        if city_lower not in self.CITY_COORDINATES:
            return {
                "success": False,
                "error": f"Unknown city: {city}",
                "known_cities": list(self.CITY_COORDINATES.keys()),
            }
        
        lat, lon = self.CITY_COORDINATES[city_lower]
        
        if add_noise:
            lat, lon = self.add_realistic_noise(lat, lon, variance_meters)
        
        return self.set_location(lat, lon)
    
    def add_realistic_noise(
        self,
        latitude: float,
        longitude: float,
        variance_meters: int = 500,
    ) -> tuple[float, float]:
        """
        Add realistic GPS noise to coordinates.
        
        GPS coordinates aren't perfectly stable in real life.
        This adds realistic variance to make location more authentic.
        
        Args:
            latitude: Base latitude
            longitude: Base longitude
            variance_meters: Maximum variance in meters
            
        Returns:
            tuple of (noisy_lat, noisy_lon)
        """
        # Convert meters to approximate degrees
        # 1 degree latitude ≈ 111km
        # 1 degree longitude varies by latitude (≈ 111km * cos(lat))
        
        lat_variance_deg = variance_meters / 111_000
        lon_variance_deg = variance_meters / (111_000 * math.cos(math.radians(latitude)))
        
        # Random offset with Gaussian distribution (more realistic)
        lat_offset = random.gauss(0, lat_variance_deg / 3)  # /3 for 99.7% within range
        lon_offset = random.gauss(0, lon_variance_deg / 3)
        
        # Clamp to variance bounds
        lat_offset = max(-lat_variance_deg, min(lat_variance_deg, lat_offset))
        lon_offset = max(-lon_variance_deg, min(lon_variance_deg, lon_offset))
        
        noisy_lat = latitude + lat_offset
        noisy_lon = longitude + lon_offset
        
        # Clamp to valid ranges
        noisy_lat = max(-90, min(90, noisy_lat))
        noisy_lon = max(-180, min(180, noisy_lon))
        
        logger.debug(
            f"Added GPS noise: ({latitude}, {longitude}) -> "
            f"({noisy_lat:.6f}, {noisy_lon:.6f})"
        )
        
        return noisy_lat, noisy_lon
    
    def stop_location_spoofing(self) -> dict[str, Any]:
        """
        Stop the mock location service.
        
        Returns:
            dict with success status
        """
        try:
            self.device.execute_script(
                f"am stopservice -n {self.LOCATION_SERVICE}"
            )
            
            self._current_location = None
            logger.info("Stopped mock location service")
            
            return {"success": True, "message": "Location spoofing stopped"}
            
        except Exception as e:
            logger.error(f"Failed to stop location spoofing: {e}")
            return {"success": False, "error": str(e)}
    
    def verify_location(self) -> dict[str, Any]:
        """
        Verify if mock location is active.
        
        Checks if the io.appium.settings LocationService is running.
        
        Returns:
            dict with verification status
        """
        try:
            # Check if location service is running
            result = self.device.execute_script(
                f"dumpsys activity services {self.LOCATION_SERVICE}"
            )
            output = _get_shell_output(result)
            
            is_active = "ServiceRecord" in output if output else False
            
            return {
                "success": True,
                "mock_active": is_active,
                "current_location": self._current_location,
                "service": self.LOCATION_SERVICE,
            }
            
        except Exception as e:
            logger.error(f"Failed to verify location: {e}")
            return {"success": False, "error": str(e)}
    
    def get_device_location(self) -> dict[str, Any]:
        """
        Get the device's current location (real or mocked).
        
        Uses dumpsys to query location providers.
        
        Returns:
            dict with location data
        """
        try:
            result = self.device.execute_script(
                "dumpsys location | grep -A5 'last location'"
            )
            output = _get_shell_output(result)
            
            return {
                "success": True,
                "raw_output": output[:500] if output else None,
                "mock_location": self._current_location,
            }
            
        except Exception as e:
            logger.error(f"Failed to get device location: {e}")
            return {"success": False, "error": str(e)}
    
    def generate_location_from_ip(
        self,
        ip_lat: float,
        ip_lon: float,
        variance_meters: int = 1000,
    ) -> tuple[float, float]:
        """
        Generate GPS coordinates based on IP geolocation.
        
        Creates coordinates near the IP location with realistic variance.
        This is useful for matching GPS to proxy IP geolocation.
        
        Args:
            ip_lat: Latitude from IP geolocation
            ip_lon: Longitude from IP geolocation
            variance_meters: Maximum deviation from IP location
            
        Returns:
            tuple of (gps_lat, gps_lon)
        """
        return self.add_realistic_noise(ip_lat, ip_lon, variance_meters)
    
    def apply_location_config(self, config: LocationConfig, geo: GeoConfig) -> dict[str, Any]:
        """
        Apply location configuration to device.
        
        Convenience method that handles the full location setup flow.
        
        Args:
            config: LocationConfig with spoofing settings
            geo: GeoConfig with target coordinates
            
        Returns:
            dict with success status and location details
        """
        if not config.enabled:
            return {"success": True, "message": "Location spoofing disabled in config"}
        
        # Only appium_settings method is currently supported
        if config.method != "appium_settings":
            logger.warning(
                f"Location method '{config.method}' not supported, "
                f"using appium_settings"
            )
        
        return self.set_location_from_geo(
            geo=geo,
            add_noise=True,
            variance_meters=config.variance_meters,
        )


def create_location_spoofer(device_ip: str) -> LocationSpoofer:
    """
    Create a LocationSpoofer for a device.
    
    Args:
        device_ip: Device IP address
        
    Returns:
        LocationSpoofer instance
    """
    device = Device(device_ip)
    return LocationSpoofer(device)
