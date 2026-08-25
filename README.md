# memecountbot

A Telegram bot that sits silently in a group chat, decides which messages are
memes, counts them per person per day, and replies to whoever goes over the
limit with a picture reminding them what the analysts think of their
productivity.

Everything else it ignores. No commands required, no reactions, no noise — the
only time it speaks is meme number six.

```
Денис: [мем]  ×5   → silence
Денис: [мем]   #6  → bot replies with the screenshot, and shuts up until tomorrow
```

Classification runs through [OpenRouter](https://openrouter.ai), so any
vision-capable model works and you pay per message, not per seat.

---

## Quick start

```bash
git clone https://github.com/m0peterson/memecountbot.git
cd memecountbot
./setup.sh
```

The script asks for two things — a bot token and an OpenRouter key — writes
`.env`, and starts the bot in Docker. `./setup.sh --local` runs it in a plain
Python venv instead.

Then, in Telegram:

1. **Turn privacy mode off.** In [@BotFather](https://t.me/BotFather):
   `/setprivacy` → pick your bot → **Disable**. Without this Telegram only
   shows the bot commands and replies addressed to it, so it will count nothing.
2. **Add the bot to your group.** If it was already there, remove and re-add it
   so the new privacy setting takes effect.

That's it. It starts counting immediately.

### Getting the two keys

| | |
|---|---|
| Bot token | [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token |
| OpenRouter key | [openrouter.ai/keys](https://openrouter.ai/keys) → create key → top up a few dollars |

### The reply image

Drop the picture you want the bot to reply with at `assets/warning.jpg`
(`.png`, `.webp`, `.gif` and `.mp4` work too — see [assets/README.md](assets/README.md)).
You can also point `WARNING_FILE` at a URL or at a Telegram `file_id`.

If there's no file, the bot falls back to replying with `WARNING_TEXT`, so it
works before you get around to it.

---

## Deploying somewhere permanent

<a href="https://render.com/deploy?repo=https://github.com/m0peterson/memecountbot"><img src="https://render.com/images/deploy-to-render-button.svg" alt="Deploy to Render" height="32"></a>
<a href="https://railway.com/new/template?template=https://github.com/m0peterson/memecountbot"><img src="https://railway.com/button.svg" alt="Deploy on Railway" height="32"></a>

Both read the manifests in this repo (`render.yaml`, `railway.json`) and only
prompt you for `BOT_TOKEN` and `OPENROUTER_API_KEY`.

| Target | How | Notes |
|---|---|---|
| **Any VPS / your laptop** | `./setup.sh` | Simplest. Docker Compose with `restart: unless-stopped`. |
| **Render** | button above | Deploys as a *background worker* with a 1 GB disk. Render has no free worker tier. |
| **Railway** | button above | Uses `railway.json`. Attach a [volume](https://docs.railway.com/volumes) mounted at `/app/data` — Railway containers have no persistent disk by default, so without one the SQLite file and verdict cache are wiped on every redeploy. |
| **Fly.io** | `fly launch --copy-config --no-deploy && fly volumes create memecount_data --size 1 && fly secrets set BOT_TOKEN=… OPENROUTER_API_KEY=… && fly deploy` | See `fly.toml`. Don't enable auto-stop — long polling needs the machine awake. |

The bot uses long polling, so it needs no public URL, no webhook and no open
port. If your platform insists on a listening port, set `PORT` and it will
serve a `/health` endpoint on it.

State lives in a single SQLite file (`DB_PATH`). Losing it costs you today's
counters and the verdict cache, and nothing else — but on platforms whose
containers have no persistent disk (Railway, and Fly without a volume) that
happens on every redeploy, so mount a volume at the directory `DB_PATH` lives in.

---

## Configuration

Every setting is an environment variable; `.env.example` documents them all
inline. The ones worth knowing:

| Variable | Default | What it does |
|---|---|---|
| `BOT_TOKEN` | — | **Required.** From @BotFather. |
| `OPENROUTER_API_KEY` | — | **Required.** From openrouter.ai. |
| `DAILY_LIMIT` | `5` | Memes allowed per person per day. The reply fires on number `DAILY_LIMIT + 1`. |
| `TIMEZONE` | `Europe/Moscow` | When "a day" rolls over. |
| `WARN_ONCE_PER_DAY` | `true` | Warn once, then skip that person's messages until tomorrow (so their counter stops at the limit + 1). `false` replies to every meme past the limit and keeps counting. |
| `OPENROUTER_MODEL` | `google/gemini-2.5-flash` | Any vision-capable model. `google/gemini-2.5-flash-lite` is cheaper, `openai/gpt-4o-mini` also works. |
| `MIN_CONFIDENCE` | `0.6` | How sure the model must be before something counts. Raise it if the bot is trigger-happy. |
| `CHECK_TEXT` | `true` | Also judge plain text messages (jokes, copypasta). Turning this off cuts most of the cost. |
| `MIN_TEXT_LEN` | `12` | Text shorter than this is never sent to the model. |
| `CHECK_VIDEO` | `false` | Judge video thumbnails too. |
| `COUNT_STICKERS_AS_MEMES` | `false` | Count every sticker without asking the model. Cheaper, blunter. |
| `WARNING_FILE` | `assets/warning.jpg` | Local path, URL or Telegram `file_id`. |
| `WARNING_TYPE` | `auto` | Force `photo` / `sticker` / `animation` / `video` / `document` / `text`. |
| `WARNING_TEXT` | the Стахановец quote | Fallback text, and the caption when `SEND_WARNING_CAPTION=true`. |
| `ALLOWED_CHATS` | *(all)* | Comma-separated chat ids the bot may work in. A malformed id is a startup error, not a warning — an unreadable allowlist would otherwise mean "allow everything". |
| `ADMIN_IDS` | *(chat admins)* | Extra users allowed to run `/memereset`. |
| `ENABLE_COMMANDS` | `true` | Set to `false` for a completely silent bot. |
| `DROP_PENDING_UPDATES` | `false` | On restart, discard messages Telegram buffered during the downtime. |
| `DB_PATH` | `data/memecount.db` | SQLite file location. |

### Commands

Optional, and only inside group chats:

- `/memestats` — today's leaderboard
- `/memereset` — clear today's counters (chat admins only). Admin status is checked with
  [`getChatMember`](https://core.telegram.org/bots/api#getchatmember), which the bot can only
  answer reliably for other members once it is an administrator itself. If it is not, list the
  people who should be able to reset in `ADMIN_IDS` instead.
- `/memehelp` — what the bot does and the current limit

---

## How it works

```
message → cheap filters → cache → OpenRouter → counter → reply at limit+1
```

1. **Filters.** Voice notes, round videos, music, polls, bot messages, channel
   posts and anonymous-admin messages are dropped without a thought. So are
   commands and text shorter than `MIN_TEXT_LEN`. Videos need `CHECK_VIDEO`.
2. **What gets looked at.** Photos are downloaded at a mid-size rendition;
   stickers, GIFs and videos are judged by their thumbnail (with the sticker's
   emoji and pack name passed along as context); image files sent as documents
   are handled too; text is judged as text.
3. **Cache.** Verdicts are keyed by Telegram's `file_unique_id`, so the same
   sticker or forwarded image is never classified twice — across restarts, in
   every chat. In a group with a healthy sticker habit this removes most calls.
4. **Classification.** One `chat/completions` call. Images are downscaled to
   768px and re-encoded as JPEG first, which keeps token cost flat regardless of
   what was uploaded. The model must answer in JSON; an answer that can't be
   parsed counts as "not a meme", so a confused model can never spam the chat.
   An *unreachable* API is different — that's "unknown": the message is skipped
   and, unlike a real verdict, the non-answer is never written to the cache.
5. **Counting.** `(chat, user, day)` in SQLite, incremented atomically. The
   "day" is a date string in `TIMEZONE`, so the reset is a new key rather than a
   scheduled job — nothing to miss if the bot was down at midnight.
6. **The reply.** On meme `DAILY_LIMIT + 1` the bot replies to that message with
   the asset. The uploaded `file_id` is cached, so it's an upload once and an
   instant reply after. If the file can't be sent as its configured type, it
   retries as a photo, then as a document, then as text.

### What it costs

One LLM call per candidate message, minus cache hits. With
`google/gemini-2.5-flash` at a downscaled 768px image, a busy 50-person chat
lands in the low single-digit dollars per month. To spend less:

- `CHECK_TEXT=false` — media only, usually a >70% cut
- `COUNT_STICKERS_AS_MEMES=true` — no model call for stickers at all
- `OPENROUTER_MODEL=google/gemini-2.5-flash-lite`
- keep `WARN_ONCE_PER_DAY=true` — once someone is warned, the bot stops
  classifying their messages for the rest of the day

### Privacy

The bot reads group messages, sends the ones it can't rule out to OpenRouter,
and stores nothing but counters: chat id, user id, date, count. No message text,
no images, no usernames are ever written to disk. Verdicts are cached by
Telegram's opaque file id, not by content.

---

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The suite covers message filtering, verdict parsing, the OpenRouter HTTP path
(against a local stub), counter atomicity, day rollover and the full
count-then-warn flow. It never touches the network.

```
bot/
  main.py        handlers, wiring, startup checks
  media.py       message → classifiable candidate
  classifier.py  OpenRouter client, image normalisation, verdict parsing
  storage.py     SQLite counters, verdict cache, key/value
  warning.py     resolving and sending the reply asset
  config.py      environment → Config
```
