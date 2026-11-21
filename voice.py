"""Voice in and out.

- Hearing: Telegram voice notes (OGG/Opus) are transcribed by an audio-capable OpenRouter model.
- Speaking: replies are turned into speech with edge-tts (free Microsoft neural voices), as MP3,
  which Telegram accepts for voice messages.
"""
import base64
import io
import logging
import os

import edge_tts
import openai

log = logging.getLogger(__name__)

TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "google/gemini-2.5-flash-lite")
VOICE_NAME = os.environ.get("VOICE_NAME", "en-IN-NeerjaNeural")  # also en-IN-PrabhatNeural (male), ta-IN-PallaviNeural / ta-IN-ValluvarNeural (Tamil)

TRANSCRIBE_PROMPT = (
    "Transcribe this voice note exactly as spoken, in the language it was spoken. "
    "Output only the transcript, with no labels, quotes or commentary. "
    "If there is no intelligible speech, output exactly: [inaudible]"
)


async def transcribe(client: openai.AsyncOpenAI, audio: bytes, fmt: str = "ogg", hints: str = "") -> str:
    """Turn a voice note into text. `hints` = background on the speaker, used to spell names and jargon right.
    Returns '' if nothing intelligible was said."""
    prompt = TRANSCRIBE_PROMPT
    if hints:
        prompt += f"\n\nBackground on the speaker, to help spell names and technical terms correctly:\n{hints}"
    response = await client.chat.completions.create(
        model=TRANSCRIBE_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "input_audio", "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}},
            ],
        }],
        max_tokens=2000,
    )
    text = (response.choices[0].message.content or "").strip()
    return "" if text == "[inaudible]" else text


async def speak(text: str) -> bytes:
    """Turn a reply into MP3 speech."""
    buf = io.BytesIO()
    async for chunk in edge_tts.Communicate(text, VOICE_NAME).stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    return buf.getvalue()
