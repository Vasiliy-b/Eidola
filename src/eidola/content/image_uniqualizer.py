"""Image uniqualization using FFmpeg.

Image uniqualization for content distribution.
Applies random visual transforms to create visually distinct copies
while preserving content: color shifts, flip, crop, noise, metadata cleanup.
"""

import json
import logging
import os
import random
import subprocess
from typing import Any

logger = logging.getLogger("eidola.content.image_uniqualizer")

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff")

# EXIF Orientation tag (1-8) → FFmpeg filters that correct pixel orientation.
# Applied with -noautorotate so we handle rotation manually (prevents double correction).
EXIF_ORIENTATION_FILTERS: dict[int, list[str]] = {
    2: ["hflip"],
    3: ["hflip", "vflip"],
    4: ["vflip"],
    5: ["transpose=0"],
    6: ["transpose=1"],
    7: ["transpose=3"],
    8: ["transpose=2"],
}


class ImageUniqualizerError(Exception):
    pass


class ImageUnikalizer:

    def __init__(self):
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise ImageUniqualizerError("FFmpeg not found!")

        self.has_exiftool = self._check_exiftool()
        if not self.has_exiftool:
            logger.info("ExifTool not found — metadata postprocessing disabled")

    @staticmethod
    def _check_exiftool() -> bool:
        try:
            subprocess.run(["exiftool", "-ver"], capture_output=True, text=True, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _get_exif_orientation_exiftool(self, input_path: str) -> int:
        """Read EXIF Orientation via ExifTool — most reliable method.

        ExifTool handles edge cases that our binary parser and ffprobe miss
        (non-standard APP marker ordering, XMP tiff:Orientation, etc.).
        Returns 1-8 on success, 0 if ExifTool unavailable or tag absent.
        """
        if not self.has_exiftool:
            return 0
        try:
            r = subprocess.run(
                ["exiftool", "-Orientation", "-n", "-s3", input_path],
                capture_output=True, text=True, check=False,
            )
            val = int(r.stdout.strip())
            return val if 1 <= val <= 8 else 0
        except (ValueError, TypeError, OSError):
            return 0

    @staticmethod
    def _get_exif_orientation(input_path: str) -> int:
        """Read EXIF Orientation (1-8) directly from JPEG bytes.

        Returns 1-8 when the tag is found, 0 when orientation is unknown
        (not JPEG, no EXIF APP1, no Orientation tag in IFD, parse error).
        Returning 0 lets the caller fall through to the ffprobe-rotation
        fallback instead of silently assuming "Normal".
        """
        import struct
        try:
            with open(input_path, "rb") as f:
                data = f.read(65536)

            if data[:2] != b"\xff\xd8":
                return 0

            offset = 2
            while offset < len(data) - 4:
                if data[offset] != 0xFF:
                    return 0
                marker = data[offset + 1]
                if marker == 0xE1:
                    length = int.from_bytes(data[offset + 2 : offset + 4], "big")
                    app1 = data[offset + 4 : offset + 2 + length]
                    if not app1.startswith(b"Exif\x00\x00"):
                        offset += 2 + length
                        continue
                    tiff = app1[6:]
                    if tiff[:2] == b"II":
                        endian = "<"
                    elif tiff[:2] == b"MM":
                        endian = ">"
                    else:
                        return 0
                    ifd_off = struct.unpack(endian + "I", tiff[4:8])[0]
                    n_entries = struct.unpack(endian + "H", tiff[ifd_off : ifd_off + 2])[0]
                    for i in range(min(n_entries, 50)):
                        eo = ifd_off + 2 + i * 12
                        if eo + 12 > len(tiff):
                            break
                        tag = struct.unpack(endian + "H", tiff[eo : eo + 2])[0]
                        if tag == 0x0112:
                            val = struct.unpack(endian + "H", tiff[eo + 8 : eo + 10])[0]
                            return val if 1 <= val <= 8 else 0
                    return 1
                elif marker in (0xDA, 0xD9):
                    break
                else:
                    length = int.from_bytes(data[offset + 2 : offset + 4], "big")
                    offset += 2 + length
                    continue
        except Exception as e:
            logger.debug("EXIF read failed: %s", e)
        return 0

    def generate_random_params(self) -> dict[str, Any]:
        return {
            "flip_horizontal": random.choice([True, False]),
            "brightness": random.uniform(-0.05, 0.05),
            "contrast": random.uniform(0.95, 1.05),
            "saturation": random.uniform(0.95, 1.05),
            "gamma": random.uniform(0.95, 1.05),
            "hue_shift": random.uniform(-8, 8),
            "rotate": random.uniform(-0.3, 0.3),
            "scale": random.uniform(0.98, 1.02),
            "pixel_offset_x": random.randint(-4, 4),
            "pixel_offset_y": random.randint(-4, 4),
            "color_mix_strength": random.uniform(0.01, 0.03),
            "noise_strength": random.choice([1, 1, 2]),
            "micro_crop_pad": True,
            "border_size": 0,
            "border_color": "0x000000",
            "resolution_delta": random.choice([-4, 0, 0, 4]),
            "quality": random.randint(90, 96),
        }

    def get_image_info(self, input_path: str) -> dict[str, Any]:
        try:
            cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", input_path]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            info = json.loads(result.stdout)
            stream = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
            if not stream:
                raise ImageUniqualizerError("Image stream not found")

            width = int(stream["width"])
            height = int(stream["height"])

            exif_orientation = self._get_exif_orientation_exiftool(input_path)
            if exif_orientation == 0:
                exif_orientation = self._get_exif_orientation(input_path)
            raw_exif = exif_orientation

            ffprobe_rotation = 0
            for sd in stream.get("side_data_list", []):
                if "rotation" in sd:
                    ffprobe_rotation = int(sd["rotation"])
                    break
            if ffprobe_rotation == 0:
                tags = stream.get("tags", {})
                try:
                    ffprobe_rotation = int(tags.get("rotate", "0"))
                except (ValueError, TypeError):
                    ffprobe_rotation = 0

            if exif_orientation == 0 and ffprobe_rotation != 0:
                abs_rot = abs(ffprobe_rotation)
                if abs_rot == 180:
                    exif_orientation = 3
                elif ffprobe_rotation in (-90, 270):
                    exif_orientation = 6
                elif ffprobe_rotation in (90, -270):
                    exif_orientation = 8
                logger.info(
                    "EXIF absent, using ffprobe rotation %d → orientation %d",
                    ffprobe_rotation, exif_orientation,
                )
            if exif_orientation == 0:
                exif_orientation = 1

            if exif_orientation in (5, 6, 7, 8):
                width, height = height, width

            logger.info(
                "Image probe: %dx%d, exif_tag=%d, ffprobe_rotation=%d, final_orientation=%d",
                width, height, raw_exif, ffprobe_rotation, exif_orientation,
            )
            return {
                "width": width,
                "height": height,
                "rotation": abs(ffprobe_rotation),
                "exif_orientation": exif_orientation,
            }
        except ImageUniqualizerError:
            raise
        except Exception as e:
            raise ImageUniqualizerError(f"Error analyzing image: {e}")

    def _build_filters(self, params: dict[str, Any], img_info: dict[str, Any]) -> list[str]:
        filters: list[str] = []
        w, h = img_info["width"], img_info["height"]

        orient = img_info.get("exif_orientation", 1)
        orient_filters = EXIF_ORIENTATION_FILTERS.get(orient, [])
        if orient_filters:
            filters.extend(orient_filters)
            logger.info("Orientation correction: orient=%d → %s", orient, orient_filters)

        if params.get("flip_horizontal"):
            filters.append("hflip")

        eq_parts = []
        if params.get("brightness", 0) != 0:
            eq_parts.append(f"brightness={params['brightness']}")
        if params.get("contrast", 1.0) != 1.0:
            eq_parts.append(f"contrast={params['contrast']}")
        if params.get("saturation", 1.0) != 1.0:
            eq_parts.append(f"saturation={params['saturation']}")
        if params.get("gamma", 1.0) != 1.0:
            eq_parts.append(f"gamma={params['gamma']}")
        if eq_parts:
            filters.append(f"eq={':'.join(eq_parts)}")

        if params.get("hue_shift", 0) != 0:
            filters.append(f"hue=h={params['hue_shift']}")

        mix = params.get("color_mix_strength", 0)
        if mix > 0:
            main = round(1.0 - mix, 4)
            bleed = round(mix / 2.0, 4)
            filters.append(
                f"colorchannelmixer={main}:{bleed}:{bleed}:0:"
                f"{bleed}:{main}:{bleed}:0:"
                f"{bleed}:{bleed}:{main}:0"
            )

        if abs(params.get("rotate", 0)) > 0.01:
            filters.append(
                f"rotate={params['rotate'] * 3.14159 / 180}:fillcolor=black:ow=iw:oh=ih"
            )

        if params.get("scale", 1.0) != 1.0:
            new_w = int(w * params["scale"])
            new_h = int(h * params["scale"])
            filters.append(f"scale={new_w}:{new_h}:flags=lanczos,scale={w}:{h}:flags=lanczos")

        ox = params.get("pixel_offset_x", 0)
        oy = params.get("pixel_offset_y", 0)
        if ox != 0 or oy != 0:
            abs_ox, abs_oy = abs(ox), abs(oy)
            crop_x = abs_ox + ox
            crop_y = abs_oy + oy
            filters.append(
                f"pad=iw+{abs_ox * 2}:ih+{abs_oy * 2}:{abs_ox}:{abs_oy}:color=black,"
                f"crop=iw-{abs_ox * 2}:ih-{abs_oy * 2}:{crop_x}:{crop_y}"
            )
            filters.append(f"scale={w}:{h}:flags=lanczos")

        if params.get("micro_crop_pad"):
            ct, cb = random.randint(1, 3), random.randint(1, 3)
            cl, cr_ = random.randint(1, 3), random.randint(1, 3)
            filters.append(f"crop=iw-{cl + cr_}:ih-{ct + cb}:{cl}:{ct}")
            filters.append(f"scale={w}:{h}:flags=lanczos")

        ns = max(0, int(params.get("noise_strength", 0)))
        if ns > 0:
            filters.append(f"noise=alls={ns}:allf=t+u")

        border_size = params.get("border_size", 0)
        delta = params.get("resolution_delta", 0)
        if border_size > 0:
            color = params.get("border_color", "0x1A1A1A")
            filters.append(
                f"pad=iw+{border_size * 2}:ih+{border_size * 2}:"
                f"{border_size}:{border_size}:color={color}"
            )
            out_w = w + delta
            out_h = h + delta
            out_w = out_w if out_w % 2 == 0 else out_w - 1
            out_h = out_h if out_h % 2 == 0 else out_h - 1
            filters.append(f"scale={out_w}:{out_h}:flags=lanczos")
        elif delta != 0:
            out_w = w + delta
            out_h = h + delta
            out_w = out_w if out_w % 2 == 0 else out_w - 1
            out_h = out_h if out_h % 2 == 0 else out_h - 1
            filters.append(f"scale={out_w}:{out_h}:flags=lanczos")

        return filters

    def uniqualize_image(
        self,
        input_path: str,
        output_path: str,
        custom_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not os.path.exists(input_path):
            raise ImageUniqualizerError(f"Input file not found: {input_path}")

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        img_info = self.get_image_info(input_path)
        logger.info("Image info: %s", img_info)
        params = custom_params if custom_params else self.generate_random_params()
        logger.info("Params: flip=%s, rotate=%.3f, scale=%.3f", 
                     params.get("flip_horizontal"), params.get("rotate", 0), params.get("scale", 1))
        filters = self._build_filters(params, img_info)
        quality = params.get("quality", 92)

        cmd = ["ffmpeg", "-y", "-noautorotate", "-i", input_path]
        if filters:
            cmd.extend(["-vf", ",".join(filters)])
        cmd.extend(["-map_metadata", "-1"])

        ext = os.path.splitext(output_path)[1].lower()
        if ext in (".jpg", ".jpeg"):
            cmd.extend(["-q:v", str(max(1, min(31, int((100 - quality) * 31 / 100))))])
        elif ext == ".webp":
            cmd.extend(["-quality", str(quality)])

        cmd.append(output_path)

        logger.info("Processing image: %s -> %s", input_path, output_path)
        logger.info("FFmpeg filters: %s", ",".join(filters) if filters else "none")
        logger.info("FFmpeg cmd: %s", " ".join(cmd))
        process = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if process.returncode != 0:
            if os.path.exists(output_path):
                os.remove(output_path)
            raise ImageUniqualizerError(f"FFmpeg error: {process.stderr[-500:]}")

        if self.has_exiftool:
            from .metadata import apply_android_image_metadata
            apply_android_image_metadata(output_path, img_info["width"], img_info["height"])
            subprocess.run(
                ["exiftool", "-m", "-P", "-overwrite_original",
                 "-Orientation#=1", output_path],
                capture_output=True, check=False,
            )

        return {
            "success": True,
            "input_path": input_path,
            "output_path": output_path,
            "params": params,
            "image_info": img_info,
        }

    def _postprocess_metadata(self, output_path: str, img_info: dict) -> None:
        """Clean metadata and add iPhone-like EXIF."""
        try:
            subprocess.run(
                ["exiftool", "-m", "-P", "-overwrite_original", "-all=", output_path],
                capture_output=True, text=True, check=False,
            )
            model, ios_ver = random.choice([
                ("iPhone 15 Pro", "18.3"),
                ("iPhone 15 Pro Max", "18.2.1"),
                ("iPhone 16 Pro", "18.3"),
                ("iPhone 16 Pro Max", "18.2"),
                ("iPhone 16", "18.3.1"),
                ("iPhone 14 Pro", "18.3"),
            ])
            subprocess.run(
                [
                    "exiftool", "-m", "-P", "-overwrite_original",
                    "-Make=Apple", f"-Model={model}", f"-Software={ios_ver}",
                    "-DateTimeOriginal<FileModifyDate",
                    "-CreateDate<FileModifyDate",
                    "-ModifyDate<FileModifyDate",
                    "-ColorSpace=sRGB",
                    f"-ExifImageWidth={img_info['width']}",
                    f"-ExifImageHeight={img_info['height']}",
                    output_path,
                ],
                capture_output=True, text=True, check=False,
            )
            subprocess.run(
                ["exiftool", "-m", "-P", "-overwrite_original", "-XMP:XMPToolkit=", output_path],
                capture_output=True, text=True, check=False,
            )
        except Exception:
            pass
