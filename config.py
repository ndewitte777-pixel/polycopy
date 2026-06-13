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
# Set via env var TARGET_WALLETS="0xaaa...,0xbbb...,0xccc..."
_targets_env = os.environ.get("TARGET_WALLETS", "")
TARGET_WALLETS = [w.strip().lower() for w in _targets_env.split(",") if w.strip()]

# ---- Polling ----
POLL_INTERVAL_SECONDS = 10        # how often to check for new activity
ACTIVITY_LOOKBACK_SECONDS = 120   # window to consider "new"

# ---- Filters ----
MIN_TRADE_USDC = 50          # ignore trades smaller than this (their size)
MIN_MARKET_LIQUIDITY = 1000  # skip markets with less liquidity than this (USDC)
ONLY_COPY_BUYS = False        # if True, ignore SELL/close activity

# ---- Position sizing ----
# Fraction of YOUR bankroll to allocate per copied trade,
# scaled by the fraction of THEIR bankroll the trade represents.
COPY_SCALE_FACTOR = 1.0      # 1.0 = match their % allocation exactly
MAX_TRADE_USDC = 25          # hard cap per trade regardless of scaling
MAX_DAILY_LOSS_USDC = 100    # kill switch
MAX_OPEN_POSITIONS = 10

# ---- Execution ----
# IMPORTANT: keep DRY_RUN=true until you've verified logic.
# Set env var DRY_RUN=false on Railway to go live.
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"

# ---- Wallet / credentials (CLOB) ----
# Set via env var on Railway. Never commit real keys to git.
PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
CLOB_API_URL = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

# ---- Data API ----
DATA_API_URL = "https://data-api.polymarket.com"
GAMMA_API_URL = "https://gamma-api.polymarket.com"

# ---- Logging ----
LOG_FILE = "polycopy.log"
STATE_FILE = "state.json"
