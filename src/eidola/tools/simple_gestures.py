"""Simple human-like gestures based on recorded data.

Uses swipe(step) + fling() for simplicity and reliability.
Statistics from 69 recorded Instagram feed scrolling gestures.
"""

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lamda.client import Device, Point


@dataclass
class GestureStats:
    """Statistics from recorded gestures (my_insta_gestures_from_feed)."""
    
    # Duration (ms) - how long finger touches screen
    duration_min: int = 33
    duration_max: int = 800
    duration_avg: int = 255
    
    # Distance (px) - how far finger moves
    distance_min: int = 140
    distance_max: int = 875
    distance_avg: int = 508
    
    # X drift (px) - horizontal movement during vertical scroll
    x_drift_min: int = 23
    x_drift_max: int = 525
    x_drift_avg: int = 201
    
    # Pause between gestures (ms)
    pause_min: int = 167
    pause_max: int = 3376
    pause_avg: int = 622
    
    # Scroll back probability (~22% in data, but often in series)
    scroll_back_chance: float = 0.08  # use 8% for natural feel


class SimpleGestures:
    """Simple gesture executor using real recorded statistics.
    
    Uses:
    - swipe(A, B, step=X) for regular scrolls with speed control
    - fling_from_bottom_to_top() for fast human-like flicks
    
    Step parameter: larger = slower, smaller = faster
    - step ~10-15: fast scroll (~80-150ms feel)
    - step ~20-30: medium scroll (~200-400ms feel)  
    - step ~40-60: slow scroll (~500-800ms feel)
    """
    
    def __init__(self, device: "Device", screen_width: int = 1080, screen_height: int = 2400):
        self.d = device
        self.w = screen_width
        self.h = screen_height
        self.stats = GestureStats()
    
    def _random_x_start(self) -> int:
        """Random X near center with natural variation."""
        center = self.w // 2
        return center + random.randint(-80, 80)
    
    def _random_x_drift(self) -> int:
        """X drift based on recorded data (usually positive = slight right drift)."""
        # 70% chance drift right (natural for right-handed users)
        direction = 1 if random.random() < 0.7 else -1
        drift = random.randint(self.stats.x_drift_min, min(150, self.stats.x_drift_avg))
        return drift * direction
    
    def _random_distance(self, mode: str = "normal") -> int:
        """Distance based on mode and recorded data.
        
        Tuned based on live testing feedback.
        """
        if mode == "fast":
            # Fast fallback (when fling fails) - reduced 1.5x
            return random.randint(450, 800)
        elif mode == "slow":
            # Slow browsing = longer scrolls (2x previous for better content viewing)
            return random.randint(300, 900)
        else:
            # Normal - big scrolls: old max → new min, new max = 2x
            return random.randint(1000, 2000)
    
    def _random_step(self, mode: str = "normal") -> int:
        """Step value for speed control.
        
        Based on observed durations:
        - Fast (33-150ms) → step 8-15
        - Medium (150-400ms) → step 16-32
        - Slow (400-800ms) → step 35-55
        """
        if mode == "fast":
            return random.randint(8, 15)
        elif mode == "slow":
            return random.randint(35, 55)
        else:
            return random.randint(16, 32)
    
    def scroll_feed(self, mode: str = "normal") -> bool:
        """Single scroll down the feed.
        
        Args:
            mode: "fast", "normal", or "slow"
            
        Returns:
            True if scrolled down, False if scrolled back (8% chance)
        """
        from lamda.client import Point
        
        # Always scroll forward. Random scroll-back was causing the agent to
        # see old posts, lose context after compaction, and enter blind-tap loops.
        # Explicit scroll_back() tool is available if the agent needs it.
        
        x_start = self._random_x_start()
        x_drift = self._random_x_drift()
        distance = self._random_distance(mode)
        step = self._random_step(mode)
        
        # Scroll down - start lower on screen, swipe up
        start_y = int(self.h * random.uniform(0.70, 0.82))
        end_y = start_y - distance
        
        # Clamp coordinates
        end_x = max(50, min(self.w - 50, x_start + x_drift))
        end_y = max(100, min(self.h - 100, end_y))
        
        start = Point(x=x_start, y=start_y)
        end = Point(x=end_x, y=end_y)
        
        self.d.swipe(start, end, step=step)
        
        return True
    
    def _get_scrollable(self):
        """Get scrollable element selector for fling operations."""
        return self.d(scrollable=True)
    
    def scroll_fast(self) -> bool:
        """Fast fling - human-like quick flick.
        
        Use for burst scrolling or skipping content quickly.
        Uses native fling which is designed to feel human.
        
        Returns:
            True if fling succeeded, False otherwise
        """
        try:
            return self._get_scrollable().fling_from_bottom_to_top()
        except Exception:
            # Fallback to fast swipe if fling fails
            self.scroll_feed(mode="fast")
            return True
    
    def scroll_back(self, slow: bool = False) -> bool:
        """Scroll back up - for going back to see previous content.
        
        Args:
            slow: If True, use normal-intensity swipe instead of fast fling
            
        Returns:
            True if scroll succeeded
        """
        if slow:
            # Normal-intensity scroll back via swipe (same as 'n' command)
            from lamda.client import Point
            
            x_start = self._random_x_start()
            x_drift = self._random_x_drift()
            distance = self._random_distance("normal")  # same as n
            step = self._random_step("normal")          # same as n
            
            start_y = int(self.h * random.uniform(0.20, 0.35))
            end_y = start_y + distance
            
            end_x = max(50, min(self.w - 50, x_start + x_drift))
            end_y = min(self.h - 100, end_y)
            
            start = Point(x=x_start, y=start_y)
            end = Point(x=end_x, y=end_y)
            
            self.d.swipe(start, end, step=step)
            return True
        else:
            # Fast fling back
            try:
                return self._get_scrollable().fling_from_top_to_bottom()
            except Exception:
                # Fallback to swipe
                return self.scroll_back(slow=True)
    
    def pull_to_refresh(self) -> bool:
        """Pull-to-refresh gesture for Instagram feed.
        
        Performs a slow downward swipe from top of screen to trigger refresh.
        Must be used when already at the TOP of the feed.
        
        Returns:
            True if gesture executed
        """
        from lamda.client import Point
        
        # Start near top of screen (below status bar area)
        start_y = int(self.h * random.uniform(0.08, 0.15))
        
        # End at middle of screen
        end_y = int(self.h * random.uniform(0.45, 0.55))
        
        # Slight x variation for realism
        x_start = int(self.w * random.uniform(0.4, 0.6))
        x_drift = random.randint(-20, 20)
        end_x = x_start + x_drift
        
        start = Point(x=x_start, y=start_y)
        end = Point(x=end_x, y=end_y)
        
        # Slow, deliberate pull gesture (higher step = slower)
        step = random.randint(35, 50)
        
        self.d.swipe(start, end, step=step)
        return True
    
    def scroll_burst(self, count: int = None) -> int:
        """Execute burst of fast scrolls.
        
        Args:
            count: Number of scrolls (default: random 2-4)
            
        Returns:
            Number of scrolls executed
        """
        import time
        
        if count is None:
            count = random.randint(2, 4)
        
        for i in range(count):
            self.scroll_fast()
            
            # Short pause between burst scrolls (100-300ms)
            if i < count - 1:
                pause = random.randint(100, 300) / 1000.0
                time.sleep(pause)
        
        return count
    
    def scroll_slow_browse(self) -> None:
        """Slow scroll for reading/viewing content."""
        self.scroll_feed(mode="slow")
    
    def watch_media(self, media_type: str = "photo") -> dict:
        """Wait appropriate duration for media type (simulates viewing content).
        
        Watch times minimal - agent processing (3-8s) provides natural delay:
        - Photo: 0.5-1.5 seconds (agent adds 3-8s on top)
        - Video: 5-13 seconds (view counts after 5s, agent adds overhead)
        - Carousel item: 0.5-1.5 seconds (agent adds 3-8s on top)
        
        Call this BEFORE interacting (like, comment) to simulate reading/viewing.
        
        Args:
            media_type: "photo", "video", or "carousel"
            
        Returns:
            dict with duration watched in seconds
        """
        import time
        
        if media_type == "video":
            # Video: 5-13 seconds (view counts after 5s, agent adds 3-8s overhead)
            duration = random.uniform(5.0, 13.0)
        elif media_type == "carousel":
            # Carousel: minimal (agent processing adds 3-8s naturally)
            duration = random.uniform(0.5, 1.5)
        else:  # photo
            # Photo: minimal (agent processing adds 3-8s naturally)
            duration = random.uniform(0.5, 1.5)
        
        time.sleep(duration)
        
        return {
            "watched": True,
            "media_type": media_type,
            "duration_sec": round(duration, 2),
        }
    
    def random_pause(self, mode: str = "normal") -> float:
        """Get random pause duration based on recorded data.
        
        Args:
            mode: "fast" (100-400ms), "normal" (300-1000ms), "slow" (800-2500ms)
            
        Returns:
            Pause duration in seconds
        """
        if mode == "fast":
            ms = random.randint(100, 400)
        elif mode == "slow":
            ms = random.randint(800, 2500)
        else:
            ms = random.randint(self.stats.pause_min, 1000)
        
        return ms / 1000.0
    
    def maybe_scroll_back(self) -> bool:
        """With 8% probability, scroll back up.
        
        Returns:
            True if scrolled back, False if not
        """
        if random.random() < self.stats.scroll_back_chance:
            self.scroll_back()
            return True
        return False
    
    def scroll_precise(self, target_distance: int, variability_percent: int = 15) -> dict:
        """Execute precise scroll with human-like variability.
        
        Scrolls approximately the target distance, but with random variation
        to avoid bot detection. Adds natural X drift and speed variation.
        
        Args:
            target_distance: Target scroll distance in pixels (positive = down, negative = up)
            variability_percent: Random variation percentage (default 15% = ±15%)
            
        Returns:
            dict with:
            - actual_distance: Actual distance scrolled (with variability)
            - direction: "down" or "up"
            - variability_applied: Pixels of variability added
        """
        from lamda.client import Point
        
        # Apply variability: ±variability_percent
        # e.g., target=500, variability=15% → actual could be 425-575
        variability_range = abs(target_distance) * variability_percent / 100
        variability = random.uniform(-variability_range, variability_range)
        actual_distance = int(target_distance + variability)
        
        # Determine direction
        scroll_down = actual_distance > 0
        abs_distance = abs(actual_distance)
        
        # Clamp to reasonable range
        abs_distance = max(100, min(1800, abs_distance))
        
        # Natural X coordinates with drift
        x_start = self._random_x_start()
        x_drift = self._random_x_drift()
        end_x = max(50, min(self.w - 50, x_start + x_drift))
        
        if scroll_down:
            # Scroll down: finger moves up (start lower, end higher)
            start_y = int(self.h * random.uniform(0.70, 0.82))
            end_y = start_y - abs_distance
        else:
            # Scroll up: finger moves down (start higher, end lower)
            start_y = int(self.h * random.uniform(0.25, 0.35))
            end_y = start_y + abs_distance
        
        # Clamp Y coordinates
        end_y = max(100, min(self.h - 100, end_y))
        
        # Randomize speed (step) for natural feel
        # Medium-slow for precision scrolling
        step = random.randint(20, 35)
        
        start = Point(x=x_start, y=start_y)
        end = Point(x=end_x, y=end_y)
        
        self.d.swipe(start, end, step=step)
        
        return {
            "actual_distance": actual_distance,
            "direction": "down" if scroll_down else "up",
            "variability_applied": int(variability),
            "step": step,
        }
    
    def double_tap_like(self, bounds: tuple[int, int, int, int], padding: float = 0.3) -> dict:
        """Double-tap to like post (Instagram native gesture).
        
        Based on GramAddict implementation: 50-140ms between taps, 30% padding from edges.
        ~70% of real users prefer this over like button.
        
        Args:
            bounds: (x1, y1, x2, y2) of tappable area (usually post image)
            padding: Fraction of width/height to avoid edges (default 0.3 = 30%)
            
        Returns:
            dict with double_tapped, position, delay_ms
        """
        import time
        
        x1, y1, x2, y2 = bounds
        
        # Validate bounds
        if x2 <= x1 or y2 <= y1:
            # Invalid bounds - tap center of screen as fallback
            random_x = self.w // 2 + random.randint(-50, 50)
            random_y = self.h // 2 + random.randint(-50, 50)
        else:
            horizontal_len = x2 - x1
            vertical_len = y2 - y1
            
            # Clamp padding to ensure valid range (max 49%)
            effective_padding = min(padding, 0.49)
            h_padding = int(effective_padding * horizontal_len)
            v_padding = int(effective_padding * vertical_len)
            
            # Ensure we have a valid range for randint
            if x1 + h_padding >= x2 - h_padding:
                random_x = (x1 + x2) // 2  # Bounds too small, tap center
            else:
                random_x = random.randint(x1 + h_padding, x2 - h_padding)
            
            if y1 + v_padding >= y2 - v_padding:
                random_y = (y1 + y2) // 2  # Bounds too small, tap center
            else:
                random_y = random.randint(y1 + v_padding, y2 - v_padding)
        
        # Human-like SMALL drift between taps (research-based)
        # Instagram DOUBLE_TAP threshold: ~24px max distance
        # Human finger natural variance: 2-8px with normal distribution
        # Using Gaussian: mean=0, std=3px, clamped to ±8px max
        # This gives realistic micro-tremor without exceeding Instagram's threshold
        drift_x = int(max(-8, min(8, random.gauss(0, 3))))
        drift_y = int(max(-8, min(8, random.gauss(0, 3))))
        tap2_x = max(50, min(self.w - 50, random_x + drift_x))
        tap2_y = max(100, min(self.h - 100, random_y + drift_y))
        
        # Double-tap timing: 30-60ms between taps (faster for video posts)
        # Instagram video: single tap opens Reels, so we need FAST double-tap
        # Using sleep with decimal for sub-second precision
        time_between_sec = random.randint(30, 60) / 1000.0  # 0.03 to 0.06 seconds
        time_between_ms = int(time_between_sec * 1000)
        
        # Use parallel input tap execution to avoid JVM overhead between taps
        # Pattern: "input tap X1 Y1 & sleep 0.05 && input tap X2 Y2"
        # - First tap runs in background (&)
        # - sleep adds delay between taps
        # - Second tap runs after sleep completes
        # This works on virtual devices (uinput-goodix) where sendevent doesn't work
        cmd = f"input tap {random_x} {random_y} & sleep {time_between_sec:.3f} && input tap {tap2_x} {tap2_y}"
        
        try:
            self.d.execute_script(cmd)
        except Exception as e:
            return {
                "error": f"Exception: {str(e)}",
                "tap1": (random_x, random_y),
                "tap2": (tap2_x, tap2_y),
            }
        
        result = {
            "double_tapped": True,
            "tap1": (random_x, random_y),
            "tap2": (tap2_x, tap2_y),
            "drift": (drift_x, drift_y),
            "delay_ms": time_between_ms,
            "method": "input_tap_parallel",  # Parallel input tap execution
        }
        
        return result
    
    def tap_right_edge(self, y_fraction: float = 0.5) -> dict:
        """Tap right edge of screen (e.g., to advance stories).
        
        Args:
            y_fraction: Vertical position as fraction of screen height (default 0.5 = middle)
            
        Returns:
            dict with tapped position
        """
        from lamda.client import Point
        
        # Clamp y_fraction to valid range
        y_fraction = max(0.05, min(0.95, y_fraction))
        
        # Right edge with small margin
        x = self.w - random.randint(50, 100)
        y = int(self.h * y_fraction) + random.randint(-50, 50)
        
        # Clamp Y to screen bounds
        y = max(50, min(self.h - 50, y))
        
        self.d.click(Point(x=x, y=y))
        
        return {"tapped": True, "position": (x, y), "edge": "right"}
    
    def tap_left_edge(self, y_fraction: float = 0.5) -> dict:
        """Tap left edge of screen (e.g., to go back in stories).
        
        Args:
            y_fraction: Vertical position as fraction of screen height (default 0.5 = middle)
            
        Returns:
            dict with tapped position
        """
        from lamda.client import Point
        
        # Clamp y_fraction to valid range
        y_fraction = max(0.05, min(0.95, y_fraction))
        
        # Left edge with small margin
        x = random.randint(50, 100)
        y = int(self.h * y_fraction) + random.randint(-50, 50)
        
        # Clamp Y to screen bounds
        y = max(50, min(self.h - 50, y))
        
        self.d.click(Point(x=x, y=y))
        
        return {"tapped": True, "position": (x, y), "edge": "left"}
    
    def swipe_carousel(self, bounds: tuple[int, int, int, int], direction: str = "left") -> dict:
        """Swipe horizontally within carousel bounds.
        
        Parameters for reliable Instagram carousel page turn:
        - Start X: 90-95% of width (near right edge)
        - Distance: 70-85% of width (long swipe needed for Instagram)
        - Speed: step 6-10 (fast swipe)
        - Y variation: ±15px (minimal vertical drift)
        
        Args:
            bounds: (x1, y1, x2, y2) of carousel/image area
            direction: "left" (next page) or "right" (previous page)
            
        Returns:
            dict with swipe details
        """
        from lamda.client import Point
        
        x1, y1, x2, y2 = bounds
        center_y = (y1 + y2) // 2
        width = x2 - x1
        height = y2 - y1
        
        # Minimal Y variation (keeps swipe horizontal)
        y_drift = random.randint(-15, 15)
        start_y = center_y + random.randint(-5, 5)
        end_y = start_y + y_drift
        
        # Clamp Y to bounds
        start_y = max(y1 + 50, min(y2 - 50, start_y))
        end_y = max(y1 + 50, min(y2 - 50, end_y))
        
        # Swipe distance (70-85% of width - Instagram needs long swipe!)
        swipe_distance = int(width * random.uniform(0.70, 0.85))
        
        if direction == "left":
            # Swipe left = finger moves right-to-left = see next page
            # Start from 90-95% position (near right edge)
            start_x = x1 + int(width * random.uniform(0.90, 0.95))
            end_x = start_x - swipe_distance
        else:
            # Swipe right = finger moves left-to-right = see previous page
            start_x = x1 + int(width * random.uniform(0.05, 0.10))
            end_x = start_x + swipe_distance
        
        # Clamp X to screen bounds
        start_x = max(50, min(self.w - 50, start_x))
        end_x = max(50, min(self.w - 50, end_x))
        
        start = Point(x=start_x, y=start_y)
        end = Point(x=end_x, y=end_y)
        
        # Fast swipe speed (step 6-10 for reliable page turn)
        step = random.randint(6, 10)
        
        self.d.swipe(start, end, step=step)
        
        return {
            "swiped": True,
            "direction": direction,
            "distance": abs(end_x - start_x),
            "y_drift": abs(end_y - start_y),
            "step": step,
        }


# =============================================================================
# Factory function for easy integration
# =============================================================================

def create_simple_gestures(device: "Device") -> SimpleGestures:
    """Create SimpleGestures with auto-detected screen dimensions.
    
    Args:
        device: Connected FIRERPA device
        
    Returns:
        Configured SimpleGestures instance
    """
    info = device.device_info()
    width = getattr(info, 'displayWidth', 1080)
    height = getattr(info, 'displayHeight', 2400)
    
    return SimpleGestures(device, screen_width=width, screen_height=height)


# =============================================================================
# Demo / Test
# =============================================================================

if __name__ == "__main__":
    print("SimpleGestures - based on 69 recorded gestures")
    print()
    
    stats = GestureStats()
    print("Recorded Statistics:")
    print(f"  Duration: {stats.duration_min}-{stats.duration_max}ms (avg {stats.duration_avg})")
    print(f"  Distance: {stats.distance_min}-{stats.distance_max}px (avg {stats.distance_avg})")
    print(f"  X drift: {stats.x_drift_min}-{stats.x_drift_max}px (avg {stats.x_drift_avg})")
    print(f"  Pause: {stats.pause_min}-{stats.pause_max}ms (avg {stats.pause_avg})")
    print(f"  Scroll back chance: {stats.scroll_back_chance*100}%")
    print()
    
    print("Step mapping (simulated speed):")
    print("  Fast (33-150ms feel):   step 8-15")
    print("  Normal (150-400ms feel): step 16-32")
    print("  Slow (400-800ms feel):   step 35-55")
    print()
    
    print("Usage:")
    print("  gestures = create_simple_gestures(device)")
    print("  gestures.scroll_feed()           # normal scroll")
    print("  gestures.scroll_feed('fast')     # fast scroll")
    print("  gestures.scroll_feed('slow')     # slow browse")
    print("  gestures.scroll_fast()           # native fling (burst)")
    print("  gestures.scroll_burst(3)         # 3 fast scrolls in sequence")
    print("  gestures.maybe_scroll_back()     # 8% chance scroll back")
