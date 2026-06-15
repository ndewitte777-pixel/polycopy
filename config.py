"""
Configuration for the Polymarket copy-trading bot.

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

# ---- Conviction scoring ----
# Minimum number of target wallets that must buy the same token within
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
# Max fraction of YOUR bankroll exposed to any single category
MAX_CATEGORY_EXPOSURE_PCT = 40.0   # e.g. 40 = max 40% in sports markets at once

# ---- Execution ----
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"

# ---- Polymarket API credentials ----
# Get these from polymarket.com -> Profile -> API Keys
# Add all three as Railway environment variables - never commit them to git
CLOB_API_KEY        = os.environ.get("CLOB_API_KEY", "")
CLOB_API_SECRET     = os.environ.get("CLOB_API_SECRET", "")
CLOB_API_PASSPHRASE = os.environ.get("CLOB_API_PASSPHRASE", "")  # may be empty, that's ok

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
