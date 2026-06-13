"""
Helper: fetch Polymarket's leaderboard so you can pick TARGET_WALLETS for bot.py.

Usage:
    python find_targets.py
"""

from data_api import DataAPI

def main():
    api = DataAPI()
    board = api.get_leaderboard(limit=20)

    if not board:
        print("Could not fetch leaderboard. The endpoint/params may have changed -- "
              "check https://docs.polymarket.com for the current /leaderboard spec, "
              "or browse https://polymarket.com/leaderboard manually and copy "
              "wallet addresses from trader profile URLs.")
        return

    print(f"{'Rank':<5}{'Wallet':<45}{'PnL':<15}{'Volume':<15}")
    for entry in board:
        wallet = entry.get("proxyWallet", "?")
        rank = entry.get("rank", "?")
        pnl = entry.get("pnl", 0)
        vol = entry.get("vol", 0)
        print(f"{rank:<5}{wallet:<45}{pnl:<15.2f}{vol:<15.2f}")

    print("\nCopy the wallet addresses you want into TARGET_WALLETS in config.py")


if __name__ == "__main__":
    main()
