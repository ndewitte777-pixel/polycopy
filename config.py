"""
Configuration for the Kalshi copy-trading bot.

For Railway deployment, sensitive/environment-specific values are read
from environment variables (set these in Railway's Variables tab):
- TARGET_WALLETS   (comma-separated 0x addresses)
- PRIVATE_KEY      (your wallet private key - only needed if DRY_RUN=false)
- DRY_RUN          ("true" or "false")
"""

import os

# ---- Wallets to copy (top leaderboard addresses, lowercase, 0x...) ----
_targets_env = os.environ.get("TARGET_WALLETS", "")
TARGET_WALLETS = [w.strip().lower() for w in _targets_env.split(",") if w.strip()]

# ---- Polling ----
POLL_INTERVAL_SECONDS = 15        # how often to check for new activity
ACTIVITY_LOOKBACK_SECONDS = 120   # window to consider "new"
POSITION_MONITOR_INTERVAL = 60    # how often (seconds) to check open positions for exits

# ---- Filters ----
MIN_TRADE_USDC = 200              # ignore trades smaller than this (their size)
MIN_MARKET_LIQUIDITY = 1000       # skip illiquid markets
ONLY_COPY_BUYS = False

# Skip trade if price has moved more than this % since the target's tx
MAX_PRICE_SLIP_PCT = 15.0         # e.g. 15 means skip if price moved >15% from their entry

# If the same wallet trades the same token again within this many seconds, skip
SAME_TOKEN_COOLDOWN_SECONDS = 3600

# ---- Claude AI trade filter ----
# Claude reviews each trade signal before it's placed and decides buy/skip.
# Set USE_CLAUDE_FILTER=true in Railway Variables to enable.
# ANTHROPIC_API_KEY is automatically available in claude.ai artifacts but
# must be set as a Railway env var for the bot.
USE_CLAUDE_FILTER = os.environ.get("USE_CLAUDE_FILTER", "false").lower() == "true"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-6"
# Minimum confidence score (0-100) Claude must give to proceed with a trade
CLAUDE_MIN_CONFIDENCE = int(os.environ.get("CLAUDE_MIN_CONFIDENCE", "60"))

# ---- Claude autonomous trader ----
# Claude independently scans markets and places its own bets
USE_CLAUDE_TRADER = os.environ.get("USE_CLAUDE_TRADER", "false").lower() == "true"
# How often Claude scans for its own trade ideas (seconds). Default 4 hours.
CLAUDE_TRADER_INTERVAL = int(os.environ.get("CLAUDE_TRADER_INTERVAL", str(4 * 3600)))
# Minimum liquidity for Claude to consider a market ($)
CLAUDE_TRADER_MIN_LIQUIDITY = float(os.environ.get("CLAUDE_TRADER_MIN_LIQUIDITY", "5000"))
# Minimum edge (probability difference) for Claude to bet
CLAUDE_MIN_EDGE = float(os.environ.get("CLAUDE_MIN_EDGE", "0.08"))

# Minimum number of target wallets that must buy the same token within
# ---- Daily profit targets ----
# Weekday and weekend daily profit goals in USDC
WEEKDAY_PROFIT_TARGET = float(os.environ.get("WEEKDAY_PROFIT_TARGET", "5.0"))
WEEKEND_PROFIT_TARGET = float(os.environ.get("WEEKEND_PROFIT_TARGET", "10.0"))
# Once daily target is hit, switch to conservative mode (smaller sizes, higher confidence bar)
CONSERVATIVE_MODE_AFTER_TARGET = True
# How much of daily profit to protect — stop trading if we give back this much after hitting target
PROFIT_PROTECTION_PCT = float(os.environ.get("PROFIT_PROTECTION_PCT", "50.0"))


USE_LIVE_SCALPER = os.environ.get("USE_LIVE_SCALPER", "true").lower() == "true"
LIVE_POLL_INTERVAL = int(os.environ.get("LIVE_POLL_INTERVAL", "20"))  # seconds between live checks
SCALP_PROFIT_PCT = float(os.environ.get("SCALP_PROFIT_PCT", "15.0"))  # % gain to trigger scalp exit
SCALP_MIN_CENTS = float(os.environ.get("SCALP_MIN_CENTS", "0.05"))    # $0.05 absolute price gain

# ---- Market time horizon preferences ----
# Bot trades ALL Kalshi categories (sports, politics, crypto, futures, etc.)
# but prioritizes same-day and next-day markets with bigger sizes.
SAME_DAY_SIZE_MULTIPLIER  = float(os.environ.get("SAME_DAY_SIZE_MULTIPLIER",  "1.5"))
NEXT_DAY_SIZE_MULTIPLIER  = float(os.environ.get("NEXT_DAY_SIZE_MULTIPLIER",  "1.2"))
LONG_TERM_SIZE_MULTIPLIER = float(os.environ.get("LONG_TERM_SIZE_MULTIPLIER", "0.7"))
# Fraction of Claude trader's scan budget to spend on short-term markets first (0-100)
SHORT_TERM_BUDGET_PCT = float(os.environ.get("SHORT_TERM_BUDGET_PCT", "70"))
# Claude trader: min hours left on a market before considering it
CLAUDE_TRADER_MIN_HOURS_LEFT = float(os.environ.get("CLAUDE_TRADER_MIN_HOURS_LEFT", "1.0"))
# Claude trader: max days out to scan (0 = no limit)
CLAUDE_TRADER_MAX_DAYS_OUT = int(os.environ.get("CLAUDE_TRADER_MAX_DAYS_OUT", "90"))

# CONVICTION_WINDOW_SECONDS to trigger a "high conviction" multiplier.
CONVICTION_THRESHOLD = 2          # e.g. 2+ wallets buying same token = strong signal
CONVICTION_WINDOW_SECONDS = 3600  # window to look for matching buys
CONVICTION_SIZE_MULTIPLIER = 1.5  # multiply your_size by this on high conviction

# ---- Position sizing ----
COPY_SCALE_FACTOR = 1.0
MAX_TRADE_USDC = 25
MAX_DAILY_LOSS_USDC = 100
MAX_OPEN_POSITIONS = 10

# ---- Kelly criterion ----
# Use Kelly fraction for sizing instead of flat proportional copy.
# Kelly f = (p*(b+1) - 1) / b  where b = (1-price)/price (binary market odds)
# Set USE_KELLY=True to enable. KELLY_FRACTION dampens it (0.25 = quarter Kelly).
USE_KELLY = False
KELLY_FRACTION = 0.25

# ---- Auto take-profit / trailing stop ----
# Take half off the table when position value hits this multiple of cost
TAKE_PROFIT_MULTIPLIER = 2.0       # e.g. 2.0 = sell half when price doubles

# Trailing stop: sell everything if price falls this % from its peak since entry
TRAILING_STOP_PCT = 40.0           # e.g. 40 = sell if price drops 40% from peak

# Hard stop loss: sell immediately if position loses this % of entry cost
HARD_STOP_LOSS_PCT = 60.0          # e.g. 60 = sell if down 60% from entry

# ---- Time-decay exits ----
# Sell losing positions that are this close to expiry (days) AND below this price
TIME_DECAY_DAYS_LEFT = 2           # if market closes within 2 days...
TIME_DECAY_MAX_PRICE = 0.15        # ...and price is below 15c, cut losses

# ---- Category filter ----
# Only copy trades from these categories. Empty list = allow all.
# Options: POLITICS, SPORTS, CRYPTO, CULTURE, WEATHER, ECONOMICS, TECH, FINANCE
ALLOWED_CATEGORIES = []            # e.g. ["SPORTS", "CRYPTO"]

# ---- Portfolio exposure ----
# Max fraction of YOUR bankroll exposed to any single KNOWN category.
# Set to 0 to disable category limits entirely.
# Note: markets with unknown/blank category are NOT limited by this.
MAX_CATEGORY_EXPOSURE_PCT = 0.0   # disabled — category parsing unreliable until fixed

# ---- Exchange selection ----
# Set EXCHANGE=kalshi (default, works in US) or EXCHANGE=polymarket
import os as _os
EXCHANGE = _os.environ.get("EXCHANGE", "kalshi").lower()

# ---- Kalshi credentials ----
# Get from kalshi.com -> Settings -> API Keys
KALSHI_API_KEY_ID   = _os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY  = _os.environ.get("KALSHI_PRIVATE_KEY", "")
KALSHI_USE_DEMO     = _os.environ.get("KALSHI_USE_DEMO", "true").lower() == "true"


DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"

# ---- Polymarket API credentials ----
# Get these from polymarket.com -> Profile -> API Keys
# Add all three as Railway environment variables - never commit them to git
CLOB_API_KEY        = os.environ.get("CLOB_API_KEY", "")
CLOB_API_SECRET     = os.environ.get("CLOB_API_SECRET", "")
CLOB_API_PASSPHRASE = os.environ.get("CLOB_API_PASSPHRASE", "")

CLOB_API_URL     = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

# ---- Data API ----
DATA_API_URL = "https://data-api.polymarket.com"
GAMMA_API_URL = "https://gamma-api.polymarket.com"

# ---- Your account ----
YOUR_BANKROLL_USDC = float(os.environ.get("YOUR_BANKROLL_USDC", "100"))

# ---- Logging / persistence ----
DATA_DIR = os.environ.get("DATA_DIR", ".")
LOG_FILE = os.path.join(DATA_DIR, "polycopy.log")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# ---- Heartbeat ----
HEARTBEAT_SILENCE_SECONDS = int(os.environ.get("HEARTBEAT_SILENCE_SECONDS", str(24 * 3600)))

# ---- Error alerting ----
ERROR_ALERT_THRESHOLD = int(os.environ.get("ERROR_ALERT_THRESHOLD", "3"))

# ---- Weekly report ----
# Day of week for weekly summary (0=Monday, 6=Sunday)
WEEKLY_REPORT_DAY = 6
WEEKLY_REPORT_HOUR = 9  # 9am UTC
