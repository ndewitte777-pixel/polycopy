# Polymarket Copy-Trading Bot

Monitors top-leaderboard wallets and replicates their trades proportionally
on your account.

## ⚠️ Read first

- Default mode is `DRY_RUN = True` — it logs what it *would* do but places no
  real orders. Run it this way for at least a few days before going live.
- "High win rate" ≠ profitable. Check a trader's actual PnL history, not just
  win %, before copying them.
- Copying introduces lag and slippage. On illiquid markets your fill price
  will be worse than theirs, which can flip a positive edge negative.
- Set `MAX_DAILY_LOSS_USDC`, `MAX_TRADE_USDC`, and `MAX_OPEN_POSITIONS`
  conservatively and don't fund the wallet with more than you can lose.

## Setup

1. Install dependencies:
   ```
   pip install requests py-clob-client --break-system-packages
   ```

2. Find target wallets:
   ```
   python find_targets.py
   ```
   If the `/leaderboard` endpoint format has changed (APIs evolve), browse
   https://polymarket.com/leaderboard manually — each trader's profile URL
   contains their wallet address (0x...). Paste 1-3 addresses into
   `TARGET_WALLETS` in `config.py`.

3. Review and adjust `config.py`:
   - `TARGET_WALLETS` — addresses to copy
   - `MIN_TRADE_USDC`, `ONLY_COPY_BUYS` — filter noise
   - `COPY_SCALE_FACTOR`, `MAX_TRADE_USDC` — position sizing
   - `MAX_DAILY_LOSS_USDC`, `MAX_OPEN_POSITIONS` — kill switches
   - Leave `DRY_RUN = True` for now

4. Run it:
   ```
   python bot.py
   ```
   Watch `polycopy.log` for "COPY SIGNAL" lines — these show what trades it
   detected and what size it would copy at.

## Going live

1. Get your Polymarket wallet's private key (Settings → export, if using the
   embedded/Magic wallet) and your funder/proxy wallet address.
2. In `config.py`:
   - Set `PRIVATE_KEY = "0x..."`
   - Set `DRY_RUN = False`
3. `executor.py` uses `signature_type=1` (default for email/Apple/Google
   sign-up wallets). If you used a browser extension wallet (MetaMask), you
   may need `signature_type=0` and to set token allowances first — see
   py-clob-client's README on GitHub.
4. Start with tiny `MAX_TRADE_USDC` (e.g. $1-2) to confirm real orders work
   before scaling up.

## Files

- `config.py` — all settings
- `data_api.py` — public read-only API calls (leaderboard, activity, positions)
- `executor.py` — order placement via py-clob-client
- `state.py` — persistence (seen trades, daily loss tracking)
- `bot.py` — main loop
- `find_targets.py` — leaderboard lookup helper

## Known limitations / TODO

- `estimate_trader_bankroll()` and `your_bankroll` are rough placeholders —
  replace with real position-value lookups for accurate proportional sizing.
- No websocket support yet; polls REST every `POLL_INTERVAL_SECONDS`.
- API endpoint shapes (especially `/leaderboard`) may drift — verify against
  https://docs.polymarket.com if `find_targets.py` returns nothing.
- No automatic exit logic — currently only copies entries/exits the target
  makes themselves (i.e., if they sell, you sell proportionally).
