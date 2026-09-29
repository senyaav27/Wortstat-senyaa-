# TVOЁ + Wordstat Telegram bot

Private analytics for TVOЁ's `willPublishedSoon` collection. The bot checks Wordstat demand and dynamics, stores measurement history in SQLite, and only serves the configured Telegram user ID. Python 3.11+, standard library only.

## Configure and run

Set these variables in the hosting provider's environment settings. Never commit real secret values:

- `YANDEX_API_KEY`
- `YANDEX_FOLDER_ID`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_ID`
- Optional: `DATABASE_PATH` (must point to a persistent disk), `TOP_BUDGET` (default 100), `DYNAMICS_BUDGET` (default 12)

Run `python tvoe_wordstat_bot.py`. Keep exactly one worker because Telegram long polling cannot be shared by parallel instances. Attach a persistent disk for SQLite history. Open the bot in Telegram and press Start once.

Commands: `/start`, `/top`, `/growth`, `/soon`, `/new`, `/title Name`, `/refresh`, `/status`. A daily scheduler refreshes the catalog. Ambiguous titles are marked for manual review. Wordstat counts search requests for a phrase, not views of the film itself.

## Deploy on Railway

Create a new private GitHub repository and upload the files from this archive directly into its root. In Railway, create a service from that GitHub repository. The Dockerfile starts the bot. Add the four required environment variables in Railway Variables, set `DATABASE_PATH` to a mounted volume location such as `/data/tvoe.sqlite3`, attach a persistent volume mounted at `/data`, then deploy. Do not enable multiple replicas.

## Limits

TVOЁ and Wordstat real API connectivity must be verified in the deployed environment. The service retries temporary errors and stops a Wordstat pass on HTTP 429. Daily checking is capped by the budget variables.
