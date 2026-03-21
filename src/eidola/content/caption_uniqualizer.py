"""Caption uniqualization using Gemini.

Generates N unique variations of a post caption while preserving
meaning, tone, CTAs, and approximate length.
"""

import json
import logging
import re
from typing import Any

from google import genai
from google.genai import types

from ..config import settings

logger = logging.getLogger("eidola.content.caption_uniqualizer")

SYSTEM_PROMPT = """\
You are an Instagram content expert. Generate unique caption variations.

Rules:
- Preserve the original meaning, tone, and approximate length
- Keep all CTAs (calls to action) intact
- Rearrange sentence order, rephrase, swap synonyms
- Vary emoji placement and selection (keep similar count)
- Hashtags: keep most, swap 1-3 for related ones, vary order
- Each variation MUST be clearly different from all others
- Output VALID JSON ONLY, no markdown:
  {"variations": ["variation1", "variation2", ...]}
"""


async def generate_caption_variations(
    caption: str,
    n: int = 40,
    batch_size: int = 20,
) -> list[str]:
    """Generate N unique caption variations using Gemini.
    
    Splits into batches if N > batch_size to stay within output limits.
    Returns exactly N variations (pads with original if Gemini underdelivers).
    """
    client = genai.Client(
        vertexai=True,
        project=settings.google_cloud_project,
        location=settings.google_cloud_location or "global",
    )

    all_variations: list[str] = []
    remaining = n

    while remaining > 0:
        batch_n = min(remaining, batch_size)
        prompt = (
            f"Original Instagram caption:\n\n{caption.strip()}\n\n"
            f"Generate {batch_n} unique variations."
        )

        try:
            response = await client.aio.models.generate_content(
                model=settings.default_model,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                config=types.GenerateContentConfig(
                    temperature=1.0,
                    top_p=0.95,
                    max_output_tokens=8192,
                    system_instruction=[types.Part.from_text(text=SYSTEM_PROMPT)],
                ),
            )

            raw = response.text.strip()
            raw = re.sub(r"^```json\s*|^```\s*|```$", "", raw, flags=re.MULTILINE).strip()
            data = json.loads(raw)
            batch_variations = data.get("variations", [])

            if not isinstance(batch_variations, list):
                logger.warning("Gemini returned non-list, using original caption")
                batch_variations = []

            all_variations.extend(batch_variations)
            remaining -= batch_n

        except Exception as e:
            logger.error("Caption uniqualization failed: %s", e)
            break

    # Pad with original if we got fewer than requested
    while len(all_variations) < n:
        all_variations.append(caption.strip())

    return all_variations[:n]


def generate_caption_variations_sync(
    caption: str,
    n: int = 40,
    batch_size: int = 20,
) -> list[str]:
    """Synchronous wrapper for caption generation."""
    client = genai.Client(
        vertexai=True,
        project=settings.google_cloud_project,
        location=settings.google_cloud_location or "global",
    )

    all_variations: list[str] = []
    remaining = n

    while remaining > 0:
        batch_n = min(remaining, batch_size)
        prompt = (
            f"Original Instagram caption:\n\n{caption.strip()}\n\n"
            f"Generate {batch_n} unique variations."
        )

        try:
            response = client.models.generate_content(
                model=settings.default_model,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                config=types.GenerateContentConfig(
                    temperature=1.0,
                    top_p=0.95,
                    max_output_tokens=8192,
                    system_instruction=[types.Part.from_text(text=SYSTEM_PROMPT)],
                ),
            )

            raw = response.text.strip()
            raw = re.sub(r"^```json\s*|^```\s*|```$", "", raw, flags=re.MULTILINE).strip()
            data = json.loads(raw)
            batch_variations = data.get("variations", [])

            if not isinstance(batch_variations, list):
                batch_variations = []

            all_variations.extend(batch_variations)
            remaining -= batch_n

        except Exception as e:
            logger.error("Caption uniqualization failed (sync): %s", e)
            break

    while len(all_variations) < n:
        all_variations.append(caption.strip())

    return all_variations[:n]
