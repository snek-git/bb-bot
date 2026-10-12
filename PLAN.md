# BB Bot: build plan

A Discord bot for the /fgog/ server. It reposts broken social media links as fixed ones with proper previews, in BB's voice (bratty, flirty, Senpai-obsessed). The idea comes from FixTweetBot (Kyrela/FixTweetBot, MIT + Commons Clause), but this is a fresh rewrite that shares no code with it.

The voice reference is `bb-voice.md`, draft 2. Every line the bot says lives in `bb/strings.yaml`.

## Scope

### First version
1. **Automatic link fixing** for Twitter/X, Pixiv, Reddit, TikTok, Bluesky and Instagram
2. **Backup fixers**: if a fixer's preview doesn't show up, switch to the next fixer
3. **File-upload fallback**: when every fixer fails, download the media with yt-dlp and upload it
4. **Hiding the original's preview** (or deleting the original, or leaving it; set per server)
5. **Delete and edit sync**: delete the original and the repost goes; edit a link out and its repost goes; edit a link in and it gets fixed
6. **Buttons on reposts**: Find source, Translate (tweets only), Delete (poster and mods only)
7. **"Find source" in the right-click Apps menu** for images people upload directly
8. **Rerun detection**: roasts whoever reposts something already posted, with a jump link to the original
9. **Quips**: 1 in 100 fixes by default (1%); always on repost roasts and failures. When a quip is due, it comes from the channel's themed pool (lewd channels like #2d-cum-zone, chaos channels like #brazil, both picked in `/settings`), otherwise the normal pool
9b. **Meme-domain links** (girlcockx, cunnyx, any domain whose URL looks like a tweet, Bluesky post, TikTok or Reddit post): counted for reruns, and left alone while their preview works. If the original message ends up with no preview at all, BB fixes them like any other link
10. **Fixer health checks** with alerts to an admin channel
11. **Rotating status text**
12. **Weekly roundup**, including Buster Brainlet of the week
13. **`/settings`**, visible only to admins (Manage Server)
14. **Docker setup** and a setup guide

### Later
- Self-hosted, BB-branded FxEmbed (needs a domain and throwaway accounts)
- Repost detection for images, not just links
- Compressing videos with ffmpeg when they're over the upload limit
- `/roll` gacha simulator, servant lookup
- BB art as the bot's own emojis (waiting on images)

### Not doing
- Slash commands for regular members. Members interact through automatic fixes and buttons only.
- Translations into other languages. English only.

## Stack

Python 3.11+, discord.py ≥ 2.4 (for `DynamicItem`, which makes buttons survive restarts), aiosqlite, aiohttp, PyYAML, yt-dlp, ffmpeg. The project is managed with `uv`. Tests use pytest, linting uses ruff. Deployment is a Docker image with a volume for the database.

## Discord setup (you do this in the Developer Portal)

- **Intents:** Guilds, Guild Messages, **Message Content** (a privileged intent you have to switch on; without it she sees empty messages). The Members intent isn't needed.
- **Invite scopes:** `bot` and `applications.commands`.
- **Permissions:** View Channels, Send Messages, Send Messages in Threads, Embed Links, Attach Files, Read Message History, Manage Messages, Manage Webhooks.
- **Secrets** go in a `.env` file that you create; I never handle the values. `DISCORD_TOKEN` is required. `SAUCENAO_API_KEY` is optional; without it, Find source only gives search-engine links. `DEV_GUILD_ID` is optional and makes command changes show up instantly in your test server.

## Layout and who owns which files

```
bb/
  __main__.py        [0]  entrypoint: python -m bb   (python -m bb --check: load everything, don't connect)
  bot.py             [0]  BBBot: DB, HTTP session, settings, down_fixers, load cogs, sync commands
  config.py          [0]  environment variables → Config dataclass
  db.py              [0]  connection, schema, every shared query
  settings_store.py  [0]  per-server settings: defaults + get/update
  strings.py         [0]  loads strings.yaml; line(key, **fmt) picks a random variant
  strings.yaml       [0]  every line BB says
  timeutil.py        [0]  humanized times, timezones
  render.py          [0]  repost text: format_repost(), format_upload()
  links/
    model.py         [0]  dataclasses: Site, Fixer, LinkMatch
    extract.py       [A]  markdown-aware URL extraction        (stub signature from [0])
    sites.py         [A]  site table, matching, fixer chains, rewriting   (stub signatures from [0])
    resolve.py       [A]  short-link expansion                 (stub signature from [0])
  upload.py          [E]  per-site media download for the upload fallback   (stub signature from [0])
  views.py           [C]  make_repost_view(): the buttons B attaches   (stub signature from [0])
  rerun.py           [B]  rerun lookup
  cogs/
    linkfix.py       [B]  on_message flow, sending, embed wait, fixer fallback, original handling
    sync.py          [B]  edit/delete sync
    buttons.py       [C]  DynamicItems: delete, translate, find source
    findsource.py    [C]  SauceNAO + search-engine links + right-click menu
    settings.py      [C]  /settings admin panel
    chat.py          [C]  replies when pinged ("help" or nothing)
    health.py        [D]  fixer probes, health state, alerts; keeps bot.down_fixers current
    status.py        [D]  rotating status text
    roundup.py       [D]  weekly roundup
tests/                    each track adds tests for its own modules
.env.example              [0]
Dockerfile, docker-compose.yml, README.md   [D]
```

`[0]` is the skeleton, written first, with the real interfaces (dataclasses, every shared DB query, every string key, and stub functions with their final signatures). A–E are build tracks that run in parallel against those interfaces. **No track edits files it doesn't own.** If a track needs a schema change, a new shared query or a new string key, it says so in its report instead of editing.

## Behaviour spec

### Link fixing (`linkfix.py`)
1. **Ignore** messages that are:
   - from the bot itself, DMs, or system messages
   - in a disabled channel
   - from webhooks, unless the server setting allows them
   - containing the skip word (`nobb` by default; whole word, case-insensitive)
2. **Extract URLs** with `extract.py`. Skip code blocks, inline code and `<url>` (Discord's "no preview" syntax). Remember which links are inside `||spoilers||`. Handle at most 4 links per message.
3. **Match** each URL to a site (`sites.match(url) -> LinkMatch | None`). Expand short links first.
4. **Record every match in `posts`** right away, before sending anything. A link counts as posted even if its fix later fails, so reruns and the roundup still see it. Run the rerun check before inserting, so a link doesn't match itself.
5. **One repost per link.** This keeps fallback, translate and buttons per-link, with no grouping logic.
6. **Repost text:**
   - Format: `[Label](<original>) • [@author](<profile>) • [FixerName](fixed_url)`. Spoilered links are wrapped in `|| ||`. Drop the `@author` part when the URL doesn't include one.
   - Optionally add one quip line below, as `-# …` subtext. Priority:
     1. the rerun callout
     2. a random quip at the server's rate (from the channel's themed pool if it has one)
   - `allowed_mentions` is always none. BB never pings anyone by accident.
7. **Sending:**
   - **Reply mode** (default): reply to the original with `reference=` and `fail_if_not_exists=False`, so the send still works if the original gets deleted mid-fix. Silent by default.
   - **Copy-the-poster mode:** send through a webhook the bot creates in that channel (reuse it if it exists), using the poster's display name and avatar. For threads, use the parent channel's webhook plus `thread=`. Buttons work because the webhook is owned by the bot.
8. **Buttons when sending** (built by `views.make_repost_view`):
   - Delete: always.
   - Translate: Twitter only.
   - Find source: added afterwards by editing the message, once the preview has appeared with an image (not video-only).
9. **Waiting for the preview:**
   - Wait up to 6 seconds for an embed. Watch `on_raw_message_edit` and read `payload.data.get("embeds")`; don't rely on the cached `message_edit` event, because the message may not be cached when the preview lands. Also check embeds already on the sent message.
   - **No embed:** edit the link to the next fixer from `sites.chain(site_id, bot.down_fixers)` and wait again. **Update `reposts.fixer_id`** every time it switches, because Translate rewrites from the stored fixer.
   - **Chain used up:** run the upload fallback. If that fails too, edit the repost to the failure line.
10. **The original message:** act only after **every** repost for that message is done and none failed outright. Hiding the preview after only some of them succeed would kill a working native preview. Then, per the server setting:
   - Hide its preview (default): `edit(suppress=True)`. Wait 1 second, and if embeds came back, suppress again.
   - Delete it.
   - Leave it.
11. **Record** each repost in `reposts` when it's sent. Update `status` to ok / uploaded / failed as it settles.

### Upload fallback (`upload.py`, track E)
`fetch_media(match, max_bytes, session, workdir) -> list[MediaFile]` returns an empty list on failure. The caller owns the temp directory and edits the repost to: the original link, the attachment(s), and the "BB carried it herself" line. Each site needs its own strategy:
- **Twitter/X:** media URLs from the FxTwitter or vxTwitter JSON API (`api.fxtwitter.com`, `api.vxtwitter.com`), which works for both photos and videos. Use yt-dlp only as a last resort for video.
- **TikTok, Reddit:** yt-dlp.
- **Bluesky:** the public AppView API (`public.api.bsky.app`, no login) for images and video.
- **Pixiv:** image URLs from `www.pixiv.net/ajax/illust/<id>`, then fetch `i.pximg.net` with a `Referer: https://www.pixiv.net/` header, because pximg blocks hotlinking. R-18 posts need a logged-in session, so the fallback skips them for now.
- **Instagram:** yt-dlp. Usually needs cookies, which we don't have yet, so expect it to fail cleanly.

Everything runs in a worker thread or async with a 60-second total timeout. Files over `max_bytes` are skipped, at most 4 files.

### Edit and delete sync (`sync.py`)
- **Original deleted** (`on_raw_message_delete` / `on_raw_bulk_message_delete`): delete all its reposts and their rows.
- **Original edited** (`on_raw_message_edit`): only acts when the content changed (Discord also sends edit events when previews load). Compare the old and new link keys:
  - a link was removed → delete its repost
  - a link was added → fix it
- **Repost deleted by someone else:** drop its row.

### Reruns (`rerun.py`)
- **Key:** each link gets a canonical key such as `twitter:1234`, `pixiv:5678`, `reddit:abc12`, `tiktok:7300…`, `bsky:<handle>/<rkey>`, `instagram:<shortcode>`. The same post via x.com, fxtwitter, vxtwitter and so on gets the same key.
- **Lookup:** find the most recent earlier post with that key in the server. It has to be within the window (30 days by default; setting), in the same channel if the setting says so, and by a different author (unless "roast self-reposts" is on). The original must still exist; if it was deleted, drop that row and check the next older one.
- **Hit:** the rerun line is used, with `{author}` (stored display name, never a ping), `{ago}` and `{jump}`. Log a `rerun_events` row for the roundup.
- **Skip word:** messages containing it are neither fixed nor roasted.

### Buttons (`buttons.py`)
Buttons are stateless `DynamicItem`s: everything they need is encoded in the button ID, so they keep working after restarts.
- **Delete** (`bb:del:<origin_author_id>`):
  - Allowed for the original poster or anyone with Manage Messages. They delete the repost, and if the original's preview was hidden, it's restored.
  - Anyone else gets a private reply with the `delete.denied` line.
- **Translate** (`bb:tr:<0|1>`):
  - Switches the fixer URL to the translated version: FxTwitter takes `/<lang>` on the end of the status URL, using the server's language setting (`en` by default).
  - Adds or removes the translate quip line, and flips the button label between Translate and Original.
  - Webhook reposts are edited through the webhook.
- **Find source** (`bb:src`): runs Find source on the repost's embed image.

### Find source (`findsource.py`)
- **With a SauceNAO key:** search, keep results above about 60% similarity, and reply publicly with a BB line plus up to 3 results (site, similarity, link).
- **Always:** add link buttons to ascii2d, IQDB, Yandex and Google Lens for that image URL.
- **No key or no match:** the "nothing found" line plus those link buttons.
- **Right-click menu** ("Find source" under Apps) on any message with an image attachment or embed image does the same thing. It's the one member-facing app command, and it's easy to remove if you don't want it.

### Health checks (`health.py`)
- Every 10 minutes, probe each fixer with a known-good test post for its site. The request uses Discord's crawler user agent (`Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)`), and a pass means HTTP 200 plus `og:` or `twitter:` preview tags.
- 2 failures in a row mark a fixer down; 1 success marks it up. The state is stored in `fixer_health`.
- `sites.chain(site)` returns healthy fixers first, in configured order.
- Changes trigger an alert in each server's alert channel (if one is set). There's a separate alert when every fixer for a site is down.

### Status text (`status.py`)
Every 30 minutes, pick a random `status` line, where `{n}` is the number of successful fixes in the last 24 hours.

### Weekly roundup (`roundup.py`)
- **When and where:** set per server (channel, weekday, hour, timezone). Off until a channel is set.
- **Contents:**
  - most links posted (author, count)
  - top site
  - Buster Brainlet of the week: whoever got roasted for the most reruns. Leave this line out if nobody was.
- **Format:** an embed in BB purple.

### `/settings` (`settings.py`)
- `default_permissions(manage_guild=True)`, server-only, so members never see it. The panel is private (ephemeral) and opens with BB's title line.
- **Sections:**
  - Sites: on or off for each
  - Channels: deny list, or allow-list mode
  - Repost style: reply vs. copy-the-poster, silent vs. ping
  - Original message: hide preview / delete / leave
  - Quips: off / 1 in 10 / 1 in 5 / 1 in 3 / always, plus lewd and chaos channels
  - Reruns: on or off, window in days, same channel or whole server, roast self-reposts
  - Webhooks: whether BB fixes links posted by webhooks
  - Roundup: channel, day, hour, Brainlet award
  - Alerts channel
  - Skip word
  - Translation language
  - Timezone
  - Status: BB's permissions in this channel plus fixer health
- Changes save immediately.

## Data model (SQLite, WAL mode)

```
guild_settings(guild_id PK, data JSON)                  -- merged over code defaults
reposts(repost_id PK, guild_id, channel_id, origin_message_id, origin_author_id,
        site, canonical_key, fixer_id, via_webhook, webhook_id, translated,
        status,  -- ok | uploaded | failed
        created_at)
posts(id PK, guild_id, channel_id, message_id, author_id, author_name,
      site, canonical_key, created_at)                  -- every detected link
rerun_events(id PK, guild_id, offender_id, offender_name, canonical_key, created_at)
fixer_health(fixer_id PK, healthy, consecutive_failures, last_checked, last_change)
```
Indexes: `posts(guild_id, canonical_key, created_at)`, `reposts(origin_message_id)`, `reposts(created_at)`.

## Testing
- **Unit tests (pytest), no Discord needed:**
  - URL extraction: code blocks, `<url>`, spoilers, punctuation at the end of links
  - Every site's URL variants → the correct canonical key and fixer URL, including translation
  - Every line in `strings.yaml` formats with its declared placeholders
  - Rerun lookup on an in-memory database
  - Humanized times
- **Live fixer check:** a test (`pytest -m live`, skipped by default) that probes every fixer.
- **Manual checklist in a test server** (in README): each site, spoilers, skip word, delete/edit sync, each button, copy-the-poster mode, a forced fixer failure, a rerun, `/settings` hidden from non-admins.

### Pings (`chat.py`, track C)
When someone pings BB with no link, she replies with a `ping` line, or the `help` line if the message contains "help". Each person gets one reply per 30 seconds at most.

## Build order
1. **Skeleton [0]:** shared interfaces, schema, every query, every string key, stub signatures. Done by the lead.
2. **Tracks A–E in parallel** as subagents, each held to its file ownership above.
3. **Integration:** lead fixes the seams, then runs ruff + pytest + `python -m bb --check`, which loads every cog without connecting so the wiring can be tested without your token. Then a real start with your token from `.env`.
4. **You test** in a test server with the manual checklist.
