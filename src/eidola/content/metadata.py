"""Realistic Android device metadata for uniqualized content.

Generates EXIF/QuickTime metadata that looks like it came from a real
Android phone camera. Uses per-device EXIF templates when available
(from config/exif_templates/{device_id}.json), falls back to random devices.
"""

import json
import random
import subprocess
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("eidola.content.metadata")

EXIF_TEMPLATES_DIR = Path(__file__).parent.parent.parent.parent / "config" / "exif_templates"

_template_cache: dict[str, dict] = {}


def load_device_template(device_id: str) -> dict | None:
    """Load per-device EXIF template from config/exif_templates/.

    Returns template dict or None if not found.
    Cached after first load.
    """
    if device_id in _template_cache:
        return _template_cache[device_id]

    path = EXIF_TEMPLATES_DIR / f"{device_id}.json"
    if not path.exists():
        _template_cache[device_id] = None
        return None

    try:
        with open(path, encoding="utf-8") as f:
            template = json.load(f)
        _template_cache[device_id] = template
        logger.debug("Loaded EXIF template for %s: %s %s", device_id,
                      template.get("make", "?"), template.get("model", "?"))
        return template
    except Exception as e:
        logger.warning("Failed to load EXIF template for %s: %s", device_id, e)
        _template_cache[device_id] = None
        return None


def _template_to_device_dict(template: dict) -> dict:
    """Convert a per-device EXIF template to the ANDROID_DEVICES format."""
    return {
        "make": template.get("make", "samsung"),
        "model": template.get("model", "SM-G998B"),
        "software": template.get("software", ""),
        "focal_length": str(template.get("focal_length", "5.9")),
        "focal_length_35mm": str(template.get("focal_length_35mm", "26")),
        "f_number": str(template.get("fnumber", "1.8")),
        "max_aperture": str(template.get("max_aperture", "1.8")),
        "lens_model": template.get("lens_model", ""),
    }

# Realistic Android devices with camera specs
ANDROID_DEVICES = [
    {
        "make": "Xiaomi",
        "model": "MI 8",
        "software": "MI Camera v4.5.003080.0",
        "focal_length": "4.0",
        "focal_length_35mm": "26",
        "f_number": "1.8",
        "max_aperture": "1.8",
        "lens_model": "Xiaomi MI 8 rear camera 4.03mm f/1.8",
    },
    {
        "make": "Xiaomi",
        "model": "Redmi Note 12 Pro",
        "software": "MI Camera v4.7.002830.0",
        "focal_length": "5.43",
        "focal_length_35mm": "24",
        "f_number": "1.9",
        "max_aperture": "1.9",
        "lens_model": "Xiaomi Redmi rear camera 5.43mm f/1.9",
    },
    {
        "make": "samsung",
        "model": "SM-G998B",
        "software": "G998BXXSGHWK4",
        "focal_length": "5.9",
        "focal_length_35mm": "26",
        "f_number": "1.8",
        "max_aperture": "1.8",
        "lens_model": "Samsung S21 Ultra rear camera 5.9mm f/1.8",
    },
    {
        "make": "samsung",
        "model": "SM-S918B",
        "software": "S918BXXU4BWLB",
        "focal_length": "6.3",
        "focal_length_35mm": "23",
        "f_number": "1.7",
        "max_aperture": "1.7",
        "lens_model": "Samsung S23 Ultra rear camera 6.3mm f/1.7",
    },
    {
        "make": "Google",
        "model": "Pixel 8 Pro",
        "software": "Pixel Camera 9.3.065.621816370.15",
        "focal_length": "6.9",
        "focal_length_35mm": "24",
        "f_number": "1.68",
        "max_aperture": "1.68",
        "lens_model": "Google Pixel 8 Pro rear camera 6.9mm f/1.68",
    },
    {
        "make": "OnePlus",
        "model": "CPH2449",
        "software": "OnePlus Camera v6.8.88",
        "focal_length": "5.59",
        "focal_length_35mm": "24",
        "f_number": "1.8",
        "max_aperture": "1.8",
        "lens_model": "OnePlus 11 rear camera 5.59mm f/1.8",
    },
]

ANDROID_VERSIONS = [
    "13", "13", "14", "14", "14", "15",
]


def _random_timestamp_near_now(max_offset_hours: int = 72) -> datetime:
    """Generate a random timestamp within last N hours."""
    offset = random.randint(0, max_offset_hours * 3600)
    return datetime.now(timezone.utc) - timedelta(seconds=offset)


def _random_iso() -> int:
    return random.choice([50, 64, 80, 100, 125, 160, 200, 250, 320, 400, 640, 800])


def _random_exposure() -> str:
    """Realistic exposure time as fraction string."""
    options = [
        "1/30", "1/50", "1/60", "1/100", "1/120", "1/125",
        "1/200", "1/250", "1/500", "1/1000", "1/2000",
    ]
    return random.choice(options)


def _random_white_balance() -> str:
    return random.choice(["Auto", "Auto", "Auto", "Daylight", "Cloudy", "Fluorescent"])


def apply_android_image_metadata(
    output_path: str,
    width: int,
    height: int,
    device: dict | None = None,
    device_id: str | None = None,
    gps_lat: float | None = None,
    gps_lon: float | None = None,
) -> bool:
    """Apply realistic Android EXIF metadata to an image.

    Uses per-device template if device_id is provided and template exists.
    Falls back to device dict or random device.

    Returns True on success, False if exiftool not available.
    """
    if device_id:
        template = load_device_template(device_id)
        if template:
            dev = _template_to_device_dict(template)
        else:
            dev = device or random.choice(ANDROID_DEVICES)
    else:
        dev = device or random.choice(ANDROID_DEVICES)
    ts = _random_timestamp_near_now()
    iso = _random_iso()
    exposure = _random_exposure()
    wb = _random_white_balance()
    android_ver = random.choice(ANDROID_VERSIONS)
    render_offset = random.randint(0, 2)

    ts_str = ts.strftime("%Y:%m:%d %H:%M:%S")
    ts_sub = f"{random.randint(100, 999)}"
    ts_offset = "+00:00"

    # Step 1: Strip everything
    if not _run_exiftool([
        "exiftool", "-m", "-P", "-overwrite_original", "-all=", output_path
    ]):
        return False

    # Step 2: Write comprehensive Android EXIF
    args = [
        "exiftool", "-m", "-P", "-overwrite_original",
        # Device info
        f"-Make={dev['make']}",
        f"-Model={dev['model']}",
        f"-Software={dev['software']}",
        # Timestamps
        f"-DateTimeOriginal={ts_str}",
        f"-CreateDate={ts_str}",
        f"-ModifyDate={ts_str}",
        f"-SubSecTimeOriginal={ts_sub}",
        f"-SubSecTimeDigitized={ts_sub}",
        f"-OffsetTimeOriginal={ts_offset}",
        # Camera settings
        f"-FocalLength={dev['focal_length']}",
        f"-FocalLengthIn35mmFormat={dev['focal_length_35mm']}",
        f"-FNumber={dev['f_number']}",
        f"-MaxApertureValue={dev['max_aperture']}",
        f"-ExposureTime={exposure}",
        f"-ISO={iso}",
        f"-LensModel={dev['lens_model']}",
        f"-LensMake={dev['make']}",
        # Image settings
        f"-ExifImageWidth={width}",
        f"-ExifImageHeight={height}",
        "-ColorSpace=sRGB",
        "-Orientation=1",
        "-ExposureMode=Auto",
        "-ExposureProgram=Program AE",
        f"-WhiteBalance={wb}",
        "-MeteringMode=Multi-segment",
        "-Flash=No Flash",
        "-SceneCaptureType=Standard",
        "-Contrast=Normal",
        "-Saturation=Normal",
        "-Sharpness=Normal",
        # Resolution
        "-XResolution=72",
        "-YResolution=72",
        "-ResolutionUnit=inches",
        # Android-specific
        "-YCbCrPositioning=Centered",
        f"-ImageDescription=",
        f"-UserComment=",
        output_path,
    ]

    if not _run_exiftool(args):
        return False

    # Step 3: Add GPS if provided
    if gps_lat is not None and gps_lon is not None:
        lat_ref = "N" if gps_lat >= 0 else "S"
        lon_ref = "E" if gps_lon >= 0 else "W"
        lat_var = gps_lat + random.uniform(-0.005, 0.005)
        lon_var = gps_lon + random.uniform(-0.005, 0.005)
        _run_exiftool([
            "exiftool", "-m", "-P", "-overwrite_original",
            f"-GPSLatitude={abs(lat_var):.6f}",
            f"-GPSLatitudeRef={lat_ref}",
            f"-GPSLongitude={abs(lon_var):.6f}",
            f"-GPSLongitudeRef={lon_ref}",
            f"-GPSAltitude={random.randint(10, 200)}",
            "-GPSAltitudeRef=Above Sea Level",
            f"-GPSTimeStamp={ts.strftime('%H:%M:%S')}",
            f"-GPSDateStamp={ts.strftime('%Y:%m:%d')}",
            output_path,
        ])

    # Step 4: Clean XMP toolkit trace left by exiftool
    _run_exiftool([
        "exiftool", "-m", "-P", "-overwrite_original",
        "-XMP:XMPToolkit=", output_path,
    ])

    return True


def apply_android_video_metadata(
    output_path: str,
    device_id: str | None = None,
) -> bool:
    """Apply realistic Android video metadata.

    Uses per-device template if device_id is provided and template exists.

    FFmpeg sets container-level metadata (-brand isom, handler VideoHandle/SoundHandle)
    and strips SEI NAL units (-bsf:v filter_units=remove_types=6).
    ExifTool cleans remaining identifiers and sets realistic timestamps.

    Key forensic traces to eliminate:
    - Lavf/Lavs (FFmpeg library) in WritingApplication/CompressorName
    - x264/NVENC encoder strings (handled by FFmpeg SEI removal)
    - Google/TikTok tracking atoms
    - XMP toolkit traces from exiftool itself
    """
    ts = _random_timestamp_near_now()
    render_offset = random.randint(1, 4)

    if device_id:
        template = load_device_template(device_id)
        dev = _template_to_device_dict(template) if template else random.choice(ANDROID_DEVICES)
    else:
        dev = random.choice(ANDROID_DEVICES)

    # Step 1: Remove all identifying metadata (container level)
    if not _run_exiftool([
        "exiftool", "-m", "-P", "-overwrite_original",
        "-QuickTime:Comment=",
        "-QuickTime:VidMd5=",
        "-QuickTime:AigcInfo=",
        "-Keys:all=",
        "-UserData:all=",
        "-XMP:all=",
        "-QuickTime:Encoder=",
        "-QuickTime:CompressorName=",
        "-QuickTime:GoogleHostHeader=",
        "-QuickTime:GooglePingMessage=",
        "-QuickTime:GooglePingURL=",
        "-QuickTime:GoogleSourceData=",
        "-QuickTime:GoogleStartTime=",
        "-QuickTime:GoogleTrackDuration=",
        output_path,
    ]):
        return False

    # Step 2: Set realistic timestamps + Android device info
    ts_str = ts.strftime("%Y:%m:%d %H:%M:%S")
    _run_exiftool([
        "exiftool", "-m", "-P", "-overwrite_original",
        f"-CreateDate={ts_str}",
        f"-MediaCreateDate={ts_str}",
        f"-TrackCreateDate={ts_str}",
        f"-ModifyDate={ts_str}",
        f"-MediaModifyDate={ts_str}",
        f"-TrackModifyDate={ts_str}",
        f"-QuickTime:Make={dev['make']}",
        f"-QuickTime:Model={dev['model']}",
        f"-QuickTime:Software=Android {random.choice(ANDROID_VERSIONS)}",
        output_path,
    ])

    # Step 3: Shift ModifyDate forward (mimics camera save time)
    _run_exiftool([
        "exiftool", "-m", "-P", "-overwrite_original",
        f"-ModifyDate+=0:0:0 0:0:{render_offset}",
        f"-MediaModifyDate+=0:0:0 0:0:{render_offset}",
        f"-TrackModifyDate+=0:0:0 0:0:{render_offset}",
        output_path,
    ])

    # Step 4: Clean XMP toolkit trace left by exiftool itself
    _run_exiftool([
        "exiftool", "-m", "-P", "-overwrite_original",
        "-XMP:all=", output_path,
    ])

    return True


def _run_exiftool(args: list[str]) -> bool:
    """Run exiftool command. Returns False if not installed."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            logger.debug("ExifTool returned %d: %s", result.returncode, result.stderr[:200])
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        logger.debug("ExifTool error: %s", e)
        return True
