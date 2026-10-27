# BB

A Discord bot for the /fgog/ server. When someone posts a Twitter/X, Pixiv, Reddit, TikTok, Bluesky or Instagram link, BB reposts it through a fixer service (FxTwitter, phixiv and friends) so the preview actually works, and hides the broken preview on the original. She does it in character: bratty, smug, Senpai-obsessed. Every line she says lives in `bb/strings.yaml`.

What she does:

- Fixes links automatically, one repost per link, with backup fixers when a preview doesn't show up
- Downloads and uploads the media herself when every fixer fails
- Hides the original's preview (or deletes the original, or leaves it alone; per server)
- Deletes or updates her reposts when the original is deleted or edited
- Reposts show just the preview, a small "Tweet" (or "Pixiv", ...) link to the original, and Find source / Translate buttons
- "Find source" in the right-click Apps menu for uploaded images
- Roasts reposts (links already posted recently) with a jump link to the first post
- Checks every fixer every 10 minutes and tells admins when one breaks or comes back
- Rotating status text with the number of links fixed in the last 24 hours
- A weekly roundup: top poster, top site, and the Buster Brainlet of the week
- FGO JP news from Atlas Academy's data: new events and banners (with their pickups and banner art), and text-only reminders 2 days and 1 day before they end. Ping her with `gre new` in any channel to see what's on right now
- `/settings`, visible only to members with Manage Server

The idea comes from [FixTweetBot](https://github.com/Kyrela/FixTweetBot) (MIT + Commons Clause). BB is a fresh rewrite and shares no code with it.

## 1. Create the bot in the Discord Developer Portal

1. Go to <https://discord.com/developers/applications>, click **New Application**, name it BB.
2. **Bot** tab:
   - **Reset Token** and copy it. It goes in `.env` as `DISCORD_TOKEN` (see below). Never commit it.
   - Under **Privileged Gateway Intents**, turn on **Message Content Intent**. Without it BB sees every message as empty and fixes nothing. Presence and Server Members intents are not needed.
   - Optional: turn off **Public Bot** so only you can add her to servers.
3. **Installation** tab: under Installation Contexts, keep **Guild Install** and untick **User Install**.
4. Optional, **Emojis** tab: upload custom emojis named `bb_smug`, `bb_annoyed` and `bb_wink`, then copy each one's markup (`<:bb_smug:123456789012345678>`) into `.env`. Unset ones are simply left out of her lines. App emojis work in every server without extra permissions.
5. Copy the **Application ID** from **General Information**.

### Invite her

Scopes: `bot` and `applications.commands`. Permissions:

| Permission | Why |
|---|---|
| View Channels | see the channels she works in |
| Send Messages | post reposts, alerts and roundups |
| Send Messages in Threads | the same, inside threads |
| Embed Links | without it Discord won't show previews on her messages, which is the whole point |
| Attach Files | the upload fallback |
| Read Message History | reply to the original message |
| Manage Messages | hide the original's preview, or delete the original |
| Manage Webhooks | "copy the poster" mode (reposts under the poster's name and avatar) |

That set adds up to the permissions integer **275414903808**. Replace `APPLICATION_ID` and open:

```
https://discord.com/oauth2/authorize?client_id=APPLICATION_ID&scope=bot+applications.commands&permissions=275414903808
```

Pick the server and authorize. If a channel overrides permissions (read-only channels, for example), give BB's role the permissions above there too. `/settings` -> Status shows what she's missing in the current channel.

## 2. Configure `.env`

```sh
cp .env.example .env
```

| Variable | Required | Meaning |
|---|---|---|
| `DISCORD_TOKEN` | yes | the bot token from step 1 |
| `DEV_GUILD_ID` | no | your test server's ID: slash commands sync there instantly instead of globally (global sync can take a while) |
| `SAUCENAO_API_KEY` | no | from <https://saucenao.com/user.php?page=search-api>. Without it, Find source only offers search-engine links |
| `DATABASE_PATH` | no | SQLite file, default `data/bb.sqlite`. Leave the default under Docker |
| `LOG_LEVEL` | no | `DEBUG`, `INFO` (default), `WARNING`, `ERROR` |
| `EMOJI_BB_SMUG`, `EMOJI_BB_ANNOYED`, `EMOJI_BB_WINK` | no | custom emoji markup from step 1.4 |

`.env` is git-ignored and never copied into the Docker image.

## 3a. Run with uv

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/), and `ffmpeg` on the PATH for the upload fallback (`brew install ffmpeg`, `apt install ffmpeg`).

```sh
uv sync
uv run python -m bb --check   # load config, database, strings and every cog, then exit; no token needed
uv run python -m bb           # run her
```

`--check` uses an in-memory database and never connects to Discord. It prints the number of string keys, the loaded cogs and the app commands, then `ok`. Run it after every change.

## 3b. Run with Docker Compose

```sh
docker compose up -d --build          # build and start in the background
docker compose logs -f                # follow the logs
docker compose run --rm bb python -m bb --check
docker compose down                   # stop (the data volume is kept)
```

To update: `git pull && docker compose up -d --build`.

The image is `python:3.12-slim` with ffmpeg, dependencies installed by uv from `uv.lock`, running as a non-root user (uid 1000). The database lives in the named volume `bb-data`, mounted at `/app/data`, so it survives rebuilds. To back it up:

```sh
docker compose stop
docker cp "$(docker compose ps -aq bb)":/app/data ./bb-backup
docker compose start
```

If you'd rather use a bind mount (`./data:/app/data`), create the directory first and make it writable by uid 1000: `mkdir -p data && sudo chown 1000:1000 data`.

## 3c. Deploying to mc-server

```bash
scripts/deploy.sh
```

Runs lint and tests, copies the project (including `.env`) to `mc-server:/opt/bb-bot`, rebuilds the
container, and waits until BB is back on Discord. `BB_HOST` / `BB_DEST` change the target.

**Backups:** `/etc/cron.d/bb-bot-backup` runs `scripts/backup.sh` nightly at 04:30 server time. It copies
the database to `/opt/bb-bot-backups/bb-YYYY-MM-DD.sqlite` (kept outside the deploy folder, so a deploy
can't touch it) and keeps the newest 14. Restore by stopping BB and copying a backup over
`/app/data/bb.sqlite` in the `bb-data` volume.

## 4. Set her up in your server

Everything is in `/settings` (Manage Server only). Worth doing right away:

- **Alerts channel**: where she reports fixers going down and coming back. No channel, no alerts.
- **FGO news**: the channel for FGO JP news. It's off until a channel is set. BB checks every 15 minutes and posts only what's new from then on, plus anything that started in the last 2 days. "What's on now" shows you privately what's running.
- **Roundup**: channel, weekday, hour, and whether to award Buster Brainlet. The roundup uses the server's **timezone** setting. It's off until a channel is set, and the first one goes out at the next scheduled slot. If BB is offline at that hour she still posts it if she's back within 12 hours; otherwise that week is skipped.

## Development

```sh
uv run pytest            # unit tests, no network and no Discord
uv run pytest -m live    # also probes every real fixer over the network
uv run ruff check
```

## Manual test checklist

Run through this in a test server (set `DEV_GUILD_ID` so commands show up instantly), with BB and a second, non-admin account.

**Link fixing**
- [ ] One link per site gets a working preview: Twitter/X, Pixiv, Reddit, TikTok, Bluesky, Instagram. The original's preview disappears.
- [ ] A message with several links gets one repost per link.
- [ ] `||spoilered||` links are reposted spoilered.
- [ ] Links in code blocks, inline code and `<angle brackets>` are ignored.
- [ ] A message containing the skip word (`nobb`) is ignored.
- [ ] "Copy the poster" mode: the repost shows the poster's name and avatar, also inside a thread.
- [ ] "Delete original" and "leave original" modes do what they say.

**Sync**
- [ ] Deleting the original deletes the repost.
- [ ] Editing a link out of the original deletes that repost; editing a new link in fixes it.

**Buttons**
- [ ] Delete works for the poster and for a mod; anyone else gets a private refusal. The original's preview comes back.
- [ ] Translate switches a tweet to the translated version and back.
- [ ] Find source appears once an image preview loads, and returns results (or search links).
- [ ] Right-click an uploaded image -> Apps -> Find source.

**Repost roasts**
- [ ] Post a link, then post the same link from the second account (via a different domain, e.g. x.com vs fxtwitter.com): BB roasts it with a jump link to the first post.

**Fixer failure**
- [ ] Post a link to a deleted or private post: BB tries each fixer in turn, then the upload fallback, then edits the repost to the failure line.
- [ ] Health alerts: set an alerts channel, then make one fixer unreachable from the bot's host. Under Docker, add `extra_hosts: ["fxtwitter.com:127.0.0.1"]` to the service and `docker compose up -d`. Within two checks (up to 20 minutes) the alerts channel says it's down and names the backup, and new tweet links use the backup. Remove the line, restart, and within one check she announces it's back.

**Admin and background**
- [ ] `/settings` is invisible to the non-admin account and works for an admin; changes stick after a restart.
- [ ] The status text appears under BB's name and changes every 30 minutes.
- [ ] Roundup: set the roundup channel with the weekday and hour set to the current hour (in the server's timezone). Within 5 minutes the weekly report appears, once; restarting her doesn't post it again.
- [ ] Ping BB with no link: she answers. Ping her with "help": she explains herself.
- [ ] FGO news: set a news channel, then click "What's on now": the current JP events and banners show up privately, with end times in your own timezone.
