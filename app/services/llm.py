"""
Central Claude wrapper. Every AI feature goes through here so we have one
place for the model name, JSON parsing, retries, and error handling.
"""
import json
import logging
from typing import Union
import anthropic
from app.core.config import settings

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"


def _client() -> anthropic.Anthropic:
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # remove leading ```json / ``` and trailing ```
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    return text.strip()


def call_claude(
    prompt: str,
    system: str = "",
    max_tokens: int = 1024,
    expect_json: bool = False,
) -> Union[str, dict, list]:
    """Single entry point for all model calls.
    If expect_json=True, parses and returns the decoded object."""
    client = _client()
    kwargs = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    message = client.messages.create(**kwargs)
    text = message.content[0].text

    if not expect_json:
        return text

    cleaned = _strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # one retry asking the model to fix its own JSON
        fix = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[{
                "role": "user",
                "content": f"Return ONLY valid JSON, no prose:\n{cleaned}",
            }],
        )
        return json.loads(_strip_fences(fix.content[0].text))
