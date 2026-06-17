"""
Claude AI Trade Filter
======================
Before placing any copied trade, this module asks Claude to evaluate
the signal and decide whether to buy, skip, or reduce size.

Claude is given:
- The market question and outcome being bet on
- Current price and implied probability
- Time until market closes
- Liquidity
- How many target wallets are buying (conviction)
- Recent price direction (if available)

Claude responds with a structured JSON decision:
{
  "decision": "BUY" | "SKIP" | "REDUCE",
  "confidence": 0-100,
  "reason": "short explanation",
  "suggested_size_pct": 0-100  (% of planned size to use, 100 = full size)
}

If Claude is unavailable or returns an error, the trade proceeds normally
(fail-open, not fail-closed) so the bot keeps working.
"""

import json
import json
import logging
import requests
import time
from datetime import datetime, timezone

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    CLAUDE_MIN_CONFIDENCE,
    USE_CLAUDE_FILTER,
)

log = logging.getLogger("polycopy.claude_filter")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Track failures to back off when Claude API is struggling
_consecutive_failures = 0
_backoff_until = 0.0
MAX_BACKOFF_SECONDS = 120

SYSTEM_PROMPT = """You are an expert prediction market trader analyzing trade signals on Kalshi.

You will be given details about a trade that a top-performing trader has made, and you must decide whether to copy it.

Your job:
1. Assess whether the implied probability (current price) seems reasonable given your knowledge
2. Consider time until close — very short time = higher risk for unfavorable outcomes
3. Consider liquidity — low liquidity = harder to exit
4. Consider conviction — more top traders agreeing = stronger signal
5. Flag any obvious red flags (e.g. near-certain outcomes already priced in, extremely low probability longshots, markets closing in hours)

You must respond ONLY with a valid JSON object, no other text:
{
  "decision": "BUY" or "SKIP" or "REDUCE",
  "confidence": <integer 0-100, how confident you are in this decision>,
  "reason": "<one sentence explanation>",
  "suggested_size_pct": <integer 0-100, percentage of planned size to use>
}

Guidelines:
- BUY: signal looks good, proceed at suggested_size_pct (usually 80-100)
- REDUCE: signal has merit but some concern, proceed at suggested_size_pct (usually 30-70)  
- SKIP: signal looks poor, do not trade (suggested_size_pct = 0)
- Be decisive. Don't REDUCE everything out of caution.
- If confidence < 50, lean toward SKIP or REDUCE
- Markets closing within 6 hours are high risk — be more selective"""


def build_prompt(market_info: dict, price: float, your_size: float,
                 conviction: int, trader_bankroll: float, usdc_size: float) -> str:
    question = market_info.get("question", "Unknown")
    outcome = market_info.get("outcome", "Unknown")
    end_date = market_info.get("end_date", "")
    liquidity = market_info.get("liquidity", 0)
    category = market_info.get("category", "Unknown")
    url = market_info.get("url", "")

    # Time until close
    time_str = "unknown"
    if end_date:
        try:
            end = datetime.fromisoformat(end_date + "T23:59:00+00:00")
            now = datetime.now(timezone.utc)
            hours_left = max(0, (end - now).total_seconds() / 3600)
            if hours_left < 24:
                time_str = f"{hours_left:.1f} hours"
            else:
                time_str = f"{hours_left/24:.1f} days"
        except Exception:
            time_str = end_date

    implied_prob = price * 100
    their_pct = (usdc_size / trader_bankroll * 100) if trader_bankroll else 0

    return f"""TRADE SIGNAL TO EVALUATE:

Market: {question}
Category: {category}
Betting on: {outcome}
Current price: {price:.3f} (implied probability: {implied_prob:.1f}%)
Time until close: {time_str}
Market liquidity: ${liquidity:,.0f} USDC
Conviction: {conviction} top-ranked Kalshi trader(s) bought this outcome
Their position size: ${usdc_size:.2f} ({their_pct:.2f}% of their estimated bankroll)
Our planned size: ${your_size:.2f}
Market URL: {url}

Should we copy this trade? Respond with JSON only."""


def evaluate_trade(market_info: dict, price: float, your_size: float,
                   conviction: int, trader_bankroll: float,
                   usdc_size: float) -> dict:
    """
    Ask Claude whether to copy this trade.
    Returns a decision dict. On any error, returns a default BUY to fail open.
    """
    global _consecutive_failures, _backoff_until

    if not USE_CLAUDE_FILTER:
        return {"decision": "BUY", "confidence": 100,
                "reason": "Claude filter disabled", "suggested_size_pct": 100}

    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — Claude filter skipped")
        return {"decision": "BUY", "confidence": 100,
                "reason": "No API key", "suggested_size_pct": 100}

    # Back off if Claude has been failing repeatedly
    if time.time() < _backoff_until:
        remaining = int(_backoff_until - time.time())
        log.info("Claude filter backing off for %ds due to repeated failures — proceeding", remaining)
        return {"decision": "BUY", "confidence": 100,
                "reason": "API backoff", "suggested_size_pct": 100}

    prompt = build_prompt(market_info, price, your_size, conviction,
                          trader_bankroll, usdc_size)

    for attempt in range(3):  # retry up to 3 times
        try:
            resp = requests.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 256,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()

            content = data.get("content", [])
            if not content or not content[0].get("text", "").strip():
                log.warning("Claude filter empty response (attempt %d)", attempt + 1)
                if attempt < 2:
                    import time as _time
                    _time.sleep(2 ** attempt)  # 1s, 2s backoff
                    continue
                break

            raw_text = content[0]["text"].strip()

            # Strip markdown fences
            if "```" in raw_text:
                parts = raw_text.split("```")
                raw_text = parts[1] if len(parts) > 1 else parts[0]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
            raw_text = raw_text.strip()

            # Find JSON object within response
            start = raw_text.find("{")
            end = raw_text.rfind("}") + 1
            if start >= 0 and end > start:
                raw_text = raw_text[start:end]

            decision = json.loads(raw_text)
            decision.setdefault("decision", "BUY")
            decision.setdefault("confidence", 50)
            decision.setdefault("reason", "No reason given")
            decision.setdefault("suggested_size_pct", 100)

            # Reset failure count on success
            _consecutive_failures = 0

            log.info(
                "Claude filter: %s (confidence=%d, size_pct=%d) | %s",
                decision["decision"],
                decision["confidence"],
                decision["suggested_size_pct"],
                decision["reason"],
            )
            return decision

        except Exception as e:
            log.warning("Claude filter error (attempt %d): %s — proceeding with trade", attempt + 1, e)
            if attempt < 2:
                import time as _time
                _time.sleep(2 ** attempt)
                continue
            break

    # All retries failed — track and back off
    _consecutive_failures += 1
    if _consecutive_failures >= 3:
        backoff = min(MAX_BACKOFF_SECONDS, 30 * _consecutive_failures)
        _backoff_until = time.time() + backoff
        log.warning("Claude filter failed %d times — backing off for %ds",
                    _consecutive_failures, backoff)

    # Fail open — proceed with trade
    return {"decision": "BUY", "confidence": 100,
            "reason": "Claude unavailable — proceeding", "suggested_size_pct": 100}


def apply_decision(decision: dict, planned_size: float,
                   min_confidence: int = None) -> tuple[bool, float]:
    """
    Convert Claude's decision into (should_trade, adjusted_size).
    Returns (False, 0) to skip, or (True, adjusted_size) to proceed.
    """
    if min_confidence is None:
        min_confidence = CLAUDE_MIN_CONFIDENCE

    d = decision.get("decision", "BUY").upper()
    confidence = decision.get("confidence", 100)
    size_pct = decision.get("suggested_size_pct", 100)

    if d == "SKIP":
        return False, 0.0

    if confidence < min_confidence and d != "REDUCE":
        return False, 0.0

    adjusted = planned_size * (size_pct / 100)
    adjusted = max(adjusted, 1.0)

    return True, adjusted
