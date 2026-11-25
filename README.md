# ForexFactory Discord Alerts Bot

A Discord bot that posts **economic calendar alerts** for forex pairs, built on
top of the [ForexFactoryScrapper](https://github.com/AtaCanYmc/ForexFactoryScrapper)
project's data domain.

It sends:

- 🔔 **Pre-event reminders** (e.g. 60 and 15 minutes before high-impact events)
- 🚨 A **release alert at T-0**, the moment the event is published
- 📊 A **post-release summary** comparing the actual result with the forecast
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
the current configuration. Release alerts and release summaries are enabled by
default.

Preview the three message types with `/test` (all of them) or individually,
e.g. `/test kind:release` or `/test kind:summary`.

| Command | What it does |
| --- | --- |
| `/calendar [date] [pair] [currencies] [impact]` | Events for one day. `date` accepts `YYYY-MM-DD`, `today`, `tomorrow`. |
| `/next [hours] [pair] [currencies] [impact]` | Upcoming events, up to 168 hours ahead. |
| `/week [pair] [currencies] [impact]` | The next seven days. |
| `/pairs` | List supported majors/crosses and explain pair filtering. |
| `/settings` | Show this server's alert configuration (ephemeral). |
| `/setup ...` | Change channel, currencies, impact threshold, digest time, reminders, timezone, release alert/summary toggles, enabled. Requires **Manage Server**. |
| `/test [kind] [social]` | Send a test message to the configured channel. `kind` can be `all`, `reminder`, `release` or `summary`; `social:true` also posts to X/Bluesky. |

### Pair filtering

`pair:EURUSD` matches **both** currencies, so you see every EUR and USD event.
Any six-letter pair works: `GBPJPY`, `USDTRY`, `EUR/USD`, etc. To filter by
currency directly, use `currencies:USD,EUR` or `currencies:all`.

### Alert behaviour

- The bot checks the calendar every 10 seconds and refreshes the feed every
  10 minutes (`FEED_TTL_SECONDS`).
- **Reminders** fire at the configured offsets before the event (`15,60` by
  default).
- **Release alerts** fire at `T-0`, within a 90-second window after the event
  time. Toggle them with `/setup release_alerts:false`.
- **Release summaries** are sent `RELEASE_SUMMARY_DELAY_SECONDS` after the event
  (30 seconds by default). The bot scrapes the event page for the actual value,
  compares it with the forecast, and labels the result as better/worse than
  expected. It retries for up to `RELEASE_SUMMARY_MAX_WAIT_SECONDS` (15 minutes)
  if the actual is not published yet, then skips that event. Toggle with
  `/setup release_summary:false`.
- The better/worse direction is a **heuristic**: for indicators such as
  unemployment or jobless claims, a lower reading is treated as better;
  otherwise a higher reading is treated as better. It is a summary, not trading
  advice.
- All alert types use the same currency/impact filter.
- Sent alerts and summaries are recorded in `data/state.json`, so restarting the
  bot will not repeat messages that were already delivered.
- The daily digest is scheduled in the server's configured timezone, but the
  times inside the embed render in each member's own timezone.

## Free social posting (optional)

The bot can also post calendar alerts to **X (Twitter)** and **Bluesky** for
free. X's own API is pay-per-use ($0.015 per text post, $0.20 if it contains a
URL), so X posting goes through **Buffer's free plan** instead. Buffer owns the X
connection and exposes a free GraphQL API. Bluesky is posted to directly through
its free AT Protocol API.

Both targets are optional. If the environment variables are missing, the bot
runs exactly as before and only posts to Discord.

### Setup

1. Create the X account and connect it to Buffer (free plan) at
   <https://buffer.com>. Buffer Free includes 3 channels, 10 queued posts per
   channel, and 1,000 API requests per channel every 30 days.
2. Create an API key at <https://publish.buffer.com/settings/api>.
3. Put the key in `.env` as `BUFFER_API_KEY`, then list your channel IDs:

   ```bash
   .venv/bin/python social.py --channels
   ```

   Copy the X channel's `id` into `BUFFER_CHANNEL_ID`.
4. For Bluesky, create a free account and an **App Password** (Settings → App
   Passwords). Add `BLUESKY_HANDLE` (e.g. `yourname.bsky.social`) and
   `BLUESKY_APP_PASSWORD`. Never use your main Bluesky password.
5. Optional but recommended: create a free Cloudinary account at
   <https://cloudinary.com> and set `CLOUDINARY_CLOUD_NAME`,
   `CLOUDINARY_API_KEY` and `CLOUDINARY_API_SECRET` from its dashboard. Buffer
   has no file-upload API, so X cards are attached as a public Cloudinary URL.
   Bluesky uploads image bytes directly and does not need Cloudinary.
6. Protect the file and restart:

   ```bash
   chmod 600 .env
   sudo systemctl restart forex-bot
   ```

7. Verify with `/test social:true`. This posts a public test message and a
   branded card to each configured target that accepts that message type.

### What gets posted

`X_POST_MODE` and `BLUESKY_POST_MODE` control each target independently:
`off`, `reminder`, `release`, `summary`, or `all`.

The defaults are deliberately simple:

- **X**: `all` — reminder, release, post-release summary, plus daily and weekly
  red-news cards.
- **Bluesky**: `all` — the same set of posts as X.

Posts include the event time, forecast/previous values and hashtags, with a
blank line between every line so nothing feels cramped. Images are attached as
branded cards, so no links are needed: no link-card dependency and no X URL
surcharge.

Once a day the bot posts a **daily red-news card** with every high-impact event
for that day. Once a week it posts a **weekly red-news card** covering the next
seven days. Both use `SOCIAL_TIMEZONE` and can be disabled with `off`.

### Branded image cards

Every social post includes a deterministic **2400×1350** card rendered locally
with Pillow. The card is drawn at 2x supersampling so text and hairlines stay
sharp on high-DPI screens, then saved as a 16:9 PNG. The cards use the Pipwire
palette (light-gray post background, charcoal number badges, teal accents, soft
red for high-impact labels) and the event title, impact badge, currency, time,
and forecast/previous/actual values. The daily and weekly red-news cards use the
same system to lay out every high-impact event in one glance. No AI image
service is involved, so the numbers are exact and each card takes well under a
second to render.

- The bundled font is `assets/fonts/Inter-Variable.ttf` (SIL OFL licence in
  `assets/fonts/OFL.txt`).
- PNG is used by default; the renderer falls back to JPEG quality 90 if a card
  exceeds Bluesky's 1 MB blob limit.
- `SOCIAL_BRAND_NAME` and `SOCIAL_BRAND_HANDLE` set the header and footer text.
- `SOCIAL_IMAGES=false` turns cards off and posts text only.

Image failures never suppress an alert: if rendering fails, Cloudinary is
unavailable, or Bluesky rejects the blob, the text post is still published.
X cards require all three `CLOUDINARY_*` variables; Bluesky cards work without
them.

Social posting is global (one X/Bluesky account for every Discord server) and is
deduplicated in `data/state.json`, so restarts do not repeat posts. Failed posts
are retried at most every two minutes.

> **Do not** use unofficial X libraries, browser automation, or scrapers to avoid
> the API fee. They violate X's terms and can get the account banned.

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
| `DEFAULT_RELEASE_ALERTS` | `true` | Send an alert at the exact release time. |
| `DEFAULT_RELEASE_SUMMARY` | `true` | Send a post-release actual-vs-forecast summary. |
| `RELEASE_SUMMARY_DELAY_SECONDS` | `30` | Wait after release before fetching the actual value. |
| `RELEASE_SUMMARY_MAX_WAIT_SECONDS` | `900` | Keep retrying for the actual value for this long. |
| `FEED_TTL_SECONDS` | `600` | Calendar feed cache duration. |
| `BUFFER_API_KEY` | empty | Buffer API key; enables X posting when set with the channel ID. |
| `BUFFER_CHANNEL_ID` | empty | Buffer channel ID for the X account. |
| `X_POST_MODE` | `all` | X posts: `off`, `reminder`, `release`, `summary`, `all`. |
| `BLUESKY_HANDLE` | empty | Bluesky handle, e.g. `name.bsky.social`. |
| `BLUESKY_APP_PASSWORD` | empty | Bluesky app password (not the main password). |
| `BLUESKY_PDS` | `https://bsky.social` | Bluesky PDS endpoint. |
| `BLUESKY_POST_MODE` | `all` | Bluesky posts: `off`, `reminder`, `release`, `summary`, `all`. |
| `SOCIAL_CURRENCIES` | empty (all) | Currency filter for social posts. |
| `SOCIAL_MIN_IMPACT` | `high` | Minimum impact for social posts. |
| `SOCIAL_REMINDER_MINUTES` | `15` | Reminder offsets for social posts. |
| `SOCIAL_TIMEZONE` | `UTC` | Timezone used in social post times and digest scheduling. |
| `SOCIAL_DAILY_TIME` | `07:00` | Daily red-news card time (`HH:MM`) or `off`. |
| `SOCIAL_WEEKLY_TIME` | `07:00` | Weekly red-news card time (`HH:MM`) or `off`. |
| `SOCIAL_WEEKLY_DAY` | `mon` | Weekly card weekday (`mon`..`sun` or `0`..`6`). |
| `SOCIAL_IMAGES` | `true` | Render branded 2400×1350 cards for social posts. |
| `SOCIAL_BRAND_NAME` | `Pipwire` | Brand text in the card header/footer. |
| `SOCIAL_BRAND_HANDLE` | empty | Optional handle shown next to the brand. |
| `SOCIAL_FONT_PATH` | bundled Inter | Override the card font file. |
| `CLOUDINARY_CLOUD_NAME` | empty | Cloudinary cloud name for X image hosting. |
| `CLOUDINARY_API_KEY` | empty | Cloudinary API key. |
| `CLOUDINARY_API_SECRET` | empty | Cloudinary API secret (never commit or print). |

Per-server settings are stored in `data/settings.json`.

## Project layout

```
forex-discord-bot/
├── bot.py               # Discord commands, alert scheduler, CLI preview
├── calendar_source.py   # ForexFactory feed client, event model, filters
├── html_parser.py       # HTML fallback parser (BeautifulSoup only)
├── config.py            # env defaults + per-guild JSON settings/state
├── formatting.py        # Discord embeds, impact styling, release summaries
├── graphics.py          # branded 2400x1350 social cards (Pillow)
├── social.py            # free X (Buffer) and Bluesky posting + CLI helper
├── assets/fonts/        # bundled Inter variable font + OFL licence
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
- The weekly JSON feed does not expose released **actual** values. Forecaster
  and previous values are shown in reminders and digests; post-release
  summaries fetch the actual value from the HTML calendar.
- If ForexFactory has not published an actual value within
  `RELEASE_SUMMARY_MAX_WAIT_SECONDS`, that summary is skipped rather than sent
  with an empty result.
- The release summary's better/worse label is a rule-of-thumb comparison of
  actual vs forecast, not market analysis or a trading signal.
- Respect ForexFactory's terms of use and keep the refresh interval reasonable.
