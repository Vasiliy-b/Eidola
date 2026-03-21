"""Device content uploader via FIRERPA.

Uploads uniqualized media files to Android device, refreshes MediaStore
so Instagram can see them, and verifies visibility.
"""

import json
import logging
import time
from io import BytesIO
from pathlib import Path

from .models import ContentVariant, DeviceManifest, PostingState, VariantStatus
from .mongo_content import ContentStore

logger = logging.getLogger("eidola.content.device_uploader")

DEVICE_POSTING_DIR = "/sdcard/DCIM/ToPost"


def upload_content_to_device(
    device,
    variant: ContentVariant,
    content_item,
    store: ContentStore | None = None,
) -> bool:
    """Upload uniqualized content to device for posting.

    Steps:
    1. Clean previous posting folder
    2. Create directory
    3. Upload media files
    4. Upload manifest.json (agent reads this)
    5. Refresh MediaStore (critical — Instagram won't see files otherwise)
    6. Verify files exist on device

    Args:
        device: FIRERPA device instance (lamda.client.Device)
        variant: ContentVariant with media paths and caption
        content_item: ContentItem for type/flow info
        store: Optional ContentStore for status updates

    Returns:
        True if all files uploaded and verified successfully.
    """
    content_id = variant.content_id
    account_id = variant.account_id

    try:
        # 1. Clean previous files
        logger.info("[%s] Cleaning %s", account_id, DEVICE_POSTING_DIR)
        device.execute_script(f"rm -rf {DEVICE_POSTING_DIR}/*")
        time.sleep(0.5)

        # 2. Create directory
        device.execute_script(f"mkdir -p {DEVICE_POSTING_DIR}")

        # 3. Upload media files
        uploaded_filenames = []
        for media in variant.media:
            raw_path = media["path"] if isinstance(media, dict) else media.path
            filename = (media.get("filename") if isinstance(media, dict) else media.filename) or ""
            if not filename:
                filename = Path(raw_path).name

            # Resolve relative paths to absolute
            local_path = Path(raw_path)
            if not local_path.is_absolute():
                local_path = local_path.resolve()
            local_str = str(local_path)

            if not local_path.exists():
                raise FileNotFoundError(f"Media file not found: {local_str}")

            remote_path = f"{DEVICE_POSTING_DIR}/{filename}"
            logger.info("[%s] Uploading %s -> %s", account_id, local_str, remote_path)
            device.upload_file(local_str, remote_path)
            uploaded_filenames.append(filename)

        # 4. Upload manifest
        manifest = DeviceManifest(
            content_id=content_id,
            type=content_item.type,
            posting_flow=content_item.posting_flow,
            media=[
                {"filename": f, "order": i + 1, "path": "", "mime": ""}
                for i, f in enumerate(uploaded_filenames)
            ],
            caption=variant.caption,
            account_id=account_id,
        )
        manifest_data = manifest.model_dump(mode="json")
        manifest_json = json.dumps(manifest_data, ensure_ascii=False, indent=2)
        device.upload_fd(
            BytesIO(manifest_json.encode("utf-8")),
            f"{DEVICE_POSTING_DIR}/manifest.json",
        )
        logger.info("[%s] Manifest uploaded", account_id)

        # 5. Refresh MediaStore (CRITICAL)
        _refresh_mediastore(device, uploaded_filenames)

        # 6. Verify
        if not _verify_files(device, uploaded_filenames):
            logger.error("[%s] File verification failed", account_id)
            return False

        logger.info(
            "[%s] Content uploaded: %d files for %s",
            account_id, len(uploaded_filenames), content_id,
        )

        if store:
            store.update_variant_status(content_id, account_id, VariantStatus.ON_DEVICE)

        return True

    except Exception as e:
        logger.error("[%s] Upload failed: %s", account_id, e)
        return False


def cleanup_device_posting_folder(device) -> None:
    """Remove all files from device posting folder after successful post."""
    try:
        device.execute_script(f"rm -rf {DEVICE_POSTING_DIR}/*")
        # Also trigger MediaStore cleanup for deleted files
        device.execute_script(
            "am broadcast -a android.intent.action.MEDIA_MOUNTED "
            "-d file:///sdcard/DCIM"
        )
        logger.info("Cleaned device posting folder")
    except Exception as e:
        logger.warning("Cleanup failed: %s", e)


def _refresh_mediastore(device, filenames: list[str]) -> None:
    """Refresh Android MediaStore so Instagram gallery sees uploaded files.

    Two strategies:
    1. Per-file scan (precise, faster)
    2. Full DCIM rescan (fallback, slower but reliable)
    """
    # Per-file scan
    for filename in filenames:
        remote_path = f"{DEVICE_POSTING_DIR}/{filename}"
        device.execute_script(
            f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
            f"-d file://{remote_path}"
        )

    time.sleep(1.5)

    # Full DCIM rescan as fallback
    device.execute_script(
        "am broadcast -a android.intent.action.MEDIA_MOUNTED "
        "-d file:///sdcard/DCIM"
    )
    time.sleep(2.0)


def _verify_files(device, filenames: list[str]) -> bool:
    """Verify all expected files exist on device."""
    result = device.execute_script(f"ls {DEVICE_POSTING_DIR}/")
    if result.exitstatus != 0:
        return False

    stdout = result.stdout
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    listed = stdout.strip().split("\n")
    listed = [f.strip() for f in listed if f.strip()]

    for filename in filenames:
        if filename not in listed:
            logger.error("Missing file on device: %s (found: %s)", filename, listed)
            return False

    # Also verify manifest
    if "manifest.json" not in listed:
        logger.error("Manifest not found on device")
        return False

    return True
