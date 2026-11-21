# One Thing Agent

A personal agent that lives in your Telegram. Each day it makes sure you pick ONE meaningful
thing to do, follows up on its own until it's done, and keeps a record of what you actually finished.

It is an agent, not a command bot. It decides for itself:
- **When to follow up.** It schedules its own wake-ups ("you said the demo is at 4, I'll check at 2").
- **What to remember.** It keeps long-term facts about you (projects, work hours, recurring blockers) and uses them.
- **Whether to speak.** On each wake-up it checks your progress first and stays quiet if a message wouldn't help.

## How it works

```
 events                          agent loop (brain.py)                   state (storage.py)
 ------                          ---------------------                   ------------------
 your Telegram message  ─┐       model reads memories + history,        entries   (goals / done)
 morning / evening      ─┼─────▶ calls tools until it has decided  ───▶ memories  (facts about you)
 its own follow-ups     ─┘       what to say (or to stay quiet)          followups (its own wake-ups)
```

- `one_thing_agent.py`: Telegram interface, daily heartbeats, and the every-minute follow-up check
- `brain.py`: the agent loop on OpenRouter (`z-ai/glm-5.3-flash`, falling back to `openai/gpt-oss-20b`).
  Tools: `save_entry`, `update_goal`, `list_entries`, `remember`, `forget`, `schedule_followup`,
  `list_followups`, `cancel_followup`
- `storage.py`: SQLite locally, Postgres on Heroku

Telegram commands: `/today`, `/plan` (its scheduled follow-ups), `/memory`, `/checkin`, `/reset`.
Mostly you just talk to it.

## 1. Keys

1. Telegram: message **@BotFather** and send `/newbot` to get a token. Telegram calls every API account a
   "bot"; that's only the account type.
2. OpenRouter: create a key at https://openrouter.ai/keys and add a few dollars of credit.

## 2. Run locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # fill in TELEGRAM_BOT_TOKEN and OPENROUTER_API_KEY
python one_thing_agent.py
```

Send `/start` in Telegram. It replies with your chat id. Put it in `.env` as `OWNER_CHAT_ID` and restart.
From then on it only talks to you.

## 3. Deploy to Heroku

Heroku has no free tier. Expect about $5/month for a dyno plus about $5/month for Postgres. Postgres is required:
the dyno disk is wiped on every restart, so memories and follow-ups must live in the database.

```powershell
heroku login
git init
git add .
git commit -m "One Thing Agent"
heroku create one-thing-agent
heroku addons:create heroku-postgresql:essential-0
heroku config:set TELEGRAM_BOT_TOKEN=... OPENROUTER_API_KEY=... OWNER_CHAT_ID=... TIMEZONE=Asia/Kolkata
git push heroku main
heroku ps:scale worker=1
heroku logs --tail
```

Stop the local copy before starting the Heroku one: two copies polling the same token conflict.

## Settings (env vars)

| Var | Default | Meaning |
|---|---|---|
| `TIMEZONE` | `Asia/Kolkata` | Time zone for heartbeats and follow-ups |
| `MORNING_TIME` / `EVENING_TIME` | `09:00` / `21:00` | Daily heartbeats (24h) |
| `OPENROUTER_MODEL` | `z-ai/glm-5.3-flash` | Main model |
| `OPENROUTER_FALLBACK_MODELS` | `openai/gpt-oss-20b` | Comma-separated backups |
| `DATABASE_URL` | unset (SQLite) | Set automatically by Heroku Postgres |
