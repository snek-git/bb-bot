# Ideas for later

Nothing here is built or scheduled. Decisions are marked as decided; everything else is a suggestion still open for discussion.

## April Fools: member gacha

An FGO-style gacha where every card is themed after the server. Build it in March 2027, run it once around April 1.

### Decided

- April Fools only: a one-off event, not a permanent feature.
- Cards are about server members and in-jokes. Each member can have several versions: low-star ones and a 5★ (for some members at least).
- No avatars. The content is written by hand, by me and the other admins, mined from the server's in-jokes.
- CEs are a big part of it, and they have to be creative. A CE is not just a description of something that happened.

### Event shape (suggestions)

- Starts as a surprise, with no announcement.
- Runs about 36–48 hours, so April 1 is covered in every time zone.
- Everyone starts with a fixed pile of Saint Quartz (e.g. 20 ten-rolls), with no refills. The scarcity is where the salt comes from.
- Real FGO rates:
  - Servants: 5★ 1%, 4★ 3%, 3★ 40%.
  - CEs: 5★ 4%, 4★ 12%, 3★ 40%.
  - A 10-roll gives 11 cards, with at least one 4★-or-better card and at least one servant.
- BB stays silent on normal rolls. She only speaks on a 5★, or when someone pulls BB herself (the only Moon Cancer, about 0.1%).
- It never pings anyone, and it runs in one channel only.
- A "My collection" button shows your cards privately, only to you.
- At the end, BB posts the results:
  - most 5★s
  - unluckiest roller
  - most-pulled member
  - who pulled BB

  Collections stay viewable afterwards as a souvenir.
- A "force on" switch that only works in the dev server, for testing without spoiling the surprise.

### Writing cards (suggestion)

- A private, admin-only workshop channel. Each message is one card: an image attachment plus the card text (an audio file is optional).
- BB reacts to each card message to confirm she understood it and adds a Preview button.
- Editing the message edits the card; deleting it removes the card.
- A pinned summary counts the pool by type and rarity, so gaps are visible (e.g. "only two 5★ servants").
- Every card records who wrote it. Anyone can delete a card about themselves, servant or CE.
- Example format:

```
CE 5 | The Incident
BB fixed every link from 2021 at once. Nobody asked.
```

```
Servant 4 Berserker | Some Guy (Summer)
NP: posts in #brazil at 4am
```

- An optional one-time script to help mine in-jokes:
  - It finds pinned messages, the most-reacted messages, repeated phrases and the most-used custom emoji.
  - It only reads. It has no code that sends anything and doesn't connect the way BB does.
  - The report stays on my machine and out of git.

### CE templates (suggestions)

- **Parodies of famous CEs.** Kaleidoscope (start with 80% NP gauge) or The Black Grail (+60% NP damage, lose 500 HP every turn). Same art layout, same shape of name, rewritten effect.
- **Bond CEs.** The object that defines a member, with a backstory. It only drops after pulling that member 5 times (NP5), so duplicates count for something.
- **Valentine letters.** A note from a member, ideally written by that member, to whoever pulls it.
- **Alternate-universe scenes.** Members in situations that never happened: school AU, swimsuit, crossover, rivals stuck in one picture.
- **Over-dramatic lore about something stupid,** in FGO's melancholic flavor-text style.
- **Split sets.** Four CEs that form one picture; owning all four reveals it. A server-wide hunt.
- **Effects that do something when pulled.** Picked from a fixed menu:
  - Cursed: BB steals some Saint Quartz.
  - Kaleidoscope parody: the next roll starts 80% of the way to a guaranteed 5★.
  - Black Grail parody: double rates for the next 3 rolls, but each costs extra.
  - Generous: a free roll for a random member, or Saint Quartz for everyone in the channel.
  - Face down: your next roll stays hidden until someone else clicks Reveal.

### CE candidate: the voice-message meltdown

One of the most famous and funniest incidents in the server: someone had a meltdown in a voice message over being called gay.

- **Voice line:** the voice message is the CE's voice line. Servants have voice lines and CEs never do, so the format break is part of the joke.
- **Effect, Overcharge** (pushing an NP past 100%, which is what a meltdown is): the next roll's 5★ rate goes way up, but BB takes extra Saint Quartz.
- **Effect, Taunt** (every enemy attacks you): his cards get a rate-up for everyone for an hour.
- **Flavor text:** a solemn monologue, like a dying Lostbelt king's last words, built from his own lines in the clip.
- **Rule:** the joke is the meltdown, not the word. "Lost it over a joke" lands; "got called gay" reads as if being gay is the insult.
- **Rule:** replaying his real voice for a day goes past a screenshot. Check that he laughs about it now. The "delete a card about me" button covers this CE too.

### Servant suggestions (undecided, the admins may have better ideas)

- Classes by role:
  - Ruler: admins
  - Pretender: most-roasted reposter
  - Rider: top link poster
  - Berserker: #brazil regulars
  - Assassin: lurkers
  - Moon Cancer: BB herself, and only her
- Version types:
  - Alter: someone roasted for a repost
  - 0★: the weekly Buster Brainlet
  - Seasonal or summer versions with a class change

### Open questions

- Real FGO keeps 1★ and 2★ servants out of the Saint Quartz gacha; they only come from Friend Point summons. Do the low-star versions go in the main pool, or in a separate free "Friend Point" banner?
- Is there pity (a guaranteed 5★ after enough rolls) for a one-day event?
- How much Saint Quartz does everyone start with?
- Exact start and end time.
- Which channel.
- Do CEs have real effects, or only joke effect text?

## Other ideas from the same discussion (undecided)

### Using Jev (TypeSafe's decision model) or Laya (an open model that answers the same kinds of question)

- **Jev basics:** a fast classifier, not a chatbot. It answers yes/no, choice, scale and locate-in-image questions with probabilities.
  - Cost: $0.042 per million input tokens.
  - Speed: about 0.2s per text answer.
  - Available through OpenRouter.
- **Understanding @BB mentions.** Jev decides what a ping wants (find source on the reply, translate it, flirt, insult) and the code picks the hand-written line or action. Best fit.
- **Find source only on art** (drawings vs photos, memes, screenshots). Needs an image check, so it adds time before the repost, and the policy below blocks it in lewd channels.
- **Translate tiebreaker.** Ask "would an English reader need this translated?" instead of "what language is this?", only for short Latin-script text that Twitter labels non-English.
- **Alerts for hacked-account scam links.** Alerts only, never deletes.
- **News repost roasts.** Catch the same FGO announcement posted by different accounts.
- **Memory search** ("@BB that Okita art from last week"). SQLite full-text search narrows it to about 20 posts, then Jev picks the best one.
- **TypeSafe's usage policy:** adult content between adults is allowed. Sexual content involving minors is banned, and the wording doesn't exempt drawings. So no images from lewd channels, ever.
- **Laya,** by ConvAI Innovations:
  - Open weights (Apache 2.0), 421M parameters, runs on CPU in about 33 ms.
  - Text only, as far as I can tell.
  - Overconfident until tuned for each task.
  - It would need its own container on mc-server.
- **If built:** about a 1 second timeout, then fall back to today's behaviour. A Jev answer never deletes anything. Tune the cut-offs on the dry run first.

### Without any AI model

- **Favourites channel.** Posts that get N of a chosen reaction are copied to a hall-of-fame channel. A skull-reaction "hall of shame" version for #brazil.
- **Most-posted servant of the week.** Look up each art link on Danbooru by its source URL to get character tags (tested and working). This also powers "@BB show me Okita".
- **Guess the servant.** Cropped art or a voice line from Atlas Academy; first correct answer in chat scores. Needs a nickname table.
- **Real-banner gacha simulator** (the "roll" idea postponed early on).
- **"Last day of the banner" posts** in a news channel, from Atlas event data.
- **Keeping copies of art** from fixed links, re-posted when the tweet is deleted. Images only.
- **One-time import of old messages,** so roasts and stats have history from day one. It must be a separate script that only reads Discord and writes the database, and can never send.
- **Weekly report extras:** most-used custom emoji, and the most-reacted post of the week.

### Checked and dropped

- **Atlas DB link previews:** they already show a proper preview.
- **Reposting the official FGO Twitter accounts:** the X API is paid and scrapers break.
- **Reminders, polls, role menus:** Discord or other bots already do them.
- **A local language detector:** Lingua was tested and fails on the same short texts as Twitter's.
