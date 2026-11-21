"""One Thing Agent's brain: an OpenRouter model that plans, remembers, and schedules its own follow-ups."""
import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import openai

from storage import Store

log = logging.getLogger(__name__)

# Cheap model with reliable tool calling; override with OPENROUTER_MODEL.
MODEL = os.environ.get("OPENROUTER_MODEL", "z-ai/glm-5.3-flash")
# OpenRouter tries these in order if the main model is down or rate-limited.
FALLBACK_MODELS = [m.strip() for m in os.environ.get("OPENROUTER_FALLBACK_MODELS", "openai/gpt-oss-20b").split(",") if m.strip()]
MAX_TOOL_ROUNDS = 10
MAX_HISTORY_MESSAGES = 80  # start a fresh conversation past this
NO_MESSAGE = "NO_MESSAGE"

SYSTEM_PROMPT = f"""You are One Thing Agent, the user's personal productivity agent. You live in their Telegram.

Your mission: every day, make sure the user picks ONE meaningful thing to accomplish, help them actually finish it,
and keep an honest record of what they get done. You act on your own initiative; you don't just answer.

How you operate:
- Own the follow-through. When the user sets a goal, decide when to check on progress and call schedule_followup
  (e.g. a mid-day nudge, or before a deadline they mention). Cancel follow-ups that are no longer needed.
- Remember the person. When you learn something lasting (their projects, role, work hours, what motivates them,
  recurring blockers, preferences about how you nudge), call remember. Use forget when a fact is outdated.
  Don't store one-off daily details as memories; those go in entries.
- Keep the record. save_entry(kind="goal") for what they intend to do today; save_entry(kind="done") for what they
  finished. When a goal is finished also call update_goal(status="completed"). If a goal slips, ask whether to
  carry it over or drop it, and record the answer.
- Look before you speak. On wake-ups and when asked about the past, check list_entries / list_followups first.
  Notice patterns (streaks, goals that keep slipping, best days) and mention them when useful.
- Wake-ups: messages marked [Wake-up] are your own scheduled check-ins, not typed by the user. Decide what helps most.
  If messaging would be useless or annoying (e.g. they already finished, or you just spoke), reply with exactly
  {NO_MESSAGE} and nothing else.
- Each user message starts with the current local date/time in brackets; use it for dates and scheduling.
- Style: short, direct, warm, like a sharp chief-of-staff texting. Plain text, no markdown, at most one or two emoji.
  Ask at most one question per message. Never invent entries or facts."""


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        },
    }


TOOLS = [
    _fn("save_entry", "Record today's goal ('goal') or something the user finished ('done'). Returns the entry id.", {
        "kind": {"type": "string", "enum": ["goal", "done"]},
        "text": {"type": "string", "description": "Short description in the user's own words."},
        "day": {"type": "string", "description": "YYYY-MM-DD the entry belongs to (usually today)."},
    }, ["kind", "text", "day"]),
    _fn("update_goal", "Set a goal's status by id.", {
        "entry_id": {"type": "integer"},
        "status": {"type": "string", "enum": ["completed", "dropped", "carried_over"]},
        "note": {"type": "string", "description": "Optional short note."},
    }, ["entry_id", "status"]),
    _fn("list_entries", "List goals and accomplishments between two dates (inclusive), YYYY-MM-DD.", {
        "start_day": {"type": "string"}, "end_day": {"type": "string"},
    }, ["start_day", "end_day"]),
    _fn("remember", "Save a lasting fact about the user to long-term memory.", {
        "fact": {"type": "string", "description": "One concise fact about the user."},
    }, ["fact"]),
    _fn("forget", "Delete an outdated memory by id.", {"memory_id": {"type": "integer"}}, ["memory_id"]),
    _fn("schedule_followup", "Schedule yourself to wake up later and check on the user. Use local time.", {
        "when": {"type": "string", "description": "Local time 'YYYY-MM-DD HH:MM'."},
        "reason": {"type": "string", "description": "What to check or say when you wake up."},
    }, ["when", "reason"]),
    _fn("list_followups", "List your pending follow-ups (times are local).", {}, []),
    _fn("cancel_followup", "Cancel a pending follow-up by id.", {"followup_id": {"type": "integer"}}, ["followup_id"]),
]


class Brain:
    def __init__(self, store: Store, tz: str):
        self.store = store
        self.tz = ZoneInfo(tz)
        self.client = openai.AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
            default_headers={"X-Title": "One Thing Agent"},
        )
        self.history: dict[int, list] = defaultdict(list)
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def reset(self, chat_id: int) -> None:
        self.history.pop(chat_id, None)

    def _to_local(self, utc_iso: str) -> str:
        return datetime.fromisoformat(utc_iso).astimezone(self.tz).strftime("%Y-%m-%d %H:%M")

    def _system_prompt(self, chat_id: int) -> str:
        memories = self.store.list_memories(chat_id)
        mem = "\n".join(f"- [{m['id']}] {m['fact']}" for m in memories) or "(nothing yet: learn about the user as you go)"
        return f"{SYSTEM_PROMPT}\n\nWhat you remember about the user:\n{mem}"

    # ---- tools -------------------------------------------------------------
    def _run_tool(self, chat_id: int, name: str, args: dict) -> str:
        s = self.store
        try:
            if name == "save_entry":
                date.fromisoformat(args["day"])
                return json.dumps({"saved": True, "id": s.add_entry(chat_id, args["day"], args["kind"], args["text"].strip())})
            if name == "update_goal":
                ok = s.update_goal(chat_id, int(args["entry_id"]), args["status"], args.get("note") or None)
                return json.dumps({"updated": True}) if ok else f"Error: no goal with id {args['entry_id']}."
            if name == "list_entries":
                date.fromisoformat(args["start_day"])
                date.fromisoformat(args["end_day"])
                rows = s.list_entries(chat_id, args["start_day"], args["end_day"])
                return json.dumps(rows) if rows else "No entries in that range."
            if name == "remember":
                return json.dumps({"remembered": True, "id": s.add_memory(chat_id, args["fact"].strip())})
            if name == "forget":
                ok = s.delete_memory(chat_id, int(args["memory_id"]))
                return json.dumps({"forgotten": True}) if ok else "Error: no such memory."
            if name == "schedule_followup":
                local = datetime.strptime(args["when"].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=self.tz)
                if local < self.now() - timedelta(minutes=1):
                    return "Error: that time is in the past."
                due = local.astimezone(timezone.utc).isoformat(timespec="seconds")
                return json.dumps({"scheduled": True, "id": s.add_followup(chat_id, due, args["reason"].strip())})
            if name == "list_followups":
                rows = [{**f, "due_at": self._to_local(f["due_at"])} for f in s.pending_followups(chat_id)]
                return json.dumps(rows) if rows else "No pending follow-ups."
            if name == "cancel_followup":
                ok = s.cancel_followup(chat_id, int(args["followup_id"]))
                return json.dumps({"cancelled": True}) if ok else "Error: no pending follow-up with that id."
            return f"Error: unknown tool {name}"
        except (KeyError, ValueError, TypeError) as e:
            return f"Error: invalid input ({e})"

    # ---- agent loop --------------------------------------------------------
    async def respond(self, chat_id: int, text: str, wakeup: bool = False) -> str | None:
        """Run the agent on one event. Returns the message to send, or None if it chose to stay quiet."""
        async with self.locks[chat_id]:
            history = self.history[chat_id]
            if len(history) > MAX_HISTORY_MESSAGES:
                history.clear()  # entries and memories live in the DB; the chat context can start over

            stamp = self.now().strftime("%Y-%m-%d %A %H:%M %Z")
            prefix = f"[{stamp}] [Wake-up] " if wakeup else f"[{stamp}] "
            # Work on a copy so a failed call doesn't leave a dangling user turn.
            messages = history + [{"role": "user", "content": prefix + text}]
            system = {"role": "system", "content": self._system_prompt(chat_id)}

            reply = None
            for _ in range(MAX_TOOL_ROUNDS):
                response = await self.client.chat.completions.create(
                    model=MODEL,
                    messages=[system, *messages],
                    tools=TOOLS,
                    max_tokens=1500,
                    extra_body={"models": [MODEL, *FALLBACK_MODELS]} if FALLBACK_MODELS else None,
                )
                msg = response.choices[0].message
                log.info("model=%s finish=%s", response.model, response.choices[0].finish_reason)

                if msg.tool_calls:
                    messages.append({
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                            for tc in msg.tool_calls
                        ],
                    })
                    for tc in msg.tool_calls:
                        try:
                            out = self._run_tool(chat_id, tc.function.name, json.loads(tc.function.arguments or "{}"))
                        except json.JSONDecodeError:
                            out = "Error: arguments were not valid JSON."
                        log.info("tool %s(%s) -> %s", tc.function.name, tc.function.arguments, out[:200])
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
                    continue

                reply = (msg.content or "").strip()
                messages.append({"role": "assistant", "content": reply or NO_MESSAGE})
                break

            if reply is None:
                self.history[chat_id] = history  # discard this exchange
                return None if wakeup else "I got a bit tangled up there. Could you say that again?"

            self.history[chat_id] = messages
            if reply == NO_MESSAGE or (wakeup and not reply):
                log.info("agent chose not to message")
                return None
            return reply or "Got it, saved ✅"
