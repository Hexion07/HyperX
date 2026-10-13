import os
from google import genai
from google.genai import types

_client = None


def _get_client():
    """Lazily creates the SDK client on first real use, so a missing key
    doesn't crash the bot at import time — only when the AI is actually
    invoked."""
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Get a free key from Google AI Studio "
                "(https://aistudio.google.com/apikey) and add it to your .env file."
            )
        _client = genai.Client(api_key=api_key)
    return _client


# NOTE: gemini-2.5-flash is slated to shut down on the Gemini Developer API on
# Oct 16, 2026. Check https://ai.google.dev/gemini-api/docs/models before
# then and update GEMINI_MODEL in .env if needed — no code change required.
_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def _history_to_contents(history):
    return [
        types.Content(role=turn["role"], parts=[types.Part(text=text) for text in turn["parts"]])
        for turn in (history or [])
    ]


def _contents_to_history(contents):
    return [
        {
            "role": content.role,
            "parts": [part.text for part in (content.parts or []) if getattr(part, "text", None)],
        }
        for content in contents
    ]


async def generate_reply(system_instruction: str, user_message: str, history: list | None = None):
    """Sends one message (with prior turns as context) to Gemini and returns
    (reply_text, updated_history). history/updated_history are plain
    {"role": ..., "parts": [str, ...]} dicts — not SDK objects — so they're
    safe to store and re-pass across calls without depending on SDK
    internals."""
    client = _get_client()
    chat = client.aio.chats.create(
        model=_MODEL,
        config=types.GenerateContentConfig(system_instruction=system_instruction),
        history=_history_to_contents(history),
    )
    response = await chat.send_message(user_message)
    updated_history = _contents_to_history(chat.get_history(curated=False))
    return response.text or "", updated_history
