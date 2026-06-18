"""
Global Claude API rate limiter.
Ensures we never call the Anthropic API more than once every MIN_INTERVAL seconds.
Prevents runaway API costs from frequent live game polling.
"""

import time
import logging

log = logging.getLogger("polycopy.rate_limiter")

MIN_INTERVAL = float(30)  # minimum seconds between Claude API calls
_last_call_time = 0.0


def can_call_claude() -> bool:
    """Returns True if enough time has passed since the last Claude call."""
    global _last_call_time
    now = time.time()
    if now - _last_call_time >= MIN_INTERVAL:
        return True
    remaining = MIN_INTERVAL - (now - _last_call_time)
    log.debug("Claude rate limit: waiting %.1fs", remaining)
    return False


def mark_claude_called():
    """Call this immediately after making a Claude API request."""
    global _last_call_time
    _last_call_time = time.time()
