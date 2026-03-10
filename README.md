# Eidola

AI-powered Instagram automation on real Android devices. Uses Google ADK agents with Gemini to control a fleet of phones via [FIRERPA/lamda](https://github.com/nicejb/nicejb) — not API hacks, but actual behavioral emulation.

Each agent sees the screen (XML tree + screenshots), makes decisions (like, comment, scroll, skip), and acts through taps and gestures. Human-like scheduling, proxy isolation, fingerprint spoofing, and content pipeline included.

## Why this exists

Social media platforms are black boxes. Algorithms decide who gets seen and who doesn't. SMM automation tools that fight this are either:
- **Closed-source SaaS** charging $100-500/month (DoubleSpeed raised $2M from a16z for essentially this)
- **API-based bots** that get detected in hours

This project takes a different approach: **open-source behavioral automation on real devices**. The same tech that companies sell behind paywalls — free, inspectable, forkable.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Fleet Scheduler                    │
│  (schedule.yaml → daily plan → session rotation)     │
├──────────┬──────────┬──────────┬──────────┬─────────┤
│ phone_01 │ phone_02 │ phone_03 │   ...    │phone_10 │
│ 5 accs   │ 5 accs   │ 5 accs   │          │ 5 accs  │
├──────────┴──────────┴──────────┴──────────┴─────────┤
│              Per-Device Session Loop                  │
│  1. Setup isolation (proxy + fingerprint + GPS)       │
│  2. Rotate account                                   │
│  3. Create AI agent (Google ADK + Gemini)            │
│  4. Agent runs session (browse, like, comment)       │
│  5. Close Instagram, break, next account             │
├─────────────────────────────────────────────────────┤
│                  Instagram Agent                     │
│  ┌─────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ Screen  │→│  Gemini LLM  │→│  FIRERPA SDK  │  │
│  │ Reader  │  │  (decides)    │  │  (taps/swipes) │ │
│  └─────────┘  └──────────────┘  └───────────────┘  │
├─────────────────────────────────────────────────────┤
│                  Device Isolation                     │
│  Proxy (HTTP CONNECT) + GPS Spoof + Fingerprint      │
├─────────────────────────────────────────────────────┤
│                  Content Pipeline                     │
│  Telegram Bot → Uniqualize → Distribute → Post       │
└─────────────────────────────────────────────────────┘
```

### How the agent "sees" and "acts"

1. **Screen reading**: FIRERPA dumps the Android UI tree as XML. The agent parses it to find posts, buttons, usernames, timestamps.
2. **Visual analysis**: For comments, the agent takes a screenshot and sends it to Gemini for multimodal understanding of the post content.
3. **Decision making**: Based on mode config (engagement rates, nurtured account list), the agent decides: like? comment? scroll? skip?
4. **Action execution**: Taps, swipes, and text input through FIRERPA SDK — actual touch events on the device screen.

### Agent modes

| Mode | Behavior |
|------|----------|
| `active_engage` | Full engagement — like, comment, save VIP posts |
| `warmup` | Likes only, zero comments (for new/restricted accounts) |
| `feed_scroll` | Casual browsing with minimal engagement |
| `nurture_accounts` | Aggressive engagement with priority accounts |
| `respond` | Reply to comments and DMs |
| `login` | Authentication flow with 2FA support |

### Human-like scheduling

The scheduler mimics real phone usage patterns with archetype-based sessions:

- **Wake-up check** (8:15 ±30min) — quick glance, 8-15 min
- **Morning commute** (8:50 ±20min) — scroll on the subway
- **Lunch scroll** (12:30 ±30min) — longer session during lunch
- **Evening browse** (21:00 ±30min) — prime Instagram time
- **Can't sleep** (00:15 ±15min) — late-night doomscrolling (25% chance)

Each session has probability, jitter, energy levels (low/normal/high day), and daily budget (~4 hours with variance).

## Tech Stack

| Component | Technology |
|-----------|------------|
| AI Framework | [Google ADK](https://github.com/google/adk-python) (Agent Development Kit) |
| LLM | Gemini 3 Flash (via Vertex AI) |
| Android RPA | [FIRERPA/lamda](https://github.com/nicejb/nicejb) |
| Database | MongoDB 7 |
| Config | Pydantic + YAML |
| Telegram Bot | aiogram v3 |
| Auth | pyotp (TOTP 2FA) |

## Quick Start

### Prerequisites

- Python 3.11+
- Android device(s) with [FIRERPA](https://github.com/nicejb/nicejb) installed
- Google Cloud project with Vertex AI enabled
- MongoDB (via Docker or standalone)

### Setup

```bash
# Clone
git clone https://github.com/Vasiliy-b/Eidola.git
cd Eidola

# Virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# MongoDB
docker-compose up -d mongodb

# Configure
cp .env.example .env
# Edit .env with your credentials
```

### Configuration

1. **`.env`** — API keys, passwords, proxy credentials
2. **`config/accounts/*.yaml`** — Instagram accounts (one per file)
3. **`config/devices/*.yaml`** — Android device configs (IP, proxy, geo)
4. **`config/gmail/*.yaml`** — Gmail accounts per device
5. **`config/nurtured_accounts.yaml`** — Priority accounts for engagement
6. **`config/schedule.yaml`** — Daily session schedule
7. **`config/modes/*.yaml`** — Engagement mode configs

### Run

```bash
# Single device, single account
python run.py --account my_account --device-id phone_01 --mode active_engage

# Warmup mode (new accounts)
python run.py --account my_account --device-id phone_01 --mode warmup

# Login with 2FA
python run.py --account my_account --device-id phone_01 --mode login

# Fleet mode (all devices, all accounts)
python run.py --mode fleet

# Setup device isolation only
python run.py --device-id phone_01 --mode setup-isolation
```

### CLI Options

```
--account, -a      Account identifier
--device-id        Device ID (from config/devices/)
--mode, -m         Agent mode: active_engage, warmup, feed_scroll,
                   nurture_accounts, respond, login, system, fleet
--duration         Session duration in seconds
--no-isolation     Skip proxy/GPS (testing only — exposes real IP!)
--debug            Verbose logging
```

## Device Isolation

Every session starts with a 3-layer isolation check:

1. **Proxy** — HTTP CONNECT via iptables + redsocks (routes ALL device traffic)
2. **GPS spoofing** — Coordinates matching proxy country
3. **Fingerprint** — Android ID, WiFi MAC, device model spoofing

If any layer fails verification, the session is aborted. Periodic re-verification runs every 30 minutes during long sessions.

## Content Pipeline

Optional Telegram-based content distribution:

1. Send photos/videos/reels to Telegram bot
2. Uniqualization worker processes media (metadata strip, slight visual changes)
3. Distributor assigns content to accounts
4. Agent posts during scheduled sessions

```bash
# Start Telegram bot
python scripts/run_telegram_bot.py

# Start uniqualization worker
python scripts/run_uniqualization_worker.py

# Manual content distribution
python scripts/distribute_content.py
```

## Project Structure

```
eidola/
├── run.py                          # Entry point
├── src/eidola/
│   ├── main.py                     # Session runner, fleet management
│   ├── config.py                   # Pydantic settings, fleet models
│   ├── agents/
│   │   ├── instagram_agent.py      # Unified AI agent (main)
│   │   ├── orchestrator.py         # Legacy multi-agent (deprecated)
│   │   ├── navigator.py            # Legacy: navigation agent
│   │   ├── observer.py             # Legacy: content analysis agent
│   │   ├── engager.py              # Legacy: engagement agent
│   │   ├── system_agent.py         # Device tasks (Gmail, etc.)
│   │   └── callbacks.py            # before_model / after_tool hooks
│   ├── tools/
│   │   ├── firerpa_tools.py        # FIRERPA SDK integration (~7000 lines)
│   │   ├── screen_detector.py      # Screen type detection from XML
│   │   ├── element_finder.py       # UI element extraction
│   │   ├── gesture_generator.py    # Human-like swipe generation
│   │   ├── memory_tools.py         # MongoDB-backed agent memory
│   │   ├── auth_tools.py           # Login + 2FA tools
│   │   └── posting_tools.py        # Content posting tools
│   ├── scheduler/
│   │   ├── multi_account_scheduler.py
│   │   ├── session_runner.py       # Session lifecycle + budget
│   │   ├── daily_plan.py           # Archetype-based day planning
│   │   └── account_rotator.py      # Account rotation strategy
│   ├── device/
│   │   ├── profile_manager.py      # Proxy + fingerprint + GPS
│   │   ├── proxy_config.py         # iptables + redsocks setup
│   │   ├── fingerprint.py          # Device identity spoofing
│   │   └── location.py             # GPS mock
│   ├── content/
│   │   ├── distributor.py          # Content → accounts assignment
│   │   ├── uniqualization_worker.py
│   │   ├── image_uniqualizer.py    # Image metadata/visual tweaks
│   │   ├── video_uniqualizer.py    # Video uniqualization
│   │   └── caption_uniqualizer.py  # Caption variation
│   ├── bot/
│   │   └── telegram_bot.py         # Content intake bot
│   └── memory/
│       ├── sync_memory.py          # MongoDB persistence
│       └── windowed_session.py     # Token overflow prevention
├── config/
│   ├── fleet.yaml                  # Fleet-wide settings
│   ├── schedule.yaml               # Daily schedule with archetypes
│   ├── session_limits.yaml         # Engagement limits
│   ├── nurtured_accounts.yaml      # Priority account list
│   ├── accounts/                   # Per-account configs
│   ├── devices/                    # Per-device configs
│   ├── gmail/                      # Gmail per device
│   └── modes/                      # Mode behavior configs
├── prompts/                        # Agent instruction prompts
└── scripts/                        # Fleet management scripts
```

## Context Management

Long sessions (30+ minutes) generate massive conversation histories. The system uses a layered approach:

1. **ADK EventsCompaction** — LLM-based summarization of old events (every 10 turns)
2. **WindowedSessionService** — Hard limit safety net (50 events max)
3. **ContextCacheConfig** — Caches system prompts to reduce token usage
4. **XML/Screenshot compression** — Strips heavy payloads from tool responses

Token budget: 5M input tokens per session hard limit.

## License

MIT — do whatever you want with it.

## Contributing

PRs welcome. If you're building something on top of this, I'd love to hear about it.
