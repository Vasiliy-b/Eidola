"""Human-like gesture generator based on recorded gesture statistics.

Uses log-normal distributions and real velocity profiles from recorded data.
"""

import random
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class Point:
    """Screen coordinate point."""
    x: int
    y: int
    
    def __iter__(self):
        return iter((self.x, self.y))


@dataclass
class SwipeGesture:
    """Single swipe gesture with points and timing."""
    points: list[Point]
    duration_ms: int
    direction: int  # 1 = scroll down (feed forward), -1 = scroll up (back)
    
    def to_firerpa_points(self) -> list[Point]:
        """Convert to FIRERPA Point objects."""
        return self.points


@dataclass
class GestureSequence:
    """Multiple gestures to execute as a batch (for burst mode)."""
    gestures: list[SwipeGesture]
    pauses_ms: list[int]  # pause after each gesture (len = len(gestures))


# =============================================================================
# Distribution Helpers
# =============================================================================

def lognormal_sample(mu: float, sigma: float, min_val: float, max_val: float) -> float:
    """Sample from clipped log-normal distribution."""
    val = random.lognormvariate(mu, sigma)
    return max(min_val, min(max_val, val))


def gaussian_sample(mean: float, std: float, min_val: float, max_val: float) -> float:
    """Sample from clipped gaussian distribution."""
    val = random.gauss(mean, std)
    return max(min_val, min(max_val, val))


# =============================================================================
# Velocity Profile Functions
# =============================================================================

def ease_in_profile(t: float) -> float:
    """Start slow, accelerate to end. t in [0,1], returns [0,1]."""
    return t ** 2


def ease_out_profile(t: float) -> float:
    """Start fast, decelerate to end."""
    return 1 - (1 - t) ** 2


def ease_in_out_profile(t: float) -> float:
    """Slow start, fast middle, slow end."""
    if t < 0.5:
        return 2 * t * t
    return 1 - ((-2 * t + 2) ** 2) / 2


def peak_middle_profile(t: float) -> float:
    """Peak velocity in middle."""
    return 4 * t * (1 - t)


def linear_profile(t: float) -> float:
    """Constant velocity."""
    return t


# Weighted random selection of profiles based on observed data
VELOCITY_PROFILES: list[tuple[Callable[[float], float], float]] = [
    (ease_in_profile, 0.35),      # Most common in fast scrolls
    (ease_out_profile, 0.20),     # Common in slow/reading scrolls
    (ease_in_out_profile, 0.25),  # Natural feel
    (peak_middle_profile, 0.15),  # Observed in some gestures
    (linear_profile, 0.05),       # Rare but exists
]


def random_velocity_profile() -> Callable[[float], float]:
    """Select random velocity profile based on weights."""
    profiles, weights = zip(*VELOCITY_PROFILES)
    return random.choices(profiles, weights=weights)[0]


# =============================================================================
# Gesture Generator
# =============================================================================

class HumanGestureGenerator:
    """Generates human-like Instagram scroll gestures.
    
    Statistics based on 191 recorded gestures:
    - Velocity: 54-9926 px/sec, avg 2593
    - Duration: 33-4467ms, avg 675
    - Distance: 77-875px, avg 495
    - X drift: 12-525px, avg 186
    - Pause: 134-6000ms, avg ~1000
    - Scroll back probability: ~12% in data, use 8-9%
    """
    
    # Screen dimensions (configure for your device)
    SCREEN_WIDTH: int = 1080
    SCREEN_HEIGHT: int = 2400
    
    # Log-normal distribution parameters (mu, sigma, min, max)
    # Derived from recorded gesture statistics
    VELOCITY_PARAMS = (7.8, 0.7, 54, 9926)      # px/sec
    DURATION_PARAMS = (6.2, 0.8, 33, 4467)      # ms
    DISTANCE_PARAMS = (6.1, 0.4, 77, 875)       # px
    X_DRIFT_PARAMS = (4.8, 0.7, 12, 525)        # px
    PAUSE_PARAMS = (6.5, 0.6, 134, 6000)        # ms
    
    # Burst mode parameters
    BURST_PAUSE_PARAMS = (5.5, 0.4, 100, 400)   # shorter pauses in burst
    BURST_DURATION_SCALE = 0.4                   # faster gestures in burst
    
    # Behavioral parameters
    SCROLL_BACK_PROBABILITY = 0.085  # 8.5% chance to scroll back
    BURST_PROBABILITY = 0.15         # 15% chance to enter burst mode
    BURST_COUNT_MIN = 2
    BURST_COUNT_MAX = 4
    
    def __init__(
        self,
        screen_width: int = 1080,
        screen_height: int = 2400,
    ):
        """Initialize generator with screen dimensions."""
        self.SCREEN_WIDTH = screen_width
        self.SCREEN_HEIGHT = screen_height
    
    def _sample_velocity(self) -> float:
        return lognormal_sample(*self.VELOCITY_PARAMS)
    
    def _sample_duration(self, burst_mode: bool = False) -> int:
        duration = lognormal_sample(*self.DURATION_PARAMS)
        if burst_mode:
            duration *= self.BURST_DURATION_SCALE
        return int(max(33, duration))
    
    def _sample_distance(self) -> int:
        return int(lognormal_sample(*self.DISTANCE_PARAMS))
    
    def _sample_x_drift(self) -> int:
        drift = lognormal_sample(*self.X_DRIFT_PARAMS)
        return int(drift * random.choice([-1, 1]))  # random direction
    
    def _sample_pause(self, burst_mode: bool = False) -> int:
        if burst_mode:
            return int(lognormal_sample(*self.BURST_PAUSE_PARAMS))
        return int(lognormal_sample(*self.PAUSE_PARAMS))
    
    def _generate_points(
        self,
        start: Point,
        end: Point,
        duration_ms: int,
        num_points: int | None = None,
    ) -> list[Point]:
        """Generate swipe points with velocity profile and jitter."""
        
        # Number of points based on duration (more points for longer gestures)
        if num_points is None:
            num_points = max(5, min(20, duration_ms // 30))
        
        profile = random_velocity_profile()
        points = []
        
        for i in range(num_points + 1):
            t = i / num_points  # normalized time [0, 1]
            
            # Apply velocity profile to position
            eased_t = profile(t)
            
            # Interpolate position
            x = start.x + (end.x - start.x) * eased_t
            y = start.y + (end.y - start.y) * eased_t
            
            # Add micro-jitter (gaussian, not uniform!)
            jitter_x = gaussian_sample(0, 3, -10, 10)
            jitter_y = gaussian_sample(0, 4, -15, 15)
            
            x = int(x + jitter_x)
            y = int(y + jitter_y)
            
            # Clamp to screen bounds
            x = max(20, min(self.SCREEN_WIDTH - 20, x))
            y = max(20, min(self.SCREEN_HEIGHT - 20, y))
            
            points.append(Point(x, y))
        
        return points
    
    def generate_scroll(self, burst_mode: bool = False) -> SwipeGesture:
        """Generate single scroll gesture.
        
        Args:
            burst_mode: If True, generates faster/shorter gesture
            
        Returns:
            SwipeGesture with points and timing
        """
        # Determine direction
        if random.random() < self.SCROLL_BACK_PROBABILITY:
            direction = -1  # scroll back up
        else:
            direction = 1   # scroll down (forward in feed)
        
        # Sample parameters
        duration_ms = self._sample_duration(burst_mode)
        distance = self._sample_distance()
        x_drift = self._sample_x_drift()
        
        # Calculate start position (random in safe zone)
        center_x = self.SCREEN_WIDTH // 2
        start_x = center_x + random.randint(-80, 80)
        
        if direction == 1:  # scrolling down
            # Start from lower part of screen, swipe up
            start_y = int(self.SCREEN_HEIGHT * 0.75) + random.randint(-50, 50)
            end_y = start_y - distance
        else:  # scrolling up (back)
            # Start from upper part, swipe down
            start_y = int(self.SCREEN_HEIGHT * 0.35) + random.randint(-50, 50)
            end_y = start_y + distance
        
        end_x = start_x + x_drift
        
        # Clamp end position
        end_x = max(50, min(self.SCREEN_WIDTH - 50, end_x))
        end_y = max(100, min(self.SCREEN_HEIGHT - 100, end_y))
        
        start = Point(start_x, start_y)
        end = Point(end_x, end_y)
        
        points = self._generate_points(start, end, duration_ms)
        
        return SwipeGesture(
            points=points,
            duration_ms=duration_ms,
            direction=direction,
        )
    
    def generate_burst(self, count: int | None = None) -> GestureSequence:
        """Generate burst of multiple fast scrolls.
        
        Use when agent wants to quickly skip through content.
        All gestures execute without LLM processing in between.
        
        Args:
            count: Number of gestures (default: random 2-4)
            
        Returns:
            GestureSequence with all gestures and pauses
        """
        if count is None:
            count = random.randint(self.BURST_COUNT_MIN, self.BURST_COUNT_MAX)
        
        gestures = []
        pauses = []
        
        for i in range(count):
            gesture = self.generate_scroll(burst_mode=True)
            # Force scroll down in burst (no scroll back)
            if gesture.direction == -1:
                gesture = self.generate_scroll(burst_mode=True)
            gestures.append(gesture)
            pauses.append(self._sample_pause(burst_mode=True))
        
        return GestureSequence(gestures=gestures, pauses_ms=pauses)
    
    def generate_slow_browse(self, count: int = 2) -> GestureSequence:
        """Generate slow browsing gestures (reading mode).
        
        Longer duration, longer pauses between.
        
        Args:
            count: Number of gestures
            
        Returns:
            GestureSequence
        """
        gestures = []
        pauses = []
        
        for _ in range(count):
            gesture = self.generate_scroll(burst_mode=False)
            gestures.append(gesture)
            # Longer pauses in reading mode
            pause = self._sample_pause(burst_mode=False) * 1.5
            pauses.append(int(min(pause, 5000)))
        
        return GestureSequence(gestures=gestures, pauses_ms=pauses)
    
    def should_burst(self) -> bool:
        """Randomly decide if next action should be burst mode."""
        return random.random() < self.BURST_PROBABILITY


# =============================================================================
# FIRERPA Integration Helper
# =============================================================================

def gesture_to_firerpa_args(gesture: SwipeGesture) -> list[str]:
    """Convert gesture points to FIRERPA swipe_points arguments.
    
    Usage:
        args = gesture_to_firerpa_args(gesture)
        d.swipe_points(*[Point(x=p.x, y=p.y) for p in gesture.points])
    
    Returns:
        List of Point constructor strings for debugging
    """
    return [f"Point(x={p.x}, y={p.y})" for p in gesture.points]


def execute_gesture(device, gesture: SwipeGesture) -> None:
    """Execute single gesture on FIRERPA device.
    
    Args:
        device: FIRERPA device instance (d)
        gesture: SwipeGesture to execute
    """
    from lamda.client import Point as FPoint
    
    points = [FPoint(x=p.x, y=p.y) for p in gesture.points]
    device.swipe_points(*points)


def execute_sequence(device, sequence: GestureSequence) -> None:
    """Execute gesture sequence on FIRERPA device.
    
    Executes all gestures with pauses, no LLM processing between.
    
    Args:
        device: FIRERPA device instance
        sequence: GestureSequence to execute
    """
    import time
    
    for gesture, pause_ms in zip(sequence.gestures, sequence.pauses_ms):
        execute_gesture(device, gesture)
        time.sleep(pause_ms / 1000.0)


# =============================================================================
# Example Usage
# =============================================================================

if __name__ == "__main__":
    # Demo
    gen = HumanGestureGenerator(screen_width=1080, screen_height=2400)
    
    print("=== Single Scroll ===")
    gesture = gen.generate_scroll()
    print(f"Direction: {'down' if gesture.direction == 1 else 'up'}")
    print(f"Duration: {gesture.duration_ms}ms")
    print(f"Points: {len(gesture.points)}")
    print(f"Start: ({gesture.points[0].x}, {gesture.points[0].y})")
    print(f"End: ({gesture.points[-1].x}, {gesture.points[-1].y})")
    
    print("\n=== Burst Mode ===")
    burst = gen.generate_burst()
    print(f"Gestures in burst: {len(burst.gestures)}")
    for i, (g, p) in enumerate(zip(burst.gestures, burst.pauses_ms)):
        print(f"  {i+1}. {g.duration_ms}ms, pause {p}ms")
    
    print("\n=== Slow Browse ===")
    slow = gen.generate_slow_browse(count=2)
    for i, (g, p) in enumerate(zip(slow.gestures, slow.pauses_ms)):
        print(f"  {i+1}. {g.duration_ms}ms, pause {p}ms")
    
    print("\n=== FIRERPA Points Example ===")
    args = gesture_to_firerpa_args(gesture)
    print(f"Points for swipe_points(): {args[:3]}...")
