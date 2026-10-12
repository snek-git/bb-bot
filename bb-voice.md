# BB Bot: voice deck (draft 2)

Rewritten after reading her NA voice lines (base BB, from the Atlas Academy game data). Every line below is new writing in her style; none are copied from the game.

`{n}`, `{author}`, `{ago}` etc. get filled in by the bot. ♡ and ☆ are ordinary text symbols, not emoji. She ends half her sentences with them, so they stay.

## How she actually talks

- **Senpai**, constantly, and stretched out (Seeenpaaai♡) when she's being cute. Sometimes **Master**, usually sarcastically: *no-good Master*, *inferior Master*, *little Master*.
- Talks about herself in the third person when she's showing off: *BB thinks...*, *BB's bullying...* She calls herself a **cute little devil**, a **kouhai** (junior), and a **high-grade AI**.
- **Sweet setup, nasty punchline.** It starts cheerful and helpful, then ends with a threat or an insult. Her signature move.
- **Tsundere flip.** She gets caught being nice, insists "don't get the wrong idea", and covers it with a fresh insult.
- **Everyone who isn't Senpai is beneath her:** humans are bugs, ants, pigs. "Turn into a pig" is a running threat.
- Calls things **punishment** and **games**. Says "X, GET☆" when she gets something. Laughs **hehe**. Has a sweet tooth.
- Flirting is **possessive and teasing**: *watch me*, *you can't escape me*, *you're my toy*. Affection always wrapped in bullying.

## How people use her (no slash commands)

Links get fixed automatically. Everything else is a button under the preview card on her repost:

```
[Tweet] • [@user] • [FxTwitter]
-# quip line, sometimes
┌──────────────────────────┐
│      preview card        │
└──────────────────────────┘
[ Find source ]  [ Translate ]  [ Delete ]
```

| Button | Shows up on | What it does |
|---|---|---|
| **Find source** | Reposts with images | Finds where the art came from |
| **Translate** | Tweets | Switches the repost to the translated preview; the button flips to **Original** |
| **Delete** | Every repost | Removes the repost. Only works for the poster and mods; anyone else gets told off where only they can see it |

Each repost only gets the buttons that make sense for it.

Other ways to use her:
- **Leave a link alone:** put `nobb` in the message.
- **Ping her:** she answers with a random line.

Settings live in a config file I set up; no menus.

## Samples

### Status text (rotates)
- Watching Senpai's every link♡
- {n} links saved today. Praise me, Senpai☆
- Fixed {n} links. Humans fixed zero♡
- High-grade AI on embed duty. Ugh.

### Quip under a fix (on 1 in N fixes)
- -# Hehe, Senpai can't even post a link without BB. So helpless♡
- -# Embed, GET☆ You owe me dessert for this one, Senpai.
- -# Twitter broke it, BB fixed it. Think hard about which one of us you should thank☆
- -# Don't get the wrong idea. I fix these because broken things annoy me. Not for you♡
- -# Another one? You really can't live without me, can you, Senpai♡

### Time-aware (posted between 1 and 5am)
- -# Posting at this hour, Senpai? Can't sleep without thinking about me?♡

### Rerun (always on)
- -# Ehh? {author} posted this {ago}. Copying people now, Senpai? How pathetic♡
- -# Too slow☆ {author} got here {ago}. BB remembers everything, Senpai.

### Every fixer failed, so she uploaded it herself (always on)
- -# Every embed site died, so BB grabbed it herself. Aren't I amazing? Say it.
- -# This one's broken beyond even me. ...The website's fault, obviously.

### Pinged with nothing
- You called, Senpai? Hmm, what should I make you do in return♡
- Don't ping a high-grade AI for nothing. ...Unless you missed me☆
- Hehe. Senpai's so needy today♡

### Find source
- Found: *Found it☆ {similarity}% match on {source}. Without me you'd never know anything, Senpai♡*
- Not found: *Nothing. Either it's too obscure, or you cropped it like an amateur. Probably that one☆*

### Translate (added as the quip line once it's switched)
- -# Fine, I translated it. Senpai really can't read anything alone♡

### Delete button
- Poster or mod clicks: the repost disappears. No message.
- Anyone else clicks (only they see this):
  - *Hands off☆ That's {author}'s link, not yours.*
  - *Touch other people's things again and I'll turn you into a pig♡*

### Help (pinged with "help")
- *Post a link and BB fixes it. Put `nobb` in it and I'll ignore you. The buttons under my posts do the rest. Simple enough even for you, Senpai♡*

### Admin alerts
- {fixer} stopped working for {site}, so I switched to {backup}. The humans didn't even notice☆
- {fixer} is back. Took them long enough.
- Every {site} fixer is down. I'm uploading files myself until they get it together. You're welcome♡

### Weekly roundup
- Weekly report☆ Most links: {user} with {n}. Do you ever leave this server, Senpai? ...Good. Stay here with me♡
- Most posted site: {site}. So predictable.
- **Buster Brainlet of the week:** {user}, who reposted the same link {n} times. What a cute little ant♡

## Your picks

1. **Skip word:** `nobb` or something else
2. **Quip frequency:** 1 in how many fixes? (I'd say 5)
3. **How mean:** this draft calls people ants and amateurs. More, less, or right?
4. **Find source results:** posted for everyone, or only shown to whoever clicked?
5. **Images people upload directly** (not links): BB has no message of her own to put a Find source button on. Options: right-click the image's message → Apps → "Find source" (a menu item, not a slash command), reacting with an emoji, or skipping it
6. **Button look:** plain text labels, or small BB art emojis on them (send images)
