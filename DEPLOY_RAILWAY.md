# Deploying to Railway

## 1. Push the code to GitHub

Railway deploys from a GitHub repo.

```bash
cd polycopy
git init
git add .
git commit -m "Initial commit"
```

Create a new repo on GitHub (e.g. `polycopy`), then:

```bash
git remote add origin https://github.com/<your-username>/polycopy.git
git branch -M main
git push -u origin main
```

(The `.gitignore` already excludes `state.json`, logs, and `__pycache__`.)

## 2. Create the Railway project

1. Go to https://railway.app and sign in (GitHub login is easiest).
2. Click **New Project** → **Deploy from GitHub repo**.
3. Select your `polycopy` repo. Railway will detect it's a Python project
   via `requirements.txt`.

## 3. Set the start command

Railway should pick up the `Procfile` (`worker: python bot.py`) automatically.
If it instead tries to run a web server / fails health checks:

- Go to your service → **Settings** → **Deploy**
- Under **Custom Start Command**, set:
  ```
  python bot.py
  ```
- Under **Networking**, you can disable/ignore the public domain — this is a
  background worker, not a web service, so it doesn't need a port.

## 4. Set environment variables

Go to your service → **Variables** tab → add:

| Variable | Value | Notes |
|---|---|---|
| `TARGET_WALLETS` | `0xaaa...,0xbbb...,0xccc...` | Comma-separated, no spaces, lowercase |
| `DRY_RUN` | `true` | Keep `true` until you've watched logs for a while |
| `PRIVATE_KEY` | (leave empty for now) | Only set when going live |

Click **Deploy** (or it auto-redeploys on variable changes).

## 5. Watch the logs

Service → **Deployments** → click the active deployment → **View Logs**.

You should see:
```
Starting Polymarket copy bot. DRY_RUN=True, targets=['0x...']
```
and periodic `COPY SIGNAL` lines when target wallets make qualifying trades.

If you see `Failed to fetch leaderboard/activity: 403/404`, the API shape may
have shifted — check https://docs.polymarket.com and adjust `data_api.py`.

## 6. Going live (real money)

1. Get your Polymarket wallet's private key (Settings → export, for the
   embedded Apple/Google/email wallet).
2. In Railway Variables, set `PRIVATE_KEY` to that value.
3. Set `DRY_RUN` to `false`.
4. Redeploy. Watch logs closely. Start with a very low `MAX_TRADE_USDC`
   (edit `config.py`, commit, push) — e.g. `1` or `2` — before scaling up.

**Security note:** Railway environment variables are encrypted at rest but
are still a private key sitting on a third-party platform. Use a wallet
funded only with money you're prepared to lose entirely, and never reuse
this private key elsewhere.

## 7. Persisting state across restarts (optional)

`state.json` currently lives on ephemeral local disk — it resets on every
redeploy/restart, meaning previously-seen trades could be reprocessed.
To persist it:

- Add a [Railway Volume](https://docs.railway.com/reference/volumes) mounted
  at e.g. `/data`, and change `STATE_FILE` in `config.py` to
  `/data/state.json`.

This is optional but recommended for a long-running bot.

## 8. Stopping the bot

Service → **Settings** → **Remove Service**, or just pause/scale it down to
0 replicas, or set `TARGET_WALLETS` to empty (the bot will exit immediately
on each restart, which Railway will then keep retrying — better to actually
pause the service).
