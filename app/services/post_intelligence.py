"""
Post Intelligence service.

Takes any post caption (and optionally an image) and produces a structured
intelligence report covering:
  - Intent detection (primary + secondary)
  - Hook analysis (type, strength, suggested rewrites)
  - Psychological triggers in use
  - Structural breakdown (hook / body / CTA)
  - Platform fit scores across all 5 platforms
  - Readability + tone profile
  - Predicted engagement pattern
  - Brand voice alignment (if voice configured)
  - Specific improvement suggestions
  - Overall score (0-10)

Two modes:
  analyze()  — single post
  compare()  — two posts side-by-side, head-to-head breakdown
"""
import json
from typing import Optional
from sqlmodel import Session

from app.services.llm import call_claude
from app.services.brand_voice import get_active_voice, render_voice_prompt
from app.models.ai_models import PostIntelligenceReport


# Full analysis prompt — returns a rich JSON schema
ANALYSIS_SYSTEM = """You are an expert social media strategist and copywriter
with deep knowledge of platform algorithms, audience psychology, and
high-performing content patterns. You analyze posts with precision and
deliver actionable, specific feedback — not generic advice."""

PLATFORM_CONTEXT = {
    "linkedin": "professional, thought-leadership, long-form friendly, no hard sell",
    "instagram": "visual-first, lifestyle-oriented, strong hook before fold, saves matter",
    "twitter": "punchy, witty, opinionated, debate-friendly, thread potential",
    "facebook": "community-oriented, shares drive reach, longer captions okay",
    "tiktok": "entertainment-first, trend-aware, strong pattern interrupt at start",
}


def analyze(
    caption: str,
    workspace_id: Optional[int],
    user_id: Optional[int],
    platform_hint: str = "",
    niche_hint: str = "",
    session: Optional[Session] = None,
) -> PostIntelligenceReport:
    """Full intelligence analysis of a single post."""

    voice_block = ""
    if workspace_id and session:
        voice = get_active_voice(workspace_id, session)
        if voice:
            voice_block = f"\n\nBRAND VOICE FOR THIS WORKSPACE:\n{render_voice_prompt(voice)}"

    platform_notes = ""
    if platform_hint and platform_hint in PLATFORM_CONTEXT:
        platform_notes = f"\nPlatform context: This post is for {platform_hint} — {PLATFORM_CONTEXT[platform_hint]}"
    if niche_hint:
        platform_notes += f"\nIndustry/niche: {niche_hint}"

    prompt = f"""Analyze this social media post and return a detailed intelligence report.
{platform_notes}{voice_block}

POST TO ANALYZE:
\"\"\"{caption}\"\"\"

Return ONLY valid JSON matching this exact schema:
{{
  "primary_intent": "one of: drive_engagement | build_awareness | announce | educate | sell | humanise | drive_clicks | entertain",
  "secondary_intent": "same options or empty string",
  "hook_score": 0-10,
  "hook_type": "one of: curiosity | number | bold_claim | question | story | pattern_interrupt | none",
  "hook_analysis": "specific analysis of what works or doesn't about the opening line",
  "hook_rewrite": "a stronger alternative opening line",
  "psychological_triggers": ["list", "of", "triggers", "used"],
  "structure": {{
    "has_hook": true/false,
    "has_body": true/false,
    "has_cta": true/false,
    "cta_quality": "none | vague | specific | strong",
    "cta_text": "the exact CTA phrase, or empty if none",
    "narrative_arc": "flat | list | story | argument | revelation"
  }},
  "platform_fit": {{
    "linkedin": {{ "score": 0-10, "notes": "why" }},
    "instagram": {{ "score": 0-10, "notes": "why" }},
    "twitter": {{ "score": 0-10, "notes": "why" }},
    "facebook": {{ "score": 0-10, "notes": "why" }},
    "tiktok": {{ "score": 0-10, "notes": "why" }}
  }},
  "readability": {{
    "grade_level": "e.g. Grade 8",
    "tone": "one of: formal | conversational | casual | inspirational | urgent | neutral",
    "formality": "low | medium | high",
    "sentence_variety": "low | medium | high",
    "emotional_temperature": "neutral | warm | urgent | provocative | empathetic"
  }},
  "predicted_engagement_type": "one of: comments | shares | saves | passive_likes | mixed",
  "predicted_engagement_reason": "one sentence explaining why",
  "brand_voice_fit": null,
  "improvements": [
    {{
      "area": "hook | cta | structure | tone | length | platform_fit",
      "issue": "specific problem",
      "suggestion": "specific fix with example rewrite where applicable"
    }}
  ],
  "overall_score": 0-10,
  "summary": "2-3 sentence executive summary of the post's strengths and biggest opportunity"
}}

Be specific and actionable. Score fairly — a 10 should be rare. Improvements must include
concrete rewrites, not vague advice like 'make the hook stronger'."""

    result = call_claude(prompt, system=ANALYSIS_SYSTEM, max_tokens=2000, expect_json=True)

    # Brand voice fit (separate check if voice configured)
    brand_fit = None
    if workspace_id and session:
        voice = get_active_voice(workspace_id, session)
        if voice:
            from app.services.brand_voice import check_brand_alignment
            alignment = check_brand_alignment(voice, caption)
            brand_fit = alignment["score"]

    report = PostIntelligenceReport(
        workspace_id=workspace_id,
        user_id=user_id,
        caption=caption,
        platform_hint=platform_hint,
        niche_hint=niche_hint,
        primary_intent=result.get("primary_intent", ""),
        secondary_intent=result.get("secondary_intent", ""),
        hook_score=float(result.get("hook_score", 0)),
        hook_type=result.get("hook_type", ""),
        hook_analysis=result.get("hook_analysis", ""),
        psychological_triggers=json.dumps(result.get("psychological_triggers", [])),
        structure_analysis=json.dumps(result.get("structure", {})),
        platform_fit=json.dumps(result.get("platform_fit", {})),
        readability=json.dumps(result.get("readability", {})),
        predicted_engagement_type=result.get("predicted_engagement_type", ""),
        brand_voice_fit=brand_fit,
        improvements=json.dumps(result.get("improvements", [])),
        overall_score=float(result.get("overall_score", 0)),
        summary=result.get("summary", ""),
    )

    if session:
        session.add(report)
        session.commit()
        session.refresh(report)

    return report


def compare(
    caption_a: str,
    caption_b: str,
    workspace_id: Optional[int],
    user_id: Optional[int],
    platform_hint: str = "",
    niche_hint: str = "",
    session: Optional[Session] = None,
) -> dict:
    """Head-to-head comparison of two posts. Returns both individual reports
    plus a comparative verdict."""

    report_a = analyze(caption_a, workspace_id, user_id, platform_hint, niche_hint, session)
    report_b = analyze(caption_b, workspace_id, user_id, platform_hint, niche_hint, session)

    verdict_prompt = f"""Compare these two social media posts and give a concise verdict.

Post A (score {report_a.overall_score}/10): "{caption_a[:300]}"
Post B (score {report_b.overall_score}/10): "{caption_b[:300]}"

Return ONLY JSON:
{{
  "winner": "A" or "B" or "tie",
  "margin": "close | clear | decisive",
  "verdict": "2-3 sentences explaining which wins and why",
  "a_wins_on": ["what A does better"],
  "b_wins_on": ["what B does better"],
  "hybrid_suggestion": "one sentence on how to combine the best of both"
}}"""

    verdict = call_claude(verdict_prompt, max_tokens=600, expect_json=True)

    return {
        "report_a": _serialise(report_a),
        "report_b": _serialise(report_b),
        "comparison": verdict,
    }


def analyze_batch(
    captions: list,
    workspace_id: Optional[int],
    user_id: Optional[int],
    session: Optional[Session] = None,
) -> dict:
    """Analyze a batch of posts and surface cross-post patterns.
    Returns individual reports + pattern analysis."""

    reports = [analyze(c, workspace_id, user_id, session=session) for c in captions[:10]]

    # Pattern analysis across the batch
    hook_types = [r.hook_type for r in reports]
    scores = [r.overall_score for r in reports]
    intents = [r.primary_intent for r in reports]

    pattern_prompt = f"""Analyze patterns across {len(reports)} social posts.

Hook types used: {hook_types}
Overall scores: {scores}
Primary intents: {intents}
Average score: {sum(scores)/len(scores):.1f}

Return ONLY JSON:
{{
  "patterns": ["pattern 1", "pattern 2"],
  "strengths": ["consistent strength 1"],
  "weaknesses": ["recurring weakness 1"],
  "recommendations": ["batch-level recommendation 1", "recommendation 2"]
}}"""

    patterns = call_claude(pattern_prompt, max_tokens=700, expect_json=True)

    return {
        "reports": [_serialise(r) for r in reports],
        "patterns": patterns,
        "average_score": round(sum(scores) / len(scores), 1),
    }


def _serialise(report: PostIntelligenceReport) -> dict:
    """Turn a report into a response-ready dict with parsed JSON fields."""
    return {
        "id": report.id,
        "caption": report.caption,
        "platform_hint": report.platform_hint,
        "primary_intent": report.primary_intent,
        "secondary_intent": report.secondary_intent,
        "hook_score": report.hook_score,
        "hook_type": report.hook_type,
        "hook_analysis": report.hook_analysis,
        "psychological_triggers": json.loads(report.psychological_triggers or "[]"),
        "structure": json.loads(report.structure_analysis or "{}"),
        "platform_fit": json.loads(report.platform_fit or "{}"),
        "readability": json.loads(report.readability or "{}"),
        "predicted_engagement_type": report.predicted_engagement_type,
        "brand_voice_fit": report.brand_voice_fit,
        "improvements": json.loads(report.improvements or "[]"),
        "overall_score": report.overall_score,
        "summary": report.summary,
        "created_at": report.created_at.isoformat(),
    }
