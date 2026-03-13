"""
Unified Instagram Agent - Single agent with mode-based behavior.

Replaces the 4-agent architecture (Orchestrator, Navigator, Observer, Engager)
with a single agent that has all tools and mode-specific instructions.

Benefits:
- 1 LLM call per action instead of 3-4 (transfer overhead eliminated)
- Simpler code, easier debugging
- Mode configs in YAML for easy customization
"""

import logging
from pathlib import Path
from typing import Any

import yaml
from google.adk.agents import LlmAgent
from google.genai import types

from ..config import settings
from ..tools.firerpa_tools import create_unified_tools
from ..tools.memory_tools import create_memory_tools
from ..tools.auth_tools import create_auth_tools
from ..tools.posting_tools import create_posting_tools
from .callbacks import unified_before_model_callback, compress_tool_response

logger = logging.getLogger("eidola.agents.instagram")


def load_mode_config(mode: str, config_dir: Path = None) -> dict[str, Any]:
    """
    Load mode-specific configuration from YAML.
    
    Args:
        mode: Mode name (feed_scroll, active_engage, nurture_accounts, respond)
        config_dir: Directory containing mode configs. Defaults to config/modes/
        
    Returns:
        Mode configuration dict
    """
    if config_dir is None:
        config_dir = Path(__file__).parent.parent.parent.parent / "config" / "modes"
    
    mode_file = config_dir / f"{mode}.yaml"
    
    if mode_file.exists():
        with open(mode_file, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    
    # Return default config if file doesn't exist
    logger.warning(f"Mode config not found: {mode_file}, using defaults")
    return get_default_mode_config(mode)


def get_default_mode_config(mode: str) -> dict[str, Any]:
    """Get default configuration for a mode."""
    defaults = {
        "feed_scroll": {
            "model": "gemini-3-flash-preview",
            "temperature": 1.0,
            "engagement_rates": {"like": 0.15, "comment": 0.02, "save": 0.05},
            "nurtured_boost": 3.0,
        },
        "active_engage": {
            "model": "gemini-3-flash-preview",
            "temperature": 1.0,
            "engagement_rates": {"like": 0.40, "comment": 0.15, "save": 0.20},
            "nurtured_boost": 2.0,
            "screenshot_for_comments": True,
        },
        "nurture_accounts": {
            "model": "gemini-3-flash-preview",
            "temperature": 1.0,
            "engagement_rates": {"like": 0.95, "comment": 0.70, "save": 0.60},
            "visit_profiles": True,
            "screenshot_for_comments": True,
            "follow_cta": True,
        },
        "respond": {
            "model": "gemini-3-flash-preview",
            "temperature": 1.0,
            "check_notifications": True,
            "check_dms": True,
        },
        "warmup": {
            "model": "gemini-3-flash-preview",
            "temperature": 1.0,
            "session_duration": [10, 40],
            "engagement_rates": {
                "nurtured": {"like": 1.0, "comment": 0.0, "save": 0.10},
                "non_nurtured": {"like": 0.0, "comment": 0.0, "save": 0.0},
            },
            "comment_limits": {"min_per_session": 0, "max_per_session": 0},
            "profile_visits": {
                "enabled": True,
                "accounts_per_session": [1, 2],
                "posts_to_like": [2, 3],
            },
        },
        "login": {
            "model": "gemini-3-flash-preview",
            "temperature": 1.0,
            "engagement_rates": {"like": 0.0, "comment": 0.0, "save": 0.0},
        },
    }
    return defaults.get(mode, defaults["feed_scroll"])


def _build_base_instruction_with_comments() -> str:
    """Build base instruction with comment sections (for active_engage, nurture, feed_scroll)."""
    return """# Instagram Agent

You control Instagram on Android. Trust tool results only — never assume.

## Core Loop
1. `analyze_feed_posts(3)` → returns posts[], recommended_action, target_post
2. Follow recommended_action: "engage"→Step 3, "scroll"→`scroll_feed("normal")`→Step 1, "scroll_to_buttons"→`scroll_to_post_buttons(username)`→Step 1, "recover"→see Recovery
3. Like: video→`tap(like_button.x,y)`, photo/carousel→`double_tap_like()`
4. Media (nurtured only): carousel→`swipe_carousel(username)`, video→`watch_media("video")`
5. Comment (MUST be on post screen): `comment_on_post(author, timestamp)` — opens comments, checks visual dedup, generates+posts. posted=false/skipped=true→move on, never retry.
6. `scroll_feed("normal")`→Step 1

## Compound Tools (USE THESE — save many roundtrips)
- `navigate_to_profile(username)` — goes to any profile from anywhere (search+tap, ~1 call vs ~15)
- `return_to_feed()` — returns to home feed from anywhere (back+tap, ~1 call vs ~13)
- `open_post_and_engage(username, "like")` — opens first post on profile and likes it (~1 call vs ~10)

## Navigation
- `_nav_hint` in every response: `depth=0`=top, `depth>2`=too deep→`press_back()`
- `analyze_feed_posts` auto-escapes profiles and auto-restarts if lost
- Profile visits: `get_next_nurtured_to_visit()`→`navigate_to_profile()`→engage→`record_profile_visit()`→`return_to_feed()`

## Recovery
Dialog→`handle_dialog()` | Lost→`restart_instagram()` | Tool fails 2x→skip via `scroll_feed()` | Stuck 3+ calls→`restart_instagram()`

## ⛔ Rules
- VIP-ONLY: skip non-nurtured. is_liked=true→SKIP (double_tap unlikes!)
- VIDEO: tap(x,y) on like button, NEVER double_tap_like (opens Reels)
- COMMENT: one call only `comment_on_post()`. already_commented→SKIP. posted=false→don't retry
- carousel→`swipe_carousel()`, video→`watch_media()` for nurtured. No account switching.
"""


def _build_base_instruction_no_comments() -> str:
    """Build base instruction WITHOUT comment sections for warmup mode."""
    return """# Instagram Warmup Agent

Likes only, ZERO comments. Trust tool results only.

## Core Loop
1. `analyze_feed_posts(3)` → posts[], recommended_action, target_post (includes is_nurtured, is_liked, post_type)
2. Follow recommended_action: "engage"→like, "scroll"→`scroll_feed("normal")`→Step 1, "recover"→`handle_dialog()`/`restart_instagram()`
3. Like nurtured only: video→`tap(like_button.x,y)`, photo/carousel→`double_tap_like()`
4. Media (nurtured): carousel→`swipe_carousel(username)`, video→`watch_media("video")`
5. `scroll_feed("normal")`→Step 1

## Compound Tools
- `navigate_to_profile(username)` — go to any profile (~1 call vs ~15)
- `return_to_feed()` — back to feed from anywhere (~1 call vs ~13)

## Profile Visits (1-2/session)
`get_next_nurtured_to_visit()`→`navigate_to_profile()`→`follow_nurtured_account()`→like 2-3 posts→`record_profile_visit()`→`return_to_feed()`

## Recovery
Dialog→`handle_dialog()` | Lost→`restart_instagram()` | Tool fails 2x→skip | Stuck 3+→`restart_instagram()`

## ⛔ Rules
- NURTURED ONLY, skip all others. is_liked=true→SKIP (double_tap unlikes!)
- VIDEO→tap(x,y), NEVER double_tap_like (opens Reels). No sponsored.
- ZERO COMMENTS. No auth/login tools. No account switching.
- analyze_feed_posts() already checks everything — don't call check_post_liked/is_nurtured separately.
"""


def build_mode_instruction(mode: str, config: dict[str, Any]) -> str:
    """
    Build the instruction prompt for a specific mode.
    
    Args:
        mode: Mode name
        config: Mode configuration
        
    Returns:
        Complete instruction string for the agent
    """
    engagement = config.get("engagement_rates", {})
    
    if mode == "warmup":
        base = _build_base_instruction_no_comments()
    elif mode == "login":
        base = ""  # Login has self-contained instructions
    else:
        base = _build_base_instruction_with_comments()
    
    # Mode-specific instructions
    mode_instructions = {
        "feed_scroll": f"""
## MODE: Feed Scroll (Casual Browsing)

**Goal**: Browse feed naturally, engage ONLY with VIP accounts.

**Behavior**:
1. `analyze_feed_posts(3)` → get visible posts with nurtured status
2. Follow `recommended_action`:
   - `"engage"` on VIP → like (+ comment/save/share per rates below)
   - `"scroll"` → `scroll_feed("normal")`
3. VIP engagement rates:
   - Like: {engagement.get('like', 0.40) * 100:.0f}%
   - Comment: {engagement.get('comment', 0.10) * 100:.0f}%
   - Save: {engagement.get('save', 0.05) * 100:.0f}%
4. Repeat

⚠️ **NEVER engage with non-VIP accounts!**
""",
        
        "active_engage": f"""
## MODE: Active Engagement

**Goal**: Engage deeply with VIP posts — like, comment, save, share.

**Behavior**:
1. `analyze_feed_posts(3)` → get visible posts
2. Follow `recommended_action`:
   - `"engage"` on VIP → like + comment + save/share per rates
   - `"scroll"` → `scroll_feed("normal")`
   - `"scroll_to_buttons"` → `scroll_to_post_buttons(username)` then retry
3. Engagement rates (VIP only):
   - Like: {engagement.get('like', 0.40) * 100:.0f}%
   - Comment: {engagement.get('comment', 0.15) * 100:.0f}% — use `comment_on_post(author, timestamp)`
   - Save: {engagement.get('save', 0.20) * 100:.0f}% — use `save_post(username)`

⚠️ **NEVER engage with non-VIP accounts!**
""",
        
        "nurture_accounts": f"""
## MODE: Nurture VIP Accounts

**Goal**: Engage deeply with VIP posts — like, comment, save, share.

**Behavior**:
1. Focus on nurtured/VIP accounts only
2. For each VIP post:
   - Like (unless already liked)
   - Comment ({engagement.get('comment', 0.70) * 100:.0f}%)
   - Save ({engagement.get('save', 0.60) * 100:.0f}%) — use `save_post(username)`
   - Share ({engagement.get('share', 0.15) * 100:.0f}%) — use `share_post(username)` → tap "Add post to your story" → `press_back()`
   - ALWAYS follow CTA verbatim

**COMMENTS**: CTA → type verbatim (mandatory, no exceptions). No CTA → natural reaction.
3. Visit profiles if configured
4. Comment naturally based on what you see

**CTA is MANDATORY** for nurtured accounts:
- Post: "Comment 🔥 if you agree" → Your comment: `🔥`
- Post: "Type YES for more" → Your comment: `YES`
- Post: "Drop FIRE below" → Your comment: `FIRE`

**No CTA?** React like a real person — emojis, short reactions, whatever feels natural.
""",
        
        "respond": """
## MODE: Respond to Interactions

**Goal**: Reply to comments and DMs.

**Behavior**:
1. Check notifications
2. Respond to comments on your posts
3. Check and reply to DMs
4. Keep responses brief and friendly

**Response style**:
- Short and warm
- Don't over-explain
- Use emojis sparingly
""",
        
        "warmup": f"""
## MODE: Warmup (Likes Only — NO Comments)

**Goal**: Warm up account safely. Like nurtured posts ONLY. **ZERO comments — ban risk!** Scroll past everything else.

**Session**: {config.get('session_duration', [10, 40])[0]}-{config.get('session_duration', [10, 40])[1]} minutes.

---

### Engagement Rules

| Account Type | Like | Comment | Media | Profile Visit |
|--------------|------|---------|-------|---------------|
| **Nurtured/VIP** | ✅ ALWAYS | ❌ NEVER (warmup!) | ✅ Watch video + swipe carousel | ✅ 1-2 per session |
| **Non-nurtured** | ❌ NEVER | ❌ NEVER | ❌ SKIP | ❌ NEVER |
| **Sponsored/Ad** | ❌ SKIP | ❌ SKIP | ❌ SKIP | ❌ SKIP |

---

### ⚠️ Nurtured Check

`analyze_feed_posts()` already returns `is_nurtured` per post. Trust this flag.
Only call `is_nurtured_account(username)` separately during profile visits or if re-verifying outside feed.

---

### Behavior Flow

**Step 1**: `analyze_feed_posts(3)` → get visible posts with `is_nurtured`, `is_liked`, `recommended_action`

**Step 2**: Follow `recommended_action` from the result:

**If `is_nurtured=true` AND `is_liked=false`**:
1. Like the post (photo/carousel → `double_tap_like()`, video → `tap(like_button.x, y)`)
2. Media engagement: carousel → `swipe_carousel(username)`, video → `watch_media(media_type="video")`
3. `scroll_feed("normal")` → next post

**If `is_nurtured=true` AND `is_liked=true`**:
1. Already liked — do NOT double_tap (it would UNLIKE!)
2. `scroll_feed("normal")` → next post

**If `is_nurtured=false`**:
1. DO NOT like. DO NOT save. DO NOT engage.
2. `scroll_feed("normal")` → next post

**If `is_sponsored=true`**:
1. `scroll_feed("normal")` immediately

---

### Profile Visits ({config.get('profile_visits', {}).get('accounts_per_session', [1, 2])[0]}-{config.get('profile_visits', {}).get('accounts_per_session', [1, 2])[1]} per session) — Rotation

Visit 1-2 nurtured profiles per session using **rotation**:

1. `get_next_nurtured_to_visit()` → returns target username
2. Navigate to their profile:
   a. Tap the Search tab icon (bottom nav bar)
   b. Tap the search input field
   c. Type their **exact username** with `type_text()`
   d. Tap their profile in search results
3. On their profile: `follow_nurtured_account(username)`
   - `already_following=true` or `followed=true` → proceed
   - `followed=false` → UI issue, do NOT retry. Continue to step 4.
4. Scroll their post grid, like 2-3 recent posts (check `is_liked` first to avoid unliking!)
5. `record_profile_visit(username)` → logs the visit for rotation
6. `press_back()` until you return to feed, then `analyze_feed_posts()` to continue

⚠️ **ALWAYS** use `get_next_nurtured_to_visit()` — do NOT pick profiles manually!
⚠️ **ALWAYS** call `follow_nurtured_account()` on every nurtured profile visit!

---

### ⛔ CRITICAL RULES (Warmup-Specific)

1. **ZERO COMMENTS**: Do NOT comment on ANY post. Do NOT call `comment_on_post()`. Do NOT type in comment fields or interact with comment UI elements. Warmup = likes only!
2. **NURTURED ONLY**: NEVER like or save non-nurtured posts. `analyze_feed_posts()` returns `is_nurtured` — trust it. Only call `is_nurtured_account()` during profile visits.
3. **NATURAL SCROLLING**: Non-nurtured posts — pause 1-3s, then scroll. Don't rush.
4. **PROFILE VISITS**: Visit 1-2 nurtured profiles. Scroll grid, like 2-3 posts. No comments.
5. **NO SPONSORED**: Skip all ads/sponsored posts immediately.
""",

        "login": """
## MODE: Login (Authentication Flow)

**Goal**: Log in to Instagram account with 2FA support.

**Login Flow**:

### Step 1: Detect Current Screen
```
detect_screen() → check screen type
```

| Screen Type | Next Action |
|-------------|-------------|
| `instagram_feed` | Already logged in! Done. |
| `instagram_login` | Continue to Step 2 |
| `instagram_2fa` | Continue to Step 3 |
| Other | `open_instagram()` → retry |

### Step 2: Enter Credentials (if on login screen)
```
1. get_account_credentials(account_id) → {username, password, has_2fa}
2. Find username field → tap(x, y) → type_text(username)
3. Find password field → tap(x, y) → type_text(password)
4. Find "Log in" button → tap(x, y)
5. wait_for_idle(3000)
```

### Step 3: Handle 2FA (if required)
**Method Selection Screen** (if "Choose a way to confirm"):
```
1. Find "Authentication app" option → tap(x, y)
2. Find "Continue" button → tap(x, y)
3. wait_for_idle(2000)
```

**Code Entry Screen** (if code input field visible):
```
1. generate_2fa_code(account_id) → {code, valid_for_seconds}
2. If valid_for_seconds < 5, wait 5 seconds, regenerate
3. Find code field (content-desc="Code,") → tap(x, y)
4. type_text(code)
5. wait_for_idle(1500)  # CRITICAL: wait for button to enable
6. Find "Continue/Confirm" button → tap(x, y)
7. wait_for_idle(3000)
```

### Step 4: Verify Success
```
detect_screen() → expect instagram_feed
```

**Tools Available**:
- `get_account_credentials(account_id)` → username, password, has_2fa
- `generate_2fa_code(account_id)` → TOTP code, valid_for_seconds
- `tap(x, y)` → tap at coordinates
- `type_text(text)` → type with human-like delays
- `detect_screen()` → get screen type
- `get_screen_elements()` → get UI elements
- `wait_for_idle(ms)` → wait for UI

**⛔ CRITICAL RULES**:
1. WAIT 1.5s after typing 2FA code before tapping Continue
2. If code `valid_for_seconds < 5`, wait and regenerate
3. 2FA field uses `content-desc="Code,"` NOT text or resource-id
4. Max 3 retries per step
5. If challenge screen appears ("Confirm it's you") → report failure
""",
    }
    
    result = base + mode_instructions.get(mode, mode_instructions["feed_scroll"])

    # Append posting instructions for all engagement modes
    if mode in ("feed_scroll", "active_engage", "nurture_accounts", "warmup"):
        result += POSTING_INSTRUCTION_BLOCK

    return result


POSTING_INSTRUCTION_BLOCK = """

## STEP 0 — POST SCHEDULED CONTENT (runs BEFORE Core Loop)

Your FIRST action every session: call `get_posting_manifest()`.

- `has_content=false` → skip to Core Loop Step 1.
- `has_content=true` → execute the posting flow below. Do NOT browse feed, like, comment, save, or call `analyze_feed_posts()` until posting is complete.

### 0.1 — Read the manifest

From `get_posting_manifest()`, store these values:

| Field | Use |
|---|---|
| `posting_flow` | Which flow to execute: feed_photo | feed_carousel | reel |
| `has_caption` | Boolean — controls whether to write a caption |
| `caption` | Seed text for creative adaptation (only when has_caption=true) |
| `media_count` | Photos to select (only for feed_carousel) |

Ignore content_id and account_id — the reporting system reads them automatically from the stored manifest.

### 0.2 — Navigate to the Create screen

1. `return_to_feed()` — the [+] button only appears at the feed top.
2. `find_element("create_button")` → if found, `tap(center_x, center_y)`.
3. If not found: `tap(66, 154)` — fixed coordinates for the top-left [+] near the Instagram logo.
4. If still not on the Create screen: tap Profile tab → find and tap [+] in the profile action bar.

**Verify:** call `get_elements_for_ai()` — confirm POST / REEL / STORY tabs and a media gallery are visible. If absent, retry the next approach.

### 0.3 — Caption rules

Read `has_caption` from the manifest. Exactly one of two paths applies:

---

**`has_caption=true` → CREATIVE ADAPTATION**

Tap the caption field, then call `type_text()` with an original caption you compose. The manifest caption is a **seed theme only** — extract its subject or mood, then write something entirely new in your own voice. Do not reference, quote, or rearrange the seed's wording.

**Transformation process:**

1. Identify the core theme or mood of the manifest caption (one or two abstract words — e.g. "fairy tale anticipation", "cooking success", "fitness pride").
2. Generate a fresh phrase that captures that theme. Vary your vocabulary every single time — never fall back on stock phrases.
3. Append 1–2 emojis that match the mood. Select emojis you have not used in recent captions.

**Seed-type rules:**

- Seed is a question → respond with a playful statement or a different question on the same theme
- Seed is a word or short phrase → expand into a casual, conversational thought (5–12 words)
- Seed is a sentence → condense into a punchy fragment or flip the perspective
- Seed is only emojis → you may keep them unchanged, or prepend 2–4 words

**Variation dimensions (rotate across posts):**

- Tone: playful, dreamy, bold, reflective, witty, mysterious, deadpan, warm
- Form: question, exclamation, ellipsis trail-off, punchy fragment, two-part line
- Length: 3–12 words

**Hard constraints:**

- Preserve the core theme of the manifest caption
- Every word you type must be your own — never paste, quote, or rearrange the manifest caption's exact wording
- Never add hashtags unless the manifest caption already contains them
- Never repeat the same emoji more than twice in a single caption
- Never produce more than 4 emojis total in a single caption
- If `type_text()` returns `blocked=true`, rephrase your text completely and retry once

---

**`has_caption=false` → DO NOTHING**

Do not tap the caption field. Do not call `type_text()`. Do not invent any text. Proceed directly to the Share button. An empty caption is intentional. A hard safety guard in `type_text()` will block any input when the manifest caption is empty, so attempting to type is both wrong and futile.

---

### 0.4 — Execute by posting_flow

**feed_photo** (single image):
1. Tap "POST" tab if not already active
2. Tap the first photo in Recents gallery
3. Tap "Next" (top-right)
4. Skip filters → tap "Next"
5. Caption step — apply rules from section 0.3
6. Tap "Share"

**feed_carousel** (multiple images):
1. Tap "POST" tab if not already active
2. Tap "Select Multiple"
3. Tap `media_count` photos from Recents
4. Tap "Next" → skip filters → tap "Next"
5. Caption step — apply rules from section 0.3
6. Tap "Share"

**reel** (video):
1. Tap "REEL" tab
2. Tap the video in Recents
3. Tap "Next" → tap "Next"
4. Caption step — apply rules from section 0.3
5. Tap "Share"

### 0.5 — Confirm and report

After tapping Share: `wait_for_idle(5000)` → `get_elements_for_ai()`.

**Success** — feed screen or "shared" confirmation visible:
`report_posting_result(success=true)`

**Failure** — error or unexpected screen:
`report_posting_result(success=false, error_message="brief description of failure")`

The complete call signature is `success` and optionally `error_message`. No other parameters. Do NOT pass content_id, account_id, or any ID — the system reads them from the stored manifest.

After reporting (success or failure), proceed to Core Loop Step 1.

### Posting constraints

- Media files are pre-loaded on the device in Recents/Gallery. You do not upload anything.
- Dismiss popups ("OK" / "Not Now" / "Done" / "Allow") by tapping the visible button.
- Do NOT swipe right from the feed — that opens the Stories camera, not post creation.
- `has_caption=false` means ZERO text. Not ".", not " ", not a space, not a single emoji. Skip the caption field entirely.
- `has_caption=true` means creative adaptation via `type_text()`. Never copy the manifest caption word-for-word.
- `report_posting_result()` accepts ONLY `success` and optionally `error_message`. Never fabricate or pass IDs.

⛔ CRITICAL FAILURE: If `has_content=true` and you skip to feed browsing without completing the posting flow and calling `report_posting_result()`, the entire session is a failure. You MUST post first.
"""



# screenshot_injector_callback removed — replaced by unified_before_model_callback
# in agents/callbacks.py which handles context trimming, image cleanup, screenshot
# injection, and mode-specific tool filtering in a single callback.


def create_instagram_agent(
    device_ip: str | None = None,
    mode: str = "feed_scroll",
    mode_config: dict[str, Any] | None = None,
) -> LlmAgent:
    """
    Create a unified Instagram agent for the specified mode.
    
    Args:
        device_ip: FIRERPA device IP
        mode: Operating mode (feed_scroll, active_engage, nurture_accounts, respond)
        mode_config: Mode configuration (overrides YAML config)
        
    Returns:
        Configured LlmAgent with all tools
    """
    ip = device_ip or settings.firerpa_device_ip
    
    # Load mode config
    config = mode_config or load_mode_config(mode)
    
    # Get model and temperature from config
    model = config.get("model", settings.default_model)
    temperature = config.get("temperature", 1.0)  # Google recommends 1.0 for Gemini
    
    # Build instruction
    instruction = build_mode_instruction(mode, config)
    
    # Get tools - all modes get unified + memory + posting tools
    # Login mode additionally gets auth tools (2FA, credentials)
    all_tools = create_unified_tools(ip)
    memory_tools = create_memory_tools()
    posting_tools = create_posting_tools()
    
    if mode == "login":
        auth_tools = create_auth_tools()
    else:
        auth_tools = []

    logger.info(
        f"🎯 Creating Instagram agent: mode={mode}, model={model}, temp={temperature}, "
        f"tools={len(all_tools) + len(memory_tools) + len(auth_tools) + len(posting_tools)}"
    )
    
    return LlmAgent(
        name="InstagramAgent",
        model=model,
        instruction=instruction,
        description=f"Unified Instagram agent in {mode} mode",
        tools=all_tools + memory_tools + auth_tools + posting_tools,
        generate_content_config=types.GenerateContentConfig(
            temperature=temperature,
            top_p=0.95,
            max_output_tokens=65535,
            thinking_config=types.ThinkingConfig(
                thinking_level="MINIMAL",
            ),
        ),
        before_model_callback=unified_before_model_callback,
        after_tool_callback=compress_tool_response,
        sub_agents=[],
    )


# create_unified_tools is now imported from firerpa_tools
