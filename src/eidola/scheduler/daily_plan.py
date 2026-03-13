"""
DailyPlanGenerator — Human-like daily schedule planner (v2).

Reads config/schedule.yaml v2 (archetype-based) and generates a full
day plan upfront: which sessions fire, when they start, how long they last.
Supports energy levels, jitter, anti-repeat from yesterday, gap enforcement,
budget trimming, and bonus session backfill.

Usage:
    gen = DailyPlanGenerator()
    plan = gen.generate_plan(date.today(), "account_123", "phone_01")
    gen.print_plan(plan)
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger("eidola.scheduler.daily_plan")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PlannedSession:
    """A single session slot in the daily plan."""

    label: str
    start_time: datetime
    duration_minutes: int
    mode: str
    status: str = "pending"  # pending | running | completed | skipped
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    account_id: str = ""

    @property
    def end_time(self) -> datetime:
        return self.start_time + timedelta(minutes=self.duration_minutes)

    def to_dict(self) -> dict:
        """Convert to MongoDB-storable dict."""
        return {
            "label": self.label,
            "start_time": self.start_time,
            "duration_minutes": self.duration_minutes,
            "mode": self.mode,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "account_id": self.account_id,
        }

    @classmethod
    def from_dict(cls, data: dict, tz: ZoneInfo | None = None) -> "PlannedSession":
        """Create from MongoDB document.
        
        MongoDB stores datetimes in UTC. If tz is provided, convert
        start_time back to the target timezone for correct display and
        comparison with datetime.now(tz).
        """
        st = data["start_time"]
        if isinstance(st, str):
            st = datetime.fromisoformat(st)
        # MongoDB returns UTC — convert to target timezone
        if tz and st.tzinfo is not None:
            st = st.astimezone(tz)
        elif tz and st.tzinfo is None:
            # Assume UTC if no tzinfo, then convert
            st = st.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        return cls(
            label=data["label"],
            start_time=st,
            duration_minutes=data["duration_minutes"],
            mode=data["mode"],
            status=data.get("status", "pending"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            account_id=data.get("account_id", ""),
        )

    def __repr__(self) -> str:
        st = self.start_time.strftime("%H:%M")
        return f"<Session {self.label} @{st} {self.duration_minutes}m [{self.status}]>"


@dataclass
class DailyPlan:
    """Full plan for a single account on a single day."""

    account_id: str
    device_id: str
    date: date
    energy_level: str
    budget_minutes: int
    sessions: list[PlannedSession]
    total_planned_minutes: int
    total_actual_minutes: int = 0
    generated_at: datetime = field(default_factory=lambda: datetime.now(tz=ZoneInfo("UTC")))
    account_ids: list[str] = field(default_factory=list)

    @property
    def session_count(self) -> int:
        return len(self.sessions)

    def pending_sessions(self) -> list[PlannedSession]:
        return [s for s in self.sessions if s.status == "pending"]

    def completed_sessions(self) -> list[PlannedSession]:
        return [s for s in self.sessions if s.status == "completed"]

    def to_dict(self) -> dict:
        """Convert to MongoDB-storable dict."""
        return {
            "account_id": self.account_id,
            "device_id": self.device_id,
            "date": self.date.isoformat() if isinstance(self.date, date) else self.date,
            "energy_level": self.energy_level,
            "budget_minutes": self.budget_minutes,
            "sessions": [s.to_dict() for s in self.sessions],
            "generated_at": self.generated_at,
            "total_planned_minutes": self.total_planned_minutes,
            "total_actual_minutes": self.total_actual_minutes,
            "account_ids": self.account_ids,
        }

    @classmethod
    def from_dict(cls, data: dict, tz: ZoneInfo | None = None) -> "DailyPlan":
        """Create from MongoDB document.
        
        Args:
            data: MongoDB document
            tz: Target timezone for session times (MongoDB stores UTC)
        """
        plan_date = data["date"]
        if isinstance(plan_date, str):
            plan_date = date.fromisoformat(plan_date)

        generated_at = data.get("generated_at", datetime.now(tz=ZoneInfo("UTC")))
        if isinstance(generated_at, str):
            generated_at = datetime.fromisoformat(generated_at)
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=ZoneInfo("UTC"))

        return cls(
            account_id=data["account_id"],
            device_id=data["device_id"],
            date=plan_date,
            energy_level=data.get("energy_level", "normal"),
            budget_minutes=data.get("budget_minutes", 240),
            sessions=[PlannedSession.from_dict(s, tz=tz) for s in data.get("sessions", [])],
            total_planned_minutes=data.get("total_planned_minutes", 0),
            total_actual_minutes=data.get("total_actual_minutes", 0),
            generated_at=generated_at,
            account_ids=data.get("account_ids", []),
        )


# ---------------------------------------------------------------------------
# Archetype helper (parsed from YAML)
# ---------------------------------------------------------------------------


@dataclass
class _Archetype:
    """Internal representation of an archetype from config."""

    label: str
    base_time: time
    jitter_minutes: int
    duration_range: tuple[int, int]
    probability: float
    energy_weight: float
    mode: str


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class DailyPlanGenerator:
    """
    Generates a full daily session plan from schedule.yaml v2.

    Steps performed by ``generate_plan``:
        1. Roll energy level for the day.
        2. Compute daily budget (Gaussian + energy offset, clamped).
        3. Select archetype list (weekday vs weekend).
        4. Roll each archetype by probability x energy multiplier.
        5. Apply jitter to start times.
        6. Anti-repeat adjustment from yesterday's plan.
        7. Enforce minimum gaps (push or drop).
        8. Trim sessions to fit budget (drop lowest-probability first).
        9. Backfill bonus sessions if total < 70 % of budget.
    """

    def __init__(self, config_path: str = "config/schedule.yaml") -> None:
        self._config_path = Path(config_path)
        self._raw: dict[str, Any] = {}
        self._tz: ZoneInfo = ZoneInfo("America/New_York")

        # Parsed buckets
        self._global: dict[str, Any] = {}
        self._weekday_archetypes: list[_Archetype] = []
        self._weekend_archetypes: list[_Archetype] = []
        self._energy_levels: dict[str, dict[str, float]] = {}
        self._astro: dict[str, Any] = {}

        self._load()

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load(self) -> None:
        with open(self._config_path, "r", encoding="utf-8") as fh:
            self._raw = yaml.safe_load(fh)

        self._global = self._raw.get("global", {})
        tz_str = self._global.get("timezone", "America/New_York")
        self._tz = ZoneInfo(tz_str)

        self._weekday_archetypes = self._parse_archetypes(
            self._raw.get("weekday_archetypes", [])
        )
        self._weekend_archetypes = self._parse_archetypes(
            self._raw.get("weekend_archetypes", [])
        )
        self._energy_levels = self._raw.get("energy_levels", {})
        self._astro = self._raw.get("astro", {})

        logger.info(
            "Loaded schedule v2: %d weekday / %d weekend archetypes",
            len(self._weekday_archetypes),
            len(self._weekend_archetypes),
        )

    @staticmethod
    def _parse_archetypes(raw_list: list[dict]) -> list[_Archetype]:
        result: list[_Archetype] = []
        for item in raw_list:
            bt = item["base_time"]
            if isinstance(bt, str):
                parts = bt.split(":")
                bt = time(int(parts[0]), int(parts[1]))
            dur = item.get("duration_minutes", [10, 20])
            result.append(
                _Archetype(
                    label=item["label"],
                    base_time=bt,
                    jitter_minutes=item.get("jitter_minutes", 15),
                    duration_range=(dur[0], dur[1]),
                    probability=item.get("probability", 0.5),
                    energy_weight=item.get("energy_weight", 1.0),
                    mode=item.get("mode", "warmup"),
                )
            )
        return result

    # ------------------------------------------------------------------
    # Energy
    # ------------------------------------------------------------------

    def _roll_energy(self) -> str:
        """Weighted random pick of energy level for the day."""
        levels = list(self._energy_levels.items())
        names = [name for name, _ in levels]
        weights = [cfg.get("probability", 0.33) for _, cfg in levels]
        return random.choices(names, weights=weights, k=1)[0]

    def _energy_cfg(self, level: str) -> dict:
        return self._energy_levels.get(level, {"multiplier": 1.0, "budget_offset": 0})

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------

    def _compute_budget(self, energy_level: str) -> int:
        budget_cfg = self._global.get("daily_budget", {})
        target = budget_cfg.get("target_minutes", 240)
        variance = budget_cfg.get("variance_minutes", 30)
        floor = budget_cfg.get("min_minutes", 180)
        ceiling = budget_cfg.get("max_minutes", 280)

        raw = random.gauss(target, variance)
        offset = self._energy_cfg(energy_level).get("budget_offset", 0)
        return int(max(floor, min(ceiling, raw + offset)))

    # ------------------------------------------------------------------
    # Core generation
    # ------------------------------------------------------------------

    def generate_plan(
        self,
        plan_date: date,
        account_id: str,
        device_id: str,
        yesterday_plan: DailyPlan | None = None,
    ) -> DailyPlan:
        """Build a complete daily plan for *plan_date*."""

        # 1. Energy
        energy_level = self._roll_energy()
        energy_cfg = self._energy_cfg(energy_level)
        energy_mult = energy_cfg.get("multiplier", 1.0)

        # 2. Budget
        budget = self._compute_budget(energy_level)

        # 3. Pick archetype set
        is_weekend = plan_date.weekday() >= 5
        archetypes = list(
            self._weekend_archetypes if is_weekend else self._weekday_archetypes
        )

        # 4. Roll each archetype
        sessions: list[PlannedSession] = []
        for arch in archetypes:
            effective_prob = min(1.0, arch.probability * (energy_mult ** arch.energy_weight))
            if random.random() >= effective_prob:
                continue  # slot didn't fire

            # 5. Jitter
            jitter = random.uniform(-arch.jitter_minutes, arch.jitter_minutes)
            start_dt = self._base_to_datetime(plan_date, arch.base_time, jitter)

            # Duration
            dur = random.randint(arch.duration_range[0], arch.duration_range[1])

            sessions.append(
                PlannedSession(
                    label=arch.label,
                    start_time=start_dt,
                    duration_minutes=dur,
                    mode=arch.mode,
                )
            )

        # Sort by start_time
        sessions.sort(key=lambda s: s.start_time)

        # 6. Anti-repeat from yesterday
        if yesterday_plan:
            sessions = self._anti_repeat(sessions, yesterday_plan)

        # 7. Enforce minimum gaps
        sessions = self._enforce_gaps(sessions)

        # 8. Trim to budget
        sessions = self._trim_to_budget(sessions, budget, archetypes)

        # 9. Bonus backfill if under 70 % of budget
        total = sum(s.duration_minutes for s in sessions)
        if total < budget * 0.70:
            sessions = self._backfill(sessions, budget, archetypes, plan_date, energy_mult)
            sessions.sort(key=lambda s: s.start_time)
            sessions = self._enforce_gaps(sessions)
            sessions = self._trim_to_budget(sessions, budget, archetypes)

        total_planned = sum(s.duration_minutes for s in sessions)

        plan = DailyPlan(
            account_id=account_id,
            device_id=device_id,
            date=plan_date,
            energy_level=energy_level,
            budget_minutes=budget,
            sessions=sessions,
            total_planned_minutes=total_planned,
            generated_at=datetime.now(tz=ZoneInfo("UTC")),
        )

        logger.info(
            "Plan generated for %s: energy=%s budget=%dm sessions=%d planned=%dm",
            plan_date,
            energy_level,
            budget,
            len(sessions),
            total_planned,
        )
        return plan

    # ------------------------------------------------------------------
    # Multi-account device plan
    # ------------------------------------------------------------------

    def generate_device_plan(
        self,
        plan_date: date,
        account_ids: list[str],
        device_id: str,
        timezone: ZoneInfo | None = None,
        mongo_db=None,
        break_between_accounts_minutes: int = 5,
    ) -> DailyPlan:
        """Generate a merged daily plan for multiple accounts on one device.

        Each account gets its own independent daily rhythm (energy, budget, archetypes).
        Plans are merged by start_time with breaks inserted between account switches.

        Args:
            plan_date: Date to generate plan for
            account_ids: List of account IDs assigned to this device
            device_id: Device identifier
            timezone: Timezone (defaults to schedule config)
            mongo_db: MongoDB database for anti-repeat logic
            break_between_accounts_minutes: Gap between switching accounts

        Returns:
            Merged DailyPlan with account_id set on each PlannedSession
        """
        if not account_ids:
            raise ValueError("account_ids must not be empty")

        # a. Generate an independent plan per account
        per_account_plans: list[DailyPlan] = []
        for acct_id in account_ids:
            plan = self.generate_plan(plan_date, acct_id, device_id)
            per_account_plans.append(plan)

        # b. Collect all sessions, tagging each with its account_id
        all_sessions: list[PlannedSession] = []
        for plan in per_account_plans:
            for sess in plan.sessions:
                sess.account_id = plan.account_id
                all_sessions.append(sess)

        # c. Sort by start_time
        all_sessions.sort(key=lambda s: s.start_time)

        # d. Walk through and enforce no-overlap + inter-account breaks
        if len(all_sessions) > 1:
            for i in range(1, len(all_sessions)):
                prev = all_sessions[i - 1]
                curr = all_sessions[i]

                prev_end = prev.end_time
                gap_minutes = (curr.start_time - prev_end).total_seconds() / 60

                if curr.account_id != prev.account_id:
                    required_gap = break_between_accounts_minutes
                else:
                    required_gap = 0

                if gap_minutes < required_gap:
                    shift = required_gap - gap_minutes
                    curr.start_time = prev_end + timedelta(minutes=required_gap)
                elif gap_minutes < 0:
                    curr.start_time = prev_end + timedelta(
                        minutes=max(0, required_gap)
                    )

        # e. Build merged plan
        total_budget = sum(p.budget_minutes for p in per_account_plans)
        total_planned = sum(s.duration_minutes for s in all_sessions)

        merged = DailyPlan(
            account_id=account_ids[0],
            device_id=device_id,
            date=plan_date,
            energy_level="mixed",
            budget_minutes=total_budget,
            sessions=all_sessions,
            total_planned_minutes=total_planned,
            generated_at=datetime.now(tz=ZoneInfo("UTC")),
            account_ids=list(account_ids),
        )

        logger.info(
            "Device plan generated for %s on %s: %d accounts, %d sessions, %dm planned",
            device_id,
            plan_date,
            len(account_ids),
            len(all_sessions),
            total_planned,
        )
        return merged

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _base_to_datetime(
        self, plan_date: date, base: time, jitter_minutes: float
    ) -> datetime:
        """Convert a base_time + jitter into a timezone-aware datetime.

        Handles day-boundary wrapping (e.g. 00:15 belongs to the same
        calendar day in the plan but is technically the next day).
        """
        base_dt = datetime.combine(plan_date, base, tzinfo=self._tz)
        result = base_dt + timedelta(minutes=jitter_minutes)

        # If the base_time is <= 01:00, it likely belongs to the "same night"
        # but is after midnight — keep it on plan_date even if jitter pushes
        # it slightly before midnight.
        if result.date() < plan_date:
            result += timedelta(days=1)

        return result

    # ------------------------------------------------------------------
    # Anti-repeat
    # ------------------------------------------------------------------

    def _anti_repeat(
        self, sessions: list[PlannedSession], yesterday: DailyPlan
    ) -> list[PlannedSession]:
        """If yesterday had the same label at a similar time, nudge it."""
        yesterday_labels: dict[str, datetime] = {
            s.label: s.start_time for s in yesterday.sessions
        }

        for sess in sessions:
            if sess.label in yesterday_labels:
                y_time = yesterday_labels[sess.label]
                # Compare only time-of-day (minutes since midnight)
                y_minutes = y_time.hour * 60 + y_time.minute
                s_minutes = sess.start_time.hour * 60 + sess.start_time.minute

                if abs(s_minutes - y_minutes) < 20:
                    # Shift opposite direction by 5-15 min
                    direction = 1 if s_minutes <= y_minutes else -1
                    shift = random.randint(5, 15) * direction
                    sess.start_time += timedelta(minutes=shift)
                    logger.debug(
                        "Anti-repeat: shifted %s by %+d min", sess.label, shift
                    )

        # Re-sort after shifting
        sessions.sort(key=lambda s: s.start_time)
        return sessions

    # ------------------------------------------------------------------
    # Gap enforcement
    # ------------------------------------------------------------------

    def _enforce_gaps(self, sessions: list[PlannedSession]) -> list[PlannedSession]:
        """Ensure >= min_gap between consecutive sessions.

        Strategy: iterate in order. If gap is too small, try pushing the
        later session forward. If pushing would overlap the *next* session
        or land outside active hours (past 00:30), drop the session instead.
        """
        min_gap = self._global.get("limits", {}).get(
            "min_gap_between_sessions_minutes", 20
        )

        if len(sessions) <= 1:
            return sessions

        kept: list[PlannedSession] = [sessions[0]]

        for sess in sessions[1:]:
            prev = kept[-1]
            gap = (sess.start_time - prev.end_time).total_seconds() / 60

            if gap >= min_gap:
                kept.append(sess)
                continue

            # Try pushing forward
            needed = min_gap - gap
            new_start = sess.start_time + timedelta(minutes=needed + random.uniform(0, 5))

            # Don't push past 00:30 next day
            cutoff = datetime.combine(
                sess.start_time.date() + timedelta(days=1),
                time(0, 30),
                tzinfo=self._tz,
            )
            if new_start + timedelta(minutes=sess.duration_minutes) > cutoff:
                logger.debug("Gap enforce: dropping %s (would exceed cutoff)", sess.label)
                continue

            sess.start_time = new_start
            kept.append(sess)

        return kept

    # ------------------------------------------------------------------
    # Budget trimming
    # ------------------------------------------------------------------

    def _trim_to_budget(
        self,
        sessions: list[PlannedSession],
        budget: int,
        archetypes: list[_Archetype],
    ) -> list[PlannedSession]:
        """Remove lowest-probability sessions until total fits in budget."""
        total = sum(s.duration_minutes for s in sessions)
        if total <= budget:
            return sessions

        # Build probability lookup
        prob_map: dict[str, float] = {a.label: a.probability for a in archetypes}

        # Sort by ascending probability (least likely first -> dropped first)
        candidates = sorted(sessions, key=lambda s: prob_map.get(s.label, 0.5))

        dropped_labels: set[str] = set()
        while total > budget and candidates:
            drop = candidates.pop(0)
            total -= drop.duration_minutes
            dropped_labels.add(drop.label)
            logger.debug(
                "Trim: dropped %s (%dm) — total now %dm / %dm",
                drop.label,
                drop.duration_minutes,
                total,
                budget,
            )

        # Rebuild in chronological order, excluding dropped
        result = [s for s in sessions if s.label not in dropped_labels]
        return result

    # ------------------------------------------------------------------
    # Bonus backfill
    # ------------------------------------------------------------------

    def _backfill(
        self,
        sessions: list[PlannedSession],
        budget: int,
        archetypes: list[_Archetype],
        plan_date: date,
        energy_mult: float,
    ) -> list[PlannedSession]:
        """Add bonus sessions from the archetype pool if under 70 % budget."""
        total = sum(s.duration_minutes for s in sessions)
        used_labels = {s.label for s in sessions}

        # Candidates: archetypes not already in the plan, sorted by probability desc
        candidates = sorted(
            [a for a in archetypes if a.label not in used_labels],
            key=lambda a: a.probability,
            reverse=True,
        )

        for arch in candidates:
            if total >= budget * 0.85:
                break  # filled enough

            jitter = random.uniform(-arch.jitter_minutes, arch.jitter_minutes)
            start_dt = self._base_to_datetime(plan_date, arch.base_time, jitter)
            dur = random.randint(arch.duration_range[0], arch.duration_range[1])

            if total + dur > budget:
                # Try shorter
                remaining = budget - total
                if remaining >= arch.duration_range[0]:
                    dur = remaining
                else:
                    continue

            sessions.append(
                PlannedSession(
                    label=arch.label,
                    start_time=start_dt,
                    duration_minutes=dur,
                    mode=arch.mode,
                )
            )
            total += dur
            logger.debug("Backfill: added %s (%dm) — total now %dm", arch.label, dur, total)

        return sessions

    # ------------------------------------------------------------------
    # Pretty-print (for --dry-run)
    # ------------------------------------------------------------------

    def print_plan(self, plan: DailyPlan) -> None:
        """Human-readable dump of a daily plan (for CLI dry-run)."""
        day_name = plan.date.strftime("%A") if isinstance(plan.date, date) else plan.date
        is_weekend = plan.date.weekday() >= 5 if isinstance(plan.date, date) else False
        day_type = "WEEKEND" if is_weekend else "WEEKDAY"

        print()
        print("=" * 64)
        print(f"  DAILY PLAN — {plan.date}  ({day_name}, {day_type})")
        print(f"  Account: {plan.account_id}  |  Device: {plan.device_id}")
        print(f"  Energy: {plan.energy_level.upper()}  |  Budget: {plan.budget_minutes}m")
        print("=" * 64)
        print()
        print(f"  {'#':<4} {'Time':<8} {'Duration':<10} {'Label':<24} {'Mode':<12} {'Status'}")
        print(f"  {'---':<4} {'------':<8} {'--------':<10} {'----------------------':<24} {'----------':<12} {'-------'}")

        for i, sess in enumerate(plan.sessions, 1):
            t = sess.start_time.strftime("%H:%M")
            dur = f"{sess.duration_minutes}m"
            print(
                f"  {i:<4} {t:<8} {dur:<10} {sess.label:<24} {sess.mode:<12} {sess.status}"
            )

        print()
        print(f"  Sessions: {plan.session_count}")
        print(f"  Planned:  {plan.total_planned_minutes}m / {plan.budget_minutes}m budget")
        utilisation = (
            (plan.total_planned_minutes / plan.budget_minutes * 100)
            if plan.budget_minutes
            else 0
        )
        print(f"  Fill:     {utilisation:.0f}%")
        print("=" * 64)
        print()

    # ------------------------------------------------------------------
    # MongoDB serialisation
    # ------------------------------------------------------------------

    def to_mongo_doc(self, plan: DailyPlan) -> dict:
        """Serialise a DailyPlan to a MongoDB-friendly dict."""
        return plan.to_dict()

    def from_mongo_doc(self, doc: dict) -> DailyPlan:
        """Deserialise a MongoDB document back into a DailyPlan."""
        return DailyPlan.from_dict(doc, tz=self._tz)

    # ------------------------------------------------------------------
    # MongoDB persistence helpers
    # ------------------------------------------------------------------

    @staticmethod
    def save_plan(plan: DailyPlan, mongo_db) -> bool:
        """Upsert a daily plan to MongoDB.

        Args:
            plan: The DailyPlan to save.
            mongo_db: pymongo Database instance.

        Returns:
            True if saved successfully.
        """
        collection = mongo_db["daily_plans"]

        try:
            result = collection.update_one(
                {
                    "account_id": plan.account_id,
                    "device_id": plan.device_id,
                    "date": plan.date.isoformat() if isinstance(plan.date, date) else plan.date,
                },
                {"$set": plan.to_dict()},
                upsert=True,
            )
            logger.debug(
                "Saved plan for %s/%s (%s): upserted=%s",
                plan.device_id,
                plan.account_id,
                plan.date,
                result.upserted_id is not None,
            )
            return True
        except Exception as e:
            logger.error("Failed to save plan: %s", e)
            return False

    @staticmethod
    def load_plan(
        account_id: str,
        device_id: str,
        target_date: str,
        mongo_db,
        tz: ZoneInfo | None = None,
    ) -> DailyPlan | None:
        """Load a daily plan from MongoDB.
        
        Args:
            tz: Target timezone — MongoDB stores UTC, this converts back.
        """
        collection = mongo_db["daily_plans"]

        try:
            doc = collection.find_one({
                "account_id": account_id,
                "device_id": device_id,
                "date": target_date,
            })
            if doc:
                logger.debug("Loaded plan for %s/%s (%s)", device_id, account_id, target_date)
                return DailyPlan.from_dict(doc, tz=tz)
            return None
        except Exception as e:
            logger.error("Failed to load plan: %s", e)
            return None

    @staticmethod
    def mark_session_status(
        plan: DailyPlan,
        session_label: str,
        status: str,
        mongo_db,
        error: str | None = None,
    ) -> bool:
        """Update a session's status in MongoDB."""
        collection = mongo_db["daily_plans"]
        now = datetime.now(tz=ZoneInfo("UTC"))

        update_fields: dict[str, Any] = {
            "sessions.$.status": status,
        }

        if status == "running":
            update_fields["sessions.$.started_at"] = now
        elif status in ("completed", "failed", "skipped"):
            update_fields["sessions.$.completed_at"] = now
            if error:
                update_fields["sessions.$.error"] = error

        inc_fields: dict[str, int] = {}
        if status == "completed":
            for sess in plan.sessions:
                if sess.label == session_label:
                    inc_fields["total_actual_minutes"] = sess.duration_minutes
                    break

        try:
            update_op: dict[str, Any] = {"$set": update_fields}
            if inc_fields:
                update_op["$inc"] = inc_fields

            result = collection.update_one(
                {
                    "account_id": plan.account_id,
                    "device_id": plan.device_id,
                    "date": plan.date.isoformat() if isinstance(plan.date, date) else plan.date,
                    "sessions.label": session_label,
                },
                update_op,
            )

            # Also update local object
            for sess in plan.sessions:
                if sess.label == session_label:
                    sess.status = status
                    if status == "running":
                        sess.started_at = now
                    elif status in ("completed", "failed", "skipped"):
                        sess.completed_at = now
                        sess.error = error
                    break

            if status == "completed" and inc_fields:
                plan.total_actual_minutes += inc_fields.get("total_actual_minutes", 0)

            return result.modified_count > 0
        except Exception as e:
            logger.error("Failed to update session status: %s", e)
            return False

    @staticmethod
    def ensure_indexes(mongo_db) -> None:
        """Create indexes for efficient queries on daily_plans collection."""
        collection = mongo_db["daily_plans"]
        collection.create_index(
            [("account_id", 1), ("device_id", 1), ("date", 1)],
            unique=True,
            name="idx_plan_lookup",
        )
        collection.create_index(
            [("date", 1)],
            name="idx_plan_date",
        )
        logger.debug("Ensured indexes on daily_plans collection")


# ---------------------------------------------------------------------------
# Helper functions (used by fleet_scheduler / session_runner)
# ---------------------------------------------------------------------------


def get_or_generate_plan(
    device_id: str,
    account_id: str,
    target_date: date,
    mongo_db,
    generator: DailyPlanGenerator,
    timezone: ZoneInfo | None = None,
    force_regenerate: bool = False,
) -> DailyPlan:
    """Get existing plan from MongoDB, or generate a new one."""
    date_str = target_date.isoformat()

    if not force_regenerate:
        existing = DailyPlanGenerator.load_plan(account_id, device_id, date_str, mongo_db, tz=timezone)
        if existing:
            logger.info(
                "Loaded existing plan for %s/%s (%s): %d sessions, %d/%dm done",
                device_id,
                account_id,
                date_str,
                len(existing.sessions),
                existing.total_actual_minutes,
                existing.total_planned_minutes,
            )
            return existing

    plan = generator.generate_plan(target_date, account_id, device_id)
    DailyPlanGenerator.save_plan(plan, mongo_db)
    return plan


def get_or_generate_device_plan(
    device_id: str,
    account_ids: list[str],
    target_date: date,
    mongo_db,
    generator: DailyPlanGenerator,
    timezone: ZoneInfo | None = None,
    force_regenerate: bool = False,
    break_between_accounts_minutes: int = 5,
) -> DailyPlan:
    """Get existing multi-account device plan from MongoDB, or generate a new one.

    Uses a device-level collection key (with multi_account flag) so it doesn't
    collide with single-account plans.
    """
    date_str = target_date.isoformat()
    collection = mongo_db["daily_plans"]

    multi_account_id = f"__multi__{device_id}"
    if not force_regenerate:
        doc = collection.find_one({
            "account_id": multi_account_id,
            "device_id": device_id,
            "date": date_str,
        })
        if doc:
            plan = DailyPlan.from_dict(doc, tz=timezone)
            logger.info(
                "Loaded existing device plan for %s (%s): %d sessions, %d/%dm done",
                device_id,
                date_str,
                len(plan.sessions),
                plan.total_actual_minutes,
                plan.total_planned_minutes,
            )
            return plan

    plan = generator.generate_device_plan(
        plan_date=target_date,
        account_ids=account_ids,
        device_id=device_id,
        timezone=timezone,
        mongo_db=mongo_db,
        break_between_accounts_minutes=break_between_accounts_minutes,
    )

    # Use a synthetic account_id to avoid unique index collision with single-account plans.
    # The unique index is on (account_id, device_id, date).
    multi_account_id = f"__multi__{device_id}"
    doc = plan.to_dict()
    doc["account_id"] = multi_account_id
    doc["multi_account"] = True
    collection.update_one(
        {
            "account_id": multi_account_id,
            "device_id": device_id,
            "date": date_str,
        },
        {"$set": doc},
        upsert=True,
    )
    logger.debug("Saved device plan for %s (%s)", device_id, date_str)

    return plan


def find_next_pending(plan: DailyPlan, skip_past: bool = True) -> PlannedSession | None:
    """Find the next pending session in a plan.
    
    Args:
        plan: The daily plan to search
        skip_past: If True, mark past sessions as "skipped" and return
                   the first future or just-past (within 30 min) session.
                   If a past session is within 30 min, run it immediately.
    """
    now = datetime.now(tz=plan.sessions[0].start_time.tzinfo) if plan.sessions else None
    
    for session in plan.sessions:
        if session.status != "pending":
            continue
        
        if skip_past and now:
            time_diff = (session.start_time - now).total_seconds() / 60
            
            if time_diff < -30:
                # More than 30 min in the past — mark as skipped
                session.status = "skipped"
                logger.info("Skipping past session: %s (was at %s)", 
                           session.label, session.start_time.strftime("%H:%M"))
                continue
            # Within 30 min of past or in future — run it
        
        return session
    return None


def format_plan_table(plan: DailyPlan) -> str:
    """Format a plan as a human-readable table string."""
    status_icons = {
        "pending": "...",
        "running": ">>>",
        "completed": "[+]",
        "skipped": "[-]",
        "failed": "[!]",
    }

    multi = bool(plan.account_ids and len(plan.account_ids) > 1)
    lines = []
    d = plan.date.isoformat() if isinstance(plan.date, date) else plan.date
    if multi:
        accts = ", ".join(plan.account_ids)
        lines.append(f"  Plan: {plan.device_id} / [{accts}] — {d}")
    else:
        lines.append(f"  Plan: {plan.device_id} / {plan.account_id} — {d}")
    lines.append(
        f"  Energy: {plan.energy_level} | Budget: {plan.budget_minutes}m | "
        f"Planned: {plan.total_planned_minutes}m | Done: {plan.total_actual_minutes}m"
    )
    if multi:
        lines.append(f"  {'#':<3} {'Time':<8} {'Account':<22} {'Label':<22} {'Dur':<6} {'Mode':<15} {'Status':<10}")
        lines.append(f"  {'---':<3} {'------':<8} {'--------------------':<22} {'--------------------':<22} {'----':<6} {'-------------':<15} {'--------':<10}")
    else:
        lines.append(f"  {'#':<3} {'Time':<8} {'Label':<22} {'Dur':<6} {'Mode':<15} {'Status':<10}")
        lines.append(f"  {'---':<3} {'------':<8} {'--------------------':<22} {'----':<6} {'-------------':<15} {'--------':<10}")

    for i, s in enumerate(plan.sessions, 1):
        t = s.start_time.strftime("%H:%M") if s.start_time else "??:??"
        icon = status_icons.get(s.status, "?")
        if multi:
            acct = s.account_id[:20] if s.account_id else "?"
            lines.append(
                f"  {i:<3} {t:<8} {acct:<22} {s.label:<22} {s.duration_minutes:<6} "
                f"{s.mode:<15} {icon} {s.status}"
            )
        else:
            lines.append(
                f"  {i:<3} {t:<8} {s.label:<22} {s.duration_minutes:<6} "
                f"{s.mode:<15} {icon} {s.status}"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point for dry-run testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    from datetime import date as _date

    logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Generate a daily plan (dry-run)")
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--account", type=str, default="demo_account")
    parser.add_argument("--device", type=str, default="phone_01")
    parser.add_argument("--config", type=str, default="config/schedule.yaml")
    args = parser.parse_args()

    plan_date = _date.fromisoformat(args.date) if args.date else _date.today()

    gen = DailyPlanGenerator(config_path=args.config)
    plan = gen.generate_plan(plan_date, args.account, args.device)
    gen.print_plan(plan)
