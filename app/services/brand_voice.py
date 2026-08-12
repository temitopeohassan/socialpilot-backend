"""
Brand voice memory service.

Two responsibilities:
  1. Render the active brand voice into a system-prompt fragment that gets
     injected into EVERY generation call (this is what makes output on-brand
     by default instead of per-request).
  2. Learn: when a human approves or edits AI output, fold that signal back
     into a rolling learned_summary so the voice sharpens over time.
"""
import json
from typing import Optional
from sqlmodel import Session, select
from app.models.ai_models import BrandVoice
from app.services.llm import call_claude


def get_active_voice(workspace_id: int, session: Session) -> Optional[BrandVoice]:
    return session.exec(
        select(BrandVoice).where(
            BrandVoice.workspace_id == workspace_id,
            BrandVoice.is_active == True,  # noqa: E712
        )
    ).first()


def render_voice_prompt(voice: Optional[BrandVoice]) -> str:
    """Turn a BrandVoice row into an instruction block for the LLM.
    Returns empty string if no voice configured (graceful degradation)."""
    if not voice:
        return ""

    tone = json.loads(voice.tone_attributes or "[]")
    do_rules = json.loads(voice.do_rules or "[]")
    dont_rules = json.loads(voice.dont_rules or "[]")
    banned = json.loads(voice.banned_words or "[]")

    parts = ["# BRAND VOICE (apply to all output)"]
    if voice.description:
        parts.append(f"Voice: {voice.description}")
    if tone:
        parts.append(f"Tone attributes: {', '.join(tone)}")
    if voice.target_audience:
        parts.append(f"Audience: {voice.target_audience}")
    if voice.writing_sample:
        parts.append(f"Reference writing sample:\n\"\"\"\n{voice.writing_sample}\n\"\"\"")
    if voice.learned_summary:
        parts.append(f"Learned preferences (from past approvals): {voice.learned_summary}")
    if do_rules:
        parts.append("ALWAYS: " + "; ".join(do_rules))
    if dont_rules:
        parts.append("NEVER: " + "; ".join(dont_rules))
    if banned:
        parts.append(f"Banned words (never use): {', '.join(banned)}")
    parts.append(f"Emoji usage: {voice.preferred_emoji_level}")
    parts.append(f"Default hashtag count: {voice.preferred_hashtag_count}")

    return "\n".join(parts)


def check_brand_alignment(voice: Optional[BrandVoice], caption: str) -> dict:
    """Score how well a caption matches the brand voice. Cheap deterministic
    checks first (banned words), then an LLM judgment for tone fit."""
    findings = []
    if not voice:
        return {"score": 1.0, "findings": []}

    banned = json.loads(voice.banned_words or "[]")
    lowered = caption.lower()
    for word in banned:
        if word.lower() in lowered:
            findings.append(f"Uses banned word: '{word}'")

    # LLM tone-fit score
    voice_block = render_voice_prompt(voice)
    prompt = f"""{voice_block}

Rate how well this caption matches the brand voice above, 0.0 to 1.0.
Caption: "{caption}"

Return ONLY JSON: {{"score": 0.0-1.0, "issues": ["..."]}}"""
    try:
        result = call_claude(prompt, max_tokens=256, expect_json=True)
        score = float(result.get("score", 0.8))
        findings.extend(result.get("issues", []))
    except Exception:
        score = 0.8  # fail open

    # Banned-word hits cap the score hard
    if any("banned word" in f for f in findings):
        score = min(score, 0.3)

    return {"score": round(score, 2), "findings": findings}


def learn_from_approval(
    voice: BrandVoice, approved_caption: str, was_edited: bool,
    original_caption: str, session: Session,
):
    """Fold an approved (and possibly human-edited) post back into the voice
    memory. If the human edited it, the diff is the strongest signal."""
    edit_note = ""
    if was_edited and original_caption != approved_caption:
        edit_note = f"""
The AI originally wrote:
"{original_caption}"
The human changed it to:
"{approved_caption}"
The edit reveals a preference — capture it."""

    prompt = f"""You maintain a rolling summary of a brand's learned voice preferences.

Current learned summary:
"{voice.learned_summary or '(none yet)'}"

A new post was just approved:
"{approved_caption}"
{edit_note}

Update the learned summary in 2-3 sentences. Keep durable patterns, drop
one-offs. Return ONLY JSON: {{"summary": "..."}}"""
    try:
        result = call_claude(prompt, max_tokens=400, expect_json=True)
        voice.learned_summary = result.get("summary", voice.learned_summary)
        voice.sample_count += 1
        session.add(voice)
        session.commit()
    except Exception:
        pass  # learning is best-effort, never blocks approval
