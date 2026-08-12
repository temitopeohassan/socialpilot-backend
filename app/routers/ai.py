from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from app.core.security import get_current_user
from app.core.config import settings
from app.models.user import User
import anthropic

router = APIRouter()


class CaptionRequest(BaseModel):
    topic: str
    platform: str  # facebook, instagram, twitter, linkedin, tiktok
    tone: Optional[str] = "professional"  # casual, professional, funny, inspirational
    include_hashtags: bool = True
    include_emoji: bool = True
    variants: int = 3  # how many caption variants to generate


class RewriteRequest(BaseModel):
    original: str
    platform: str
    tone: Optional[str] = "professional"


class HookRequest(BaseModel):
    topic: str
    platform: str


PLATFORM_LIMITS = {
    "twitter": 280,
    "instagram": 2200,
    "facebook": 63206,
    "linkedin": 3000,
    "tiktok": 2200,
}


@router.post("/captions")
async def generate_captions(
    data: CaptionRequest,
    current_user: User = Depends(get_current_user),
):
    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="AI service not configured")

    char_limit = PLATFORM_LIMITS.get(data.platform, 2200)
    hashtag_instruction = "Include 5-10 relevant hashtags." if data.include_hashtags else "Do not include hashtags."
    emoji_instruction = "Use relevant emojis to make it engaging." if data.include_emoji else "Do not use emojis."

    prompt = f"""Generate {data.variants} different social media captions for {data.platform.upper()} about: "{data.topic}"

Requirements:
- Tone: {data.tone}
- Character limit: {char_limit} characters each
- {hashtag_instruction}
- {emoji_instruction}
- Each caption should be distinct and take a different angle

Return ONLY a JSON object like:
{{"captions": ["caption1", "caption2", "caption3"]}}

No explanations, just the JSON."""

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    import json
    text = message.content[0].text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    result = json.loads(text.strip())
    return result


@router.post("/rewrite")
async def rewrite_caption(
    data: RewriteRequest,
    current_user: User = Depends(get_current_user),
):
    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="AI service not configured")

    char_limit = PLATFORM_LIMITS.get(data.platform, 2200)
    prompt = f"""Rewrite this social media caption for {data.platform.upper()} with a {data.tone} tone.
Keep it under {char_limit} characters. Preserve the core message but make it more engaging.

Original:
{data.original}

Return ONLY a JSON object: {{"rewritten": "your rewritten caption here"}}"""

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    import json
    text = message.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


@router.post("/hooks")
async def generate_hooks(
    data: HookRequest,
    current_user: User = Depends(get_current_user),
):
    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="AI service not configured")

    prompt = f"""Generate 5 attention-grabbing opening hooks for a {data.platform.upper()} post about: "{data.topic}"

Each hook should be 1-2 sentences max and immediately grab attention.
Return ONLY a JSON object: {{"hooks": ["hook1", "hook2", "hook3", "hook4", "hook5"]}}"""

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    import json
    text = message.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


@router.post("/hashtags")
async def suggest_hashtags(
    topic: str,
    platform: str = "instagram",
    count: int = 15,
    current_user: User = Depends(get_current_user),
):
    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="AI service not configured")

    prompt = f"""Suggest {count} relevant hashtags for a {platform.upper()} post about: "{topic}"
Mix popular and niche hashtags for best reach.
Return ONLY a JSON object: {{"hashtags": ["#tag1", "#tag2", ...]}}"""

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    import json
    text = message.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())
