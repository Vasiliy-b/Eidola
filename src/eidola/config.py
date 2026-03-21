"""Configuration settings for Eidola using Pydantic Settings."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Google Cloud / Vertex AI
    google_cloud_project: str = Field(
        default="",
        description="Google Cloud project ID for Vertex AI",
    )
    google_cloud_location: str = Field(
        default="us-central1",
        description="Google Cloud region for Vertex AI",
    )

    # FIRERPA Device
    firerpa_device_ip: str = Field(
        default="192.168.1.100",
        description="IP address of the FIRERPA device",
    )
    firerpa_mcp_timeout: int = Field(
        default=30,
        description="Timeout for FIRERPA MCP connections in seconds",
    )

    # MongoDB
    mongo_uri: str = Field(
        default="mongodb://localhost:27017",
        description="MongoDB connection URI",
    )
    mongo_db_name: str = Field(
        default="eidola",
        description="MongoDB database name",
    )

    # Telegram Bot
    telegram_bot_token: str = Field(
        default="",
        description="Telegram bot token from @BotFather",
    )
    telegram_bot_api_url: str = Field(
        default="",
        description="Local Bot API Server URL (for files >50MB), e.g. http://localhost:8081",
    )

    # Content Distribution
    content_dir: str = Field(
        default="./data/content",
        description="Directory for content storage (originals + variants)",
    )

    # Alerts — sent to the same chat where SMM operates
    telegram_alert_chat_id: int = Field(
        default=0,
        description="Telegram chat ID for admin/error alerts (0 = disabled). Same as SMM chat.",
    )

    # Cleanup
    cleanup_variants_after_hours: int = Field(
        default=48,
        description="Delete local variant files after N hours post-posting",
    )
    cleanup_content_expiry_days: int = Field(
        default=14,
        description="Expire content items older than N days",
    )
    disk_alert_threshold_gb: float = Field(
        default=10.0,
        description="Send alert when free disk space drops below this (GB)",
    )

    # Agent Settings
    default_model: str = Field(
        default="gemini-3-flash-preview",
        description="Default LLM model for agents (Observer, Engager - need multimodal)",
    )
    fast_model: str = Field(
        default="gemini-3-flash-preview",
        description="Fast model for Navigator and summarizer",
    )
    orchestrator_model: str | None = Field(
        default=None,
        description="Override model for Orchestrator (defaults to default_model)",
    )
    navigator_model: str | None = Field(
        default=None,
        description="Override model for Navigator (defaults to fast_model)",
    )
    observer_model: str | None = Field(
        default=None,
        description="Override model for Observer (defaults to default_model)",
    )
    engager_model: str | None = Field(
        default=None,
        description="Override model for Engager (defaults to default_model)",
    )
    comment_model: str | None = Field(
        default=None,
        description="Model for comment generation in comment_on_post (defaults to default_model). Env: COMMENT_MODEL",
    )
    orchestrator_temperature: float = Field(
        default=1.0,
        description="Temperature for Orchestrator LLM",
    )
    navigator_temperature: float = Field(
        default=1.0,
        description="Temperature for Navigator LLM (Google recommends 1.0 for Gemini 3)",
    )
    observer_temperature: float = Field(
        default=1.0,
        description="Temperature for Observer LLM",
    )
    engager_temperature: float = Field(
        default=1.0,
        description="Temperature for Engager LLM",
    )
    session_max_actions: int = Field(
        default=50,
        description="Maximum actions per session",
    )
    session_max_likes_per_hour: int = Field(
        default=30,
        description="Maximum likes per hour",
    )
    session_max_comments_per_hour: int = Field(
        default=10,
        description="Maximum comments per hour",
    )
    
    # ==========================================================================
    # Session Limits (GramAddict-style)
    # ==========================================================================
    # These control engagement behavior for natural patterns.
    # Ranges are [min, max] - actual value is randomized within range.
    # ==========================================================================
    
    # Per-session limits
    session_likes_limit_min: int = Field(
        default=120,
        description="Minimum likes per session",
    )
    session_likes_limit_max: int = Field(
        default=150,
        description="Maximum likes per session",
    )
    session_comments_limit_min: int = Field(
        default=3,
        description="Minimum comments per session",
    )
    session_comments_limit_max: int = Field(
        default=5,
        description="Maximum comments per session",
    )
    session_stories_limit_min: int = Field(
        default=120,
        description="Minimum story watches per session",
    )
    session_stories_limit_max: int = Field(
        default=150,
        description="Maximum story watches per session",
    )
    
    # Interaction probabilities (percentage)
    stories_percentage_min: int = Field(
        default=30,
        description="Minimum percentage of accounts to watch stories",
    )
    stories_percentage_max: int = Field(
        default=40,
        description="Maximum percentage of accounts to watch stories",
    )
    carousel_percentage_min: int = Field(
        default=60,
        description="Minimum percentage of carousel items to swipe",
    )
    carousel_percentage_max: int = Field(
        default=70,
        description="Maximum percentage of carousel items to swipe",
    )
    
    # Watch durations (seconds)
    watch_video_time_min: int = Field(
        default=15,
        description="Minimum video watch duration (seconds)",
    )
    watch_video_time_max: int = Field(
        default=35,
        description="Maximum video watch duration (seconds)",
    )
    watch_photo_time_min: int = Field(
        default=3,
        description="Minimum photo view duration (seconds)",
    )
    watch_photo_time_max: int = Field(
        default=4,
        description="Maximum photo view duration (seconds)",
    )

    # ==========================================================================
    # Context Management
    # ==========================================================================
    # Primary: ADK EventsCompactionConfig (summarizes old events via LLM)
    # Safety net: WindowedSessionService (hard event limit + XML/screenshot compression)
    # ==========================================================================
    
    context_max_contents: int = Field(
        default=15,
        description="Maximum conversation contents to send to LLM (before_model_callback trims older ones)",
    )
    
    # ADK Context Cache (ContextCacheConfig)
    # Caches system prompts and repeated content to reduce token usage
    context_cache_min_tokens: int = Field(
        default=1024,
        description="Minimum tokens to enable context caching",
    )
    context_cache_ttl: int = Field(
        default=21600,  # 6 hours (proactive refresh handles long sessions)
        description="Context cache TTL in seconds",
    )
    context_cache_intervals: int = Field(
        default=10,
        description="Max cache uses before refresh",
    )
    
    # Legacy settings kept for backwards compatibility (not actively used)
    session_max_turns: int = Field(
        default=10,
        description="[LEGACY] Maximum conversation turns - now handled by context_max_contents",
    )
    session_max_events: int = Field(
        default=30,
        description="[LEGACY] Maximum events - now handled by context_max_contents",
    )
    session_compress_xml: bool = Field(
        default=True,
        description="[LEGACY] XML compression - now handled by tools directly",
    )

    # Paths - use absolute path relative to this file
    prompts_dir: Path = Field(
        default=Path(__file__).parent.parent.parent / "prompts",
        description="Directory containing prompt files",
    )
    comment_styles_path: Path = Field(
        default=Path(__file__).parent.parent.parent / "config" / "comment_styles.yaml",
        description="Path to comment style configuration",
    )
    session_limits_path: Path = Field(
        default=Path(__file__).parent.parent.parent / "config" / "session_limits.yaml",
        description="Path to session limits configuration (GramAddict-style)",
    )

    @property
    def firerpa_mcp_url(self) -> str:
        """Get the full FIRERPA MCP URL."""
        return f"http://{self.firerpa_device_ip}:65000/firerpa/mcp/"

    @property
    def vertex_ai_model(self) -> str:
        """Get the full Vertex AI model path."""
        return f"projects/{self.google_cloud_project}/locations/{self.google_cloud_location}/publishers/google/models/{self.default_model}"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


def _get_default_limits(settings: Settings) -> dict:
    """Return default session limits from settings."""
    return {
        "session": {
            "total_likes_limit": [settings.session_likes_limit_min, settings.session_likes_limit_max],
            "total_comments_limit": [settings.session_comments_limit_min, settings.session_comments_limit_max],
            "total_watches_limit": [settings.session_stories_limit_min, settings.session_stories_limit_max],
        },
        "probabilities": {
            "stories_percentage": [settings.stories_percentage_min, settings.stories_percentage_max],
            "carousel_percentage": [settings.carousel_percentage_min, settings.carousel_percentage_max],
        },
        "durations": {
            "watch_video_time": [settings.watch_video_time_min, settings.watch_video_time_max],
            "watch_photo_time": [settings.watch_photo_time_min, settings.watch_photo_time_max],
        },
    }


def get_session_limits() -> dict:
    """Load session limits from YAML config file.
    
    Returns parsed YAML as dict. Values with [min, max] ranges
    should be randomized when used.
    
    Returns:
        dict with session limits configuration
    """
    import yaml
    
    settings = get_settings()
    
    if not settings.session_limits_path.exists():
        return _get_default_limits(settings)
    
    try:
        with open(settings.session_limits_path, "r", encoding="utf-8") as f:
            result = yaml.safe_load(f)
            # Handle empty file case
            if result is None:
                return _get_default_limits(settings)
            return result
    except yaml.YAMLError as e:
        # Log warning and return defaults on malformed YAML
        import logging
        logging.getLogger("eidola.config").warning(f"Failed to parse session_limits.yaml: {e}")
        return _get_default_limits(settings)


def get_random_limit(limits_dict: dict, path: str) -> int | float:
    """Get random value from a limit range.
    
    Args:
        limits_dict: Session limits dictionary from get_session_limits()
        path: Dot-separated path like "session.total_likes_limit"
        
    Returns:
        Random value within the specified range, or 0 on invalid path
    """
    import random
    
    if limits_dict is None:
        return 0
    
    parts = path.split(".")
    value = limits_dict
    
    for part in parts:
        if not isinstance(value, dict):
            return 0  # Path traversal failed
        value = value.get(part)
        if value is None:
            return 0  # Key not found
    
    if isinstance(value, list) and len(value) == 2:
        min_val, max_val = value[0], value[1]
        # Auto-correct inverted ranges
        if min_val > max_val:
            min_val, max_val = max_val, min_val
        if isinstance(min_val, float) or isinstance(max_val, float):
            return random.uniform(min_val, max_val)
        return random.randint(int(min_val), int(max_val))
    
    if isinstance(value, (int, float)):
        return value
    
    return 0  # Unknown type


# Convenience alias
settings = get_settings()


# =============================================================================
# FLEET CONFIGURATION MODELS
# =============================================================================
# These models define the structure for multi-account, multi-device fleet management.
# =============================================================================

from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class InstagramCredentials(BaseModel):
    """Instagram account credentials."""
    username: str
    password_env: str  # Reference to .env variable
    totp_secret_env: Optional[str] = None  # Reference to .env for TOTP secret


class AccountSettings(BaseModel):
    """Per-account behavior settings."""
    engagement_multiplier: float = 1.0
    can_comment: bool = True
    can_like: bool = True
    can_follow: bool = True


class AccountMetadata(BaseModel):
    """Account metadata."""
    created_at: str
    notes: str = ""
    status: str = "active"  # active | paused | banned | warming_up


class AccountConfig(BaseModel):
    """Full account configuration."""
    account_id: str
    instagram: InstagramCredentials
    assigned_device: str
    persona: str = "prompts/persona/default_persona.md"
    schedule_override: Optional[dict] = None
    mode_override: Optional[str] = None
    settings: AccountSettings = AccountSettings()
    metadata: AccountMetadata


class ProxyConfig(BaseModel):
    """Proxy configuration for device isolation.
    
    IMPORTANT: Uses global proxy mode (per_app_only=False) because:
    1. FIRERPA per-app proxy only supports ONE app (not multiple)
    2. Global proxy routes ALL device traffic through proxy
    3. Apps must be stopped BEFORE proxy starts to use new routing
    """
    enabled: bool = True
    type: str = "http-connect"  # http-connect | socks5 | https-connect
    host: str = "gate.decodo.com"
    port: int = 10001
    username_env: str = "DECODO_USERNAME"
    password_env: str = "DECODO_PASSWORD"
    session_duration: int = 30  # minutes
    drop_udp: bool = True  # Block UDP to force all traffic through TCP proxy
    bypass_local_subnet: bool = True
    dns_proxy: bool = True  # Route DNS through proxy to prevent DNS leaks
    nameserver: str = "8.8.8.8"  # Google DNS for proxied DNS queries
    per_app_only: bool = False  # Use GLOBAL proxy - routes ALL device traffic
    # Apps to stop before proxy start - ALL apps that might leak IP
    # Must include browsers, Instagram, and any other network apps
    target_apps: list[str] = [
        "com.instagram.android",
        "com.android.chrome",           # Chrome browser
        "com.android.browser",          # Stock Android browser
        "mark.via",                     # Via browser
        "org.chromium.webview_shell",   # WebView
    ]


class FingerprintConfig(BaseModel):
    """Device fingerprint configuration."""
    android_id: Optional[str] = None
    wifi_mac: Optional[str] = None
    build_model: str = "SM-G998B"
    build_brand: str = "samsung"
    build_manufacturer: str = "samsung"


class GeoConfig(BaseModel):
    """Geographic configuration for proxy and GPS."""
    country: str = "us"
    country_code: str = "US"
    city: str = "nyc"
    latitude: float = 40.7128
    longitude: float = -74.0060
    timezone: str = "America/New_York"  # IANA timezone matching the geo location


class LocationConfig(BaseModel):
    """GPS spoofing configuration."""
    enabled: bool = True
    method: str = "appium_settings"  # appium_settings | magisk | xposed
    variance_meters: int = 500
    update_interval_seconds: int = 30


class HealthConfig(BaseModel):
    """Device health monitoring configuration."""
    connectivity_check_interval: int = 5  # minutes
    app_stuck_timeout: int = 60  # seconds
    auto_reboot_on_critical: bool = False


class DeviceMetadata(BaseModel):
    """Device metadata."""
    created_at: str
    notes: str = ""
    status: str = "active"  # active | maintenance | offline


class DeviceConfig(BaseModel):
    """Full device configuration."""
    device_id: str
    device_ip: str
    device_name: str = ""
    adb_serial: str | None = None  # ADB serial for USB operations (WiFi MAC changes)
    geo: GeoConfig = GeoConfig()
    accounts: list[str] = []  # Account IDs assigned to this device
    proxy: ProxyConfig = ProxyConfig()
    fingerprint: FingerprintConfig = FingerprintConfig()
    location: LocationConfig = LocationConfig()
    health: HealthConfig = HealthConfig()
    metadata: DeviceMetadata


class RotationConfig(BaseModel):
    """Account rotation configuration."""
    strategy: str = "random_within_schedule"  # sequential | random | random_within_schedule
    min_session_minutes: int = 15
    max_session_minutes: int = 40
    break_between_accounts_minutes: int = 5


class FleetProxyConfig(BaseModel):
    """Fleet-wide proxy provider configuration."""
    provider: str = "decodo"
    host: str = "gate.decodo.com"
    port: int = 10001
    username_env: str = "DECODO_USERNAME"
    password_env: str = "DECODO_PASSWORD"


class FleetConfig(BaseModel):
    """Fleet-wide configuration."""
    name: str = "my_fleet"
    devices: list[DeviceConfig] = []
    proxy: FleetProxyConfig = FleetProxyConfig()
    rotation: RotationConfig = RotationConfig()


# =============================================================================
# FLEET CONFIGURATION LOADERS
# =============================================================================

import os
import yaml
from pathlib import Path


def load_account_config(account_id: str) -> AccountConfig | None:
    """Load account configuration from YAML file.
    
    Args:
        account_id: Account identifier (filename without .yaml)
        
    Returns:
        AccountConfig if found, None otherwise
    """
    config_dir = Path(__file__).parent.parent.parent / "config" / "accounts"
    config_path = config_dir / f"{account_id}.yaml"
    
    if not config_path.exists():
        return None
    
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    
    return AccountConfig(**data)


def load_device_config(device_id: str) -> DeviceConfig | None:
    """Load device configuration from YAML file.
    
    Args:
        device_id: Device identifier (filename without .yaml)
        
    Returns:
        DeviceConfig if found, None otherwise
    """
    config_dir = Path(__file__).parent.parent.parent / "config" / "devices"
    config_path = config_dir / f"{device_id}.yaml"
    
    if not config_path.exists():
        return None
    
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    
    return DeviceConfig(**data)


def load_all_accounts() -> list[AccountConfig]:
    """Load all account configurations from config/accounts/ directory.
    
    Returns:
        List of AccountConfig objects
    """
    config_dir = Path(__file__).parent.parent.parent / "config" / "accounts"
    accounts = []
    
    if not config_dir.exists():
        return accounts
    
    for yaml_file in config_dir.glob("*.yaml"):
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            accounts.append(AccountConfig(**data))
        except Exception as e:
            import logging
            logging.getLogger("eidola.config").warning(
                f"Failed to load account config {yaml_file}: {e}"
            )
    
    return accounts


def load_all_devices() -> list[DeviceConfig]:
    """Load all device configurations from config/devices/ directory.
    
    Returns:
        List of DeviceConfig objects
    """
    config_dir = Path(__file__).parent.parent.parent / "config" / "devices"
    devices = []
    
    if not config_dir.exists():
        return devices
    
    for yaml_file in config_dir.glob("*.yaml"):
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            devices.append(DeviceConfig(**data))
        except Exception as e:
            import logging
            logging.getLogger("eidola.config").warning(
                f"Failed to load device config {yaml_file}: {e}"
            )
    
    return devices


def load_fleet_config() -> FleetConfig:
    """Load fleet configuration from config/fleet.yaml.
    
    If fleet.yaml doesn't exist, builds FleetConfig from individual device configs.
    
    Returns:
        FleetConfig object
    """
    config_path = Path(__file__).parent.parent.parent / "config" / "fleet.yaml"
    
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return FleetConfig(**data)
    
    # Build from individual device configs
    devices = load_all_devices()
    return FleetConfig(devices=devices)


def get_account_password(account_config: AccountConfig) -> str:
    """Get account password from environment variable.
    
    Args:
        account_config: Account configuration
        
    Returns:
        Password string or empty string if not found
    """
    return os.environ.get(account_config.instagram.password_env, "")


def get_account_totp_secret(account_config: AccountConfig) -> str | None:
    """Get account TOTP secret from environment variable.
    
    Args:
        account_config: Account configuration
        
    Returns:
        TOTP secret string or None if not configured
    """
    if not account_config.instagram.totp_secret_env:
        return None
    return os.environ.get(account_config.instagram.totp_secret_env)


# =============================================================================
# GMAIL CONFIGURATION (for Play Store login, etc.)
# =============================================================================

class GmailCredentials(BaseModel):
    """Gmail account credentials."""
    email: str
    password_env: str  # Reference to .env variable
    totp_secret_env: str  # Reference to .env for TOTP secret


class GmailMetadata(BaseModel):
    """Gmail account metadata."""
    created_at: str
    notes: str = ""


class GmailConfig(BaseModel):
    """Gmail account configuration for device."""
    account_id: str
    device_id: str
    gmail: GmailCredentials
    metadata: GmailMetadata


def load_gmail_config(device_id: str) -> GmailConfig | None:
    """Load Gmail configuration for a device.
    
    Args:
        device_id: Device identifier (e.g., 'phone_01')
        
    Returns:
        GmailConfig if found, None otherwise
    """
    config_dir = Path(__file__).parent.parent.parent / "config" / "gmail"
    config_path = config_dir / f"{device_id}.yaml"
    
    if not config_path.exists():
        return None
    
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    
    return GmailConfig(**data)


def load_gmail_config_by_account_id(account_id: str) -> GmailConfig | None:
    """Load Gmail configuration by account_id.
    
    Args:
        account_id: Gmail account identifier (e.g., 'gmail_phone_01')
        
    Returns:
        GmailConfig if found, None otherwise
    """
    config_dir = Path(__file__).parent.parent.parent / "config" / "gmail"
    
    if not config_dir.exists():
        return None
    
    for yaml_file in config_dir.glob("*.yaml"):
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data.get("account_id") == account_id:
                return GmailConfig(**data)
        except Exception:
            continue
    
    return None


def load_all_gmail_configs() -> list[GmailConfig]:
    """Load all Gmail configurations from config/gmail/ directory.
    
    Returns:
        List of GmailConfig objects
    """
    config_dir = Path(__file__).parent.parent.parent / "config" / "gmail"
    configs = []
    
    if not config_dir.exists():
        return configs
    
    for yaml_file in config_dir.glob("*.yaml"):
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            configs.append(GmailConfig(**data))
        except Exception as e:
            import logging
            logging.getLogger("eidola.config").warning(
                f"Failed to load Gmail config {yaml_file}: {e}"
            )
    
    return configs


def get_gmail_password(gmail_config: GmailConfig) -> str:
    """Get Gmail password from environment variable."""
    return os.environ.get(gmail_config.gmail.password_env, "")


def get_gmail_totp_secret(gmail_config: GmailConfig) -> str | None:
    """Get Gmail TOTP secret from environment variable."""
    if not gmail_config.gmail.totp_secret_env:
        return None
    return os.environ.get(gmail_config.gmail.totp_secret_env)
