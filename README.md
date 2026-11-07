# ForexFactory Discord Alerts Bot

A Discord bot that posts **economic calendar alerts** for forex pairs, built on
top of the [ForexFactoryScrapper](https://github.com/AtaCanYmc/ForexFactoryScrapper)
project's data domain.

It sends:

- 🔔 **Pre-event reminders** (e.g. 60 and 15 minutes before high-impact events)
- 📅 A **daily digest** at a time you choose
- 🔴🟠🟡 **On-demand slash commands** for any day or week, filtered by pair,
  currency, and impact

All times are rendered with Discord timestamps, so every member sees them in
their own local timezone.

---

## Why the official JSON feed?

The cloned scrapper repo parses ForexFactory HTML with `forex-pytory`. During
testing, two problems made it unsuitable for scheduling:

1. ForexFactory's HTML timestamps are rendered in the **requester's**
   timezone (it showed CET on this machine), so "8:30am" does not reliably mean
   US Eastern.
2. Cloudflare returns **403** to the repo's bundled `cloudscraper` client. A
   browser-impersonating client (`curl_cffi`) works, but the timezone problem
   remains.

The bot therefore reads ForexFactory's official weekly JSON feeds
(`ff_calendar_thisweek.json`) as its primary source, the same data the site
uses. Every event carries an explicit UTC offset, which is exactly what an
alert scheduler needs. The JSON feed only covers the current week, so an HTML
fallback (a small in-tree parser based on the repo's `forex-pytory` logic)
fills in dates outside that coverage. The fallback infers the site's current
UTC offset from the clock in the calendar header, which sidesteps the timezone
problem described above. Keeping the parser in-tree also avoids the
`forex-pytory` → `pandas` dependency, which has no wheels for newer Python
versions such as 3.14.

---

## 1. Create the Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications)
   and click **New Application**.
2. Go to **Bot** → **Reset Token** → copy the token.
3. No privileged intents are required.
4. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `View Channels`, `Send Messages`, `Embed Links`
5. Open the generated URL and invite the bot to your server.

## 2. Configure

**Windows (PowerShell):**

```powershell
cd C:\Users\Administrator\forex-discord-bot
copy .env.example .env
notepad .env
```

> On this machine Python 3.12 is installed at
> `C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe`.
> Open a new terminal after installation if `python` is not on PATH yet.

**Linux / macOS:**

```bash
git clone https://github.com/jobmwitah/forex-discord-bot.git
cd forex-discord-bot
cp .env.example .env
nano .env        # or: vim .env
```

Set at least:

```env
DISCORD_TOKEN=your-token-here
GUILD_ID=your-server-id        # optional, makes /commands appear instantly
```

## 3. Install and run

### Windows

```powershell
# one-time setup (already done on this machine)
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# start the bot
.venv\Scripts\python bot.py
```

Or double-click `run.bat`.

### Linux / macOS

Requires Python 3.10+ (tested on 3.12 and 3.14). All dependencies ship as
wheels, so no compiler or `pandas` build is needed.

```bash
# Debian/Ubuntu only: install Python and venv support if missing
sudo apt update
sudo apt install -y python3 python3-venv python3-pip

# one-time setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# start the bot
python bot.py
```

Or without activating the virtual environment:

```bash
.venv/bin/python bot.py
```

### Test the data feed without a token

```bash
# Linux / macOS
.venv/bin/python bot.py --preview --hours 72 --impact high
.venv/bin/python bot.py --preview --pair EURUSD --impact medium
```

```powershell
# Windows
.venv\Scripts\python bot.py --preview --hours 72 --impact high
.venv\Scripts\python bot.py --preview --pair EURUSD --impact medium
```

## 4. Configure alerts in Discord

```
/setup channel:#alerts timezone:Europe/Istanbul currencies:EUR,USD,GBP,JPY impact:High digest:08:00 reminders:60,15
```

All options are optional; run `/setup` with no options (or `/settings`) to see
the current configuration.

| Command | What it does |
| --- | --- |
| `/calendar [date] [pair] [currencies] [impact]` | Events for one day. `date` accepts `YYYY-MM-DD`, `today`, `tomorrow`. |
| `/next [hours] [pair] [currencies] [impact]` | Upcoming events, up to 168 hours ahead. |
| `/week [pair] [currencies] [impact]` | The next seven days. |
| `/pairs` | List supported majors/crosses and explain pair filtering. |
| `/settings` | Show this server's alert configuration (ephemeral). |
| `/setup ...` | Change channel, currencies, impact threshold, digest time, reminders, timezone, enabled. Requires **Manage Server**. |
| `/test` | Send a test alert to the configured channel. |

### Pair filtering

`pair:EURUSD` matches **both** currencies, so you see every EUR and USD event.
Any six-letter pair works: `GBPJPY`, `USDTRY`, `EUR/USD`, etc. To filter by
currency directly, use `currencies:USD,EUR` or `currencies:all`.

### Alert behaviour

- The bot checks the calendar every 30 seconds and refreshes the feed every
  10 minutes (`FEED_TTL_SECONDS`).
- Reminders use the same currency/impact filter as the digest.
- Sent alerts are recorded in `data/state.json`, so restarting the bot will not
  repeat messages that were already delivered.
- The daily digest is scheduled in the server's configured timezone, but the
  times inside the embed render in each member's own timezone.

## Running the bot long-term on Linux

Use a `systemd` service so the bot starts on boot and restarts after crashes.

```bash
sudo tee /etc/systemd/system/forex-bot.service > /dev/null <<'EOF'
[Unit]
Description=ForexFactory Discord alerts bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/forex-discord-bot
ExecStart=/home/YOUR_USER/forex-discord-bot/.venv/bin/python bot.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now forex-bot
sudo systemctl status forex-bot
```

Replace `YOUR_USER` with your Linux username and adjust the paths if you cloned
the repo somewhere else. Useful commands:

```bash
journalctl -u forex-bot -f        # live logs
sudo systemctl restart forex-bot  # after editing .env
sudo systemctl stop forex-bot
```

On macOS, use `launchd` or run it inside `tmux`/`screen` instead.

## Configuration reference

| Variable | Default | Description |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | **Required.** Bot token. |
| `GUILD_ID` | empty | Optional test server ID for instant command sync. |
| `ALERT_CHANNEL_ID` | empty | Optional fallback channel; `/setup` is preferred. |
| `DEFAULT_CURRENCIES` | `EUR,USD,GBP,JPY,AUD,CAD,CHF,NZD` | Currencies for new servers. |
| `DEFAULT_MIN_IMPACT` | `high` | `low`, `medium`, `high`, or `all`. |
| `DEFAULT_DIGEST_TIME` | `08:00` | Daily digest time (`HH:MM`) or `off`. |
| `DEFAULT_REMINDERS` | `15,60` | Minutes before events; comma separated, or `off`. |
| `DEFAULT_TIMEZONE` | `UTC` | IANA timezone used for digest scheduling. |
| `FEED_TTL_SECONDS` | `600` | Calendar feed cache duration. |

Per-server settings are stored in `data/settings.json`.

## Project layout

```
forex-discord-bot/
├── bot.py               # Discord commands, alert scheduler, CLI preview
├── calendar_source.py   # ForexFactory feed client, event model, filters
├── html_parser.py       # HTML fallback parser (BeautifulSoup only)
├── config.py            # env defaults + per-guild JSON settings/state
├── formatting.py        # Discord embeds and impact styling
├── requirements.txt
├── .env.example
└── run.bat              # Windows launcher (Linux: use systemd, see above)
```

## Troubleshooting

- **Slash commands don't appear immediately** — set `GUILD_ID` in `.env` and
  restart; commands are then synced to that server instantly.
- **No alerts** — run `/test`, check the result of `/settings`, and make sure
  the bot has View Channel, Send Messages and Embed Links in the target channel.
- **Feed error in logs** — the bot keeps serving the last cached calendar. Check
  internet access to `nfs.faireconomy.media`.
- **pip tries to build pandas** — an older revision depended on `forex-pytory`.
  Run `git pull` and `pip install -r requirements.txt` again; the current
  version requires no pandas.
- **Times look wrong** — check `timezone` in `/settings`; it only controls when
  the digest runs and which calendar day a command means. Embed timestamps are
  always rendered locally by Discord.

## Notes and limits

- ForexFactory's weekly feed covers the current week. The HTML fallback fills in
  dates outside that window (for example next week's events in `/week`), with a
  six-hour cache per day.
- The feed does not expose released **actual** values, so embeds show forecast
  and previous values. The HTML fallback does capture actual values when
  available, but embeds still show forecast/previous for consistency.
- Respect ForexFactory's terms of use and keep the refresh interval reasonable.
