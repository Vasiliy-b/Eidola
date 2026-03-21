"""Video uniqualization using FFmpeg with NVENC support.

Video uniqualization for content distribution.
Key addition: h264_nvenc GPU encoding for ~6x faster processing on NVIDIA GPUs.
Falls back to libx264 if NVENC is unavailable.
"""

import json
import logging
import os
import random
import subprocess
import tempfile
from typing import Any

logger = logging.getLogger("eidola.content.video_uniqualizer")


class VideoUniqualizerError(Exception):
    pass


def detect_nvenc() -> bool:
    """Check if h264_nvenc encoder is available."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=True,
        )
        return "h264_nvenc" in result.stdout
    except Exception:
        return False


class VideoUnikalizer:

    def __init__(self, prefer_nvenc: bool = True):
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise VideoUniqualizerError("FFmpeg not found!")

        self.use_nvenc = prefer_nvenc and detect_nvenc()
        if self.use_nvenc:
            logger.info("NVENC encoder detected — using GPU acceleration")
        else:
            logger.info("Using libx264 CPU encoder")

        self.has_exiftool = self._check_exiftool()
        if not self.has_exiftool:
            logger.info("ExifTool not found — metadata postprocessing disabled")

        self.supports_fft3d = False
        try:
            fl = subprocess.run(
                ["ffmpeg", "-hide_banner", "-filters"],
                capture_output=True, text=True, check=True,
            )
            out = (fl.stdout or "") + (fl.stderr or "")
            self.supports_fft3d = "fft3dfilter" in out
        except Exception:
            pass

    @staticmethod
    def _check_exiftool() -> bool:
        try:
            subprocess.run(["exiftool", "-ver"], capture_output=True, text=True, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def get_video_info(self, input_path: str) -> dict[str, Any]:
        try:
            cmd = [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", input_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            info = json.loads(result.stdout)

            video_stream = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
            audio_stream = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)

            if not video_stream:
                raise VideoUniqualizerError("Video stream not found")

            duration = float(info["format"]["duration"])
            width = int(video_stream["width"])
            height = int(video_stream["height"])

            rotation = 0
            for sd in video_stream.get("side_data_list", []):
                if "rotation" in sd:
                    rotation = abs(int(sd["rotation"]))
                    break
            if rotation == 0:
                tags = video_stream.get("tags", {})
                try:
                    rotation = abs(int(tags.get("rotate", "0")))
                except (ValueError, TypeError):
                    rotation = 0
            if rotation in (90, 270):
                width, height = height, width

            return {
                "duration": duration,
                "width": width,
                "height": height,
                "fps": eval(video_stream["r_frame_rate"]),
                "has_audio": audio_stream is not None,
                "video_codec": video_stream["codec_name"],
                "audio_codec": audio_stream["codec_name"] if audio_stream else None,
            }
        except Exception as e:
            raise VideoUniqualizerError(f"Error analyzing video: {e}")

    def generate_random_params(self, duration: float, has_audio: bool = True) -> dict[str, Any]:
        params = {
            "flip_horizontal": random.choice([True, False]),
            "speed_factor": random.uniform(0.93, 1.07),
            "brightness": random.uniform(-0.06, 0.06),
            "contrast": random.uniform(0.94, 1.06),
            "saturation": random.uniform(0.95, 1.05),
            "gamma": random.uniform(0.94, 1.06),
            "hue_shift": random.uniform(-8, 8),
            "trim_start": random.uniform(0.3, 0.8),
            "trim_end": random.uniform(0.3, 0.8),
            "noise_strength": random.choice([1, 1, 2, 2, 3]),
            "pitch_shift": random.uniform(-0.8, 0.8) if has_audio else 0,
            "rotate": random.uniform(-1.0, 1.0),
            "scale": random.uniform(0.96, 1.04),
            "variable_speed": False,
            "audio_strategy": "keep",
            "metadata_mode": "tiktok_clean",
            "freq_mode": "fft3d+unsharp",
            "freq_strength": 0.8,
            "micro_crop_pad": True,
            "pixel_offset_x": random.randint(-12, 12),
            "pixel_offset_y": random.randint(-12, 12),
            "color_mix_strength": random.uniform(0.01, 0.03),
            "gradient_opacity": 0,
            "crf": random.choice([14, 15, 16, 17]),
            "border_size": random.randint(2, 5),
            "border_color": random.choice([
                "0x1A1A1A", "0x0D0D0D", "0x1C1C1E", "0x2C2C2E",
                "0x111111", "0x0A0A0A", "0x1E1E20", "0x252528",
            ]),
            "zoom_direction": "none",
            "zoom_amount": 1.0,
            "output_fps": 30,
            "resolution_delta": random.choice([-16, -8, 0, 8, 16]),
        }
        params["audio_speed"] = params["speed_factor"]
        return params

    def _build_visual_filters(self, params: dict[str, Any], video_info: dict[str, Any]) -> list[str]:
        filters: list[str] = []
        w, h = video_info["width"], video_info["height"]

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
            crop_x, crop_y = abs_ox + ox, abs_oy + oy
            filters.append(
                f"pad=iw+{abs_ox * 2}:ih+{abs_oy * 2}:{abs_ox}:{abs_oy}:color=black,"
                f"crop=iw-{abs_ox * 2}:ih-{abs_oy * 2}:{crop_x}:{crop_y}"
            )
            filters.append(f"scale={w}:{h}:flags=lanczos")

        if params.get("micro_crop_pad"):
            ct = random.randint(2, 8)
            cb = random.randint(2, 8)
            cl = random.randint(2, 8)
            cr_ = random.randint(2, 8)
            filters.append(f"crop=iw-{cl + cr_}:ih-{ct + cb}:{cl}:{ct}")
            filters.append(f"scale={w}:{h}:flags=lanczos")

        freq_strength = float(params.get("freq_strength", 0))
        if freq_strength > 0:
            sigma = 0.6 + 0.2 * max(0.0, min(3.0, freq_strength))
            if self.supports_fft3d:
                filters.append(f"fft3dfilter=sigma={sigma}")
                if "unsharp" in params.get("freq_mode", ""):
                    amount = 0.15 + 0.1 * max(0.0, min(3.0, freq_strength))
                    filters.append(f"unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount={amount:.2f}")
            else:
                amount = 0.10 + 0.1 * max(0.0, min(3.0, freq_strength))
                filters.append(f"unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount={amount:.2f}")

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

        filters.append("format=yuv420p")
        return filters

    def build_ffmpeg_command(
        self, input_path: str, output_path: str,
        params: dict[str, Any], video_info: dict[str, Any],
    ) -> list[str]:
        cmd = ["ffmpeg", "-y", "-i", input_path]

        video_filters: list[str] = []
        if params["speed_factor"] != 1.0:
            video_filters.append(f"setpts={1.0 / params['speed_factor']}*PTS")

        video_filters.extend(self._build_visual_filters(params, video_info))

        if video_filters:
            cmd.extend(["-vf", ",".join(video_filters)])

        audio_strategy = params.get("audio_strategy", "keep")
        if video_info["has_audio"] and audio_strategy != "drop":
            audio_filters = []
            if params.get("pitch_shift", 0) != 0:
                pitch_factor = 2 ** (params["pitch_shift"] / 12.0)
                audio_filters.append(f"rubberband=pitch={pitch_factor}")
            if params.get("audio_speed", 1.0) != 1.0:
                speed = params["audio_speed"]
                if 0.5 <= speed <= 2.0:
                    audio_filters.append(f"atempo={speed}")
                elif speed < 0.5:
                    audio_filters.append(f"atempo=0.5,atempo={speed / 0.5}")
                else:
                    audio_filters.append(f"atempo=2.0,atempo={speed / 2.0}")
            if audio_filters:
                cmd.extend(["-af", ",".join(audio_filters)])

        if params.get("trim_start", 0) > 0:
            cmd.extend(["-ss", str(params["trim_start"])])
        if params.get("trim_end", 0) > 0:
            duration = video_info["duration"] - params.get("trim_start", 0) - params["trim_end"]
            if duration > 0:
                cmd.extend(["-t", str(duration)])

        cmd.extend(["-map_metadata", "-1", "-map_chapters", "-1"])

        crf = str(params.get("crf", 17))
        output_fps = str(params.get("output_fps", 30))

        if self.use_nvenc:
            cmd.extend([
                "-c:v", "h264_nvenc",
                "-preset", "p5",
                "-rc", "vbr",
                "-cq", str(int(crf) + 2),
                "-b:v", "0",
            ])
        else:
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", crf,
            ])

        # Strip encoder fingerprint from H.264 bitstream (SEI NAL unit type 6).
        # Without this, "Writing library: x264 core XXX" or NVENC signature
        # is embedded inside the video stream itself, surviving -map_metadata -1.
        cmd.extend(["-bsf:v", "filter_units=remove_types=6"])

        # Android-like container metadata (not iOS "Core Media")
        cmd.extend([
            "-r", output_fps,
            "-movflags", "+faststart",
            "-brand", "isom",
            "-metadata:s:v:0", "handler_name=VideoHandle",
        ])

        if video_info["has_audio"] and audio_strategy != "drop":
            cmd.extend([
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                "-metadata:s:a:0", "handler_name=SoundHandle",
            ])
        else:
            cmd.extend(["-an"])

        cmd.append(output_path)
        return cmd

    def postprocess_metadata(self, output_path: str, mode: str) -> None:
        if not self.has_exiftool:
            return
        if mode not in ("remove", "tiktok_clean"):
            return
        if mode == "remove":
            return

        def _run(args: list[str]):
            try:
                subprocess.run(args, capture_output=True, text=True, check=True)
            except Exception as e:
                logger.warning("ExifTool step failed: %s", e)

        _run([
            "exiftool", "-m", "-P", "-overwrite_original",
            "-QuickTime:Comment=", "-QuickTime:VidMd5=", "-QuickTime:AigcInfo=",
            "-Keys:all=", "-UserData:all=", "-XMP:all=", "-QuickTime:Encoder=",
            output_path,
        ])
        _run([
            "exiftool", "-m", "-P", "-overwrite_original",
            "-CreateDate<FileModifyDate", "-MediaCreateDate<FileModifyDate",
            "-TrackCreateDate<FileModifyDate", "-ModifyDate<FileModifyDate",
            "-MediaModifyDate<FileModifyDate", "-TrackModifyDate<FileModifyDate",
            output_path,
        ])
        offset = random.randint(1, 3)
        _run([
            "exiftool", "-m", "-P", "-overwrite_original",
            f"-ModifyDate+=0:0:0 0:0:{offset}",
            f"-MediaModifyDate+=0:0:0 0:0:{offset}",
            f"-TrackModifyDate+=0:0:0 0:0:{offset}",
            output_path,
        ])
        _run([
            "exiftool", "-m", "-P", "-overwrite_original",
            "-XMP:all=", output_path,
        ])

    def uniqualize_video(
        self,
        input_path: str,
        output_path: str,
        custom_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not os.path.exists(input_path):
            raise VideoUniqualizerError(f"Input file not found: {input_path}")

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        video_info = self.get_video_info(input_path)
        params = custom_params or self.generate_random_params(
            video_info["duration"], video_info["has_audio"]
        )

        cmd = self.build_ffmpeg_command(input_path, output_path, params, video_info)

        logger.info(
            "Processing video: %s -> %s (%s)", input_path, output_path,
            "NVENC" if self.use_nvenc else "libx264",
        )

        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as lf:
                log_path = lf.name

            with open(log_path, "w") as lf:
                process = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, text=True, check=False)

            with open(log_path) as lf:
                ffmpeg_output = lf.read()
            os.unlink(log_path)

            if process.returncode != 0:
                raise VideoUniqualizerError(
                    f"FFmpeg error (code {process.returncode}):\n{ffmpeg_output[-1000:]}"
                )

            if self.has_exiftool:
                try:
                    from .metadata import apply_android_video_metadata
                    apply_android_video_metadata(output_path)
                except Exception as e:
                    logger.warning("Metadata postprocess failed: %s", e)

            return {
                "success": True,
                "input_path": input_path,
                "output_path": output_path,
                "params": params,
                "video_info": video_info,
                "encoder": "nvenc" if self.use_nvenc else "libx264",
            }

        except Exception as e:
            if os.path.exists(output_path):
                os.remove(output_path)
            raise VideoUniqualizerError(f"Processing error: {e}")
