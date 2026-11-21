"""One Thing Agent: Telegram is its interface; it wakes itself up on its own schedule.

Events that wake the agent:
  - you send a message
  - its daily heartbeats (morning plan, evening review)
  - follow-ups it scheduled for itself (checked every minute)
"""
import logging
import os
from datetime import datetime, time, timezone

import openai
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import voice
from brain import Brain
from storage import Store

load_dotenv()  # local .env; on Heroku the config vars are already in the environment
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("one_thing_agent")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_CHAT_ID = int(os.environ["OWNER_CHAT_ID"]) if os.environ.get("OWNER_CHAT_ID") else None
TZ = os.environ.get("TIMEZONE", "Asia/Kolkata")
MORNING = os.environ.get("MORNING_TIME", "09:00")
EVENING = os.environ.get("EVENING_TIME", "21:00")

store = Store(os.environ.get("DATABASE_URL"))
brain = Brain(store, TZ)

MORNING_PROMPT = (
    "Morning heartbeat. Review yesterday's entries and your memories. Open the day: ask for today's ONE thing "
    "(suggest one if a goal was carried over or a pattern points to it)."
)
EVENING_PROMPT = (
    "Evening heartbeat. Review today's entries. Close the day: find out whether the goal got done or what was "
    "accomplished, and settle any open goal (completed / carry over / drop). Clear follow-ups that are no longer needed."
)


def is_owner(update: Update) -> bool:
    return OWNER_CHAT_ID is not None and update.effective_chat.id == OWNER_CHAT_ID


async def send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    for i in range(0, len(text), 4000):  # Telegram's limit is 4096 chars
        await context.bot.send_message(chat_id, text[i : i + 4000])


def speakable(text: str) -> str:
    """Drop emoji and symbols so the voice doesn't read them out."""
    return "".join(ch for ch in text if ch.isalnum() or ch.isspace() or ch in ".,!?;:'\"()-/%&+@#₹$").strip()


async def send_voice_reply(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    """Reply as a voice message with the text as caption; fall back to text if speech fails."""
    try:
        audio = await voice.speak(speakable(text))
    except Exception:
        log.exception("text-to-speech failed")
        audio = b""
    if not audio:
        await send(context, chat_id, text)
        return
    short = len(text) <= 1000  # Telegram captions max out at 1024 chars
    await context.bot.send_voice(chat_id, voice=audio, filename="reply.mp3", caption=text if short else None)
    if not short:
        await send(context, chat_id, text)


async def run_agent(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, wakeup: bool = False,
                    voice_reply: bool = False) -> None:
    try:
        reply = await brain.respond(chat_id, text, wakeup=wakeup)
    except openai.AuthenticationError:
        log.exception("OpenRouter auth failed")
        reply = "My OpenRouter API key isn't working. Check OPENROUTER_API_KEY."
    except openai.RateLimitError:
        reply = None if wakeup else "I'm being rate-limited right now. Try again in a minute."
    except openai.APIStatusError as e:
        log.exception("OpenRouter API error %s", e.status_code)
        reply = None if wakeup else "The AI model had a hiccup. Try again in a moment."
    except openai.APIConnectionError:
        log.exception("OpenRouter unreachable")
        reply = None if wakeup else "I couldn't reach OpenRouter (network). Try again shortly."
    if reply and voice_reply:
        await send_voice_reply(context, chat_id, reply)
    elif reply:
        await send(context, chat_id, reply)


# ---- Telegram handlers -----------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if OWNER_CHAT_ID is None:
        await update.message.reply_text(
            f"Hi! Your chat id is {chat_id}.\nSet OWNER_CHAT_ID={chat_id} and restart me to activate the agent."
        )
        return
    if not is_owner(update):
        return
    await update.message.reply_text(
        f"I'm your One Thing Agent. I open your day at {MORNING}, close it at {EVENING} ({TZ}), "
        "and follow up in between when it helps.\n"
        "Just talk to me. Commands: /today  /plan (my scheduled follow-ups)  /memory  /checkin  /reset"
    )


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_owner(update):
        await run_agent(context, update.effective_chat.id, "Show me my goal and accomplishments for today.")


async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_owner(update):
        await run_agent(context, update.effective_chat.id, "What follow-ups do you have scheduled for me, and why?")


async def memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_owner(update):
        facts = store.list_memories(update.effective_chat.id)
        text = "\n".join(f"{m['id']}. {m['fact']}" for m in facts) or "I don't remember anything about you yet."
        await update.message.reply_text(text + "\n\n(Tell me 'forget 3' to remove one.)" if facts else text)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_owner(update):
        brain.reset(update.effective_chat.id)
        await update.message.reply_text("Conversation cleared. Entries, memories and follow-ups are kept.")


async def checkin_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_owner(update):
        prompt = MORNING_PROMPT if brain.now().hour < 15 else EVENING_PROMPT
        await run_agent(context, update.effective_chat.id, prompt, wakeup=True)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return  # ignore strangers; this agent is personal
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    await run_agent(context, update.effective_chat.id, update.message.text)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voice note in -> transcribe -> agent -> voice reply out."""
    if not is_owner(update):
        return
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id, "record_voice")
    media = update.message.voice or update.message.audio
    tg_file = await media.get_file()
    audio = bytes(await tg_file.download_as_bytearray())
    fmt = "ogg" if update.message.voice else (tg_file.file_path or "x.mp3").rsplit(".", 1)[-1].lower()
    try:
        hints = "\n".join(m["fact"] for m in store.list_memories(chat_id))
        text = await voice.transcribe(brain.client, audio, fmt, hints)
    except openai.OpenAIError:
        log.exception("transcription failed")
        await update.message.reply_text("I couldn't make out that voice note. Could you try again or type it?")
        return
    if not text:
        await update.message.reply_text("I couldn't hear anything in that one. Try again?")
        return
    log.info("voice note transcribed: %s", text[:200])
    await run_agent(context, chat_id, f"[Voice note, transcribed] {text}", voice_reply=True)


# ---- autonomous wake-ups ---------------------------------------------------
async def morning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    brain.reset(OWNER_CHAT_ID)  # fresh conversation each day; long-term state is in the DB
    await run_agent(context, OWNER_CHAT_ID, MORNING_PROMPT, wakeup=True)


async def evening_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_agent(context, OWNER_CHAT_ID, EVENING_PROMPT, wakeup=True)


async def followup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fire any follow-ups the agent scheduled for itself."""
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for f in store.claim_due_followups(now_utc):
        log.info("follow-up %s due: %s", f["id"], f["reason"])
        await run_agent(context, f["chat_id"], f"You scheduled this follow-up: {f['reason']}", wakeup=True)


def parse_time(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m), tzinfo=brain.tz)


def main() -> None:
    app = Application.builder().token(TOKEN).build()
    for name, fn in [("start", start), ("today", today), ("plan", plan), ("memory", memory),
                     ("reset", reset), ("checkin", checkin_now)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))

    if OWNER_CHAT_ID is not None:
        app.job_queue.run_daily(morning_job, parse_time(MORNING), name="morning")
        app.job_queue.run_daily(evening_job, parse_time(EVENING), name="evening")
        app.job_queue.run_repeating(followup_job, interval=60, first=10, name="followups")
        log.info("Agent running: heartbeats at %s and %s (%s), follow-ups checked every minute", MORNING, EVENING, TZ)
    else:
        log.warning("OWNER_CHAT_ID not set: send /start in Telegram to get it.")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
