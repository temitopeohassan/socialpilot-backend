"""
Campaign planner: single prompt -> structured multi-post campaign.

Pipeline:
  1. plan_campaign()      LLM turns the brief into a strategy + list of core
                          ideas (concepts), each with a suggested day offset.
  2. generate_variations() for each core idea, produce platform-specific
                          captions applying the brand voice.
  3. schedule_variations() use the best-time predictor to assign concrete
                          datetimes across the campaign window.

The output is a Campaign + CoreIdeas + PostVariations, all in DRAFT so the
human can review before anything is committed to the publishing pipeline.
"""
import json
from datetime import datetime, timedelta
from typing import Optional, List
from sqlmodel import Session, select

from app.services.llm import call_claude
from app.services.brand_voice import get_active_voice, render_voice_prompt
from app.services import posting_time
from app.models.ai_models import (
    Campaign, CampaignBrief, CoreIdea, PostVariation,
    CampaignStatus, VariationStatus, BrandVoice,
)
from app.models.user import SocialAccount

PLATFORM_LIMITS = {"twitter": 280, "instagram": 2200, "facebook": 63206,
                   "linkedin": 3000, "tiktok": 2200}


def plan_campaign(
    workspace_id: int, user_id: int, raw_prompt: str,
    parsed: dict, session: Session,
) -> Campaign:
    """Create a Campaign + CoreIdeas from a parsed brief.

    `parsed` is the structured params extracted by the intent router:
      { goal, platforms:[...], post_count:int,
        starts_at:iso, ends_at:iso, themes:[...] }
    """
    voice = get_active_voice(workspace_id, session)
    voice_block = render_voice_prompt(voice)

    platforms = parsed.get("platforms") or ["instagram"]
    count = int(parsed.get("post_count") or 3)
    goal = parsed.get("goal") or raw_prompt
    themes = parsed.get("themes") or []

    starts = _parse_dt(parsed.get("starts_at")) or datetime.utcnow() + timedelta(days=1)
    ends = _parse_dt(parsed.get("ends_at")) or starts + timedelta(days=7)

    # 1) Ask the model for a strategy + N core ideas
    planning_prompt = f"""{voice_block}

You are a senior social media strategist. Plan a campaign.

Goal: {goal}
Platforms: {', '.join(platforms)}
Number of posts: {count}
Themes/notes: {', '.join(themes) if themes else 'none specified'}
Window: {starts.date()} to {ends.date()}

Produce a coherent campaign with a narrative arc (don't make {count} unrelated
posts). Return ONLY JSON:
{{
  "name": "short campaign name",
  "strategy_summary": "2-3 sentences explaining the arc and why",
  "ideas": [
    {{"sequence": 1, "concept": "the core message/angle for this post",
      "day_offset": 0}}
  ]
}}
Exactly {count} ideas. day_offset is days from the campaign start."""

    plan = call_claude(planning_prompt, max_tokens=2000, expect_json=True)

    campaign = Campaign(
        workspace_id=workspace_id, created_by=user_id,
        name=plan.get("name", "Untitled Campaign"),
        goal=goal, status=CampaignStatus.PLANNING,
        brand_voice_id=voice.id if voice else None,
        starts_at=starts, ends_at=ends,
        strategy_summary=plan.get("strategy_summary", ""),
        target_platforms=json.dumps(platforms),
        post_count=count,
    )
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    session.add(CampaignBrief(
        campaign_id=campaign.id, raw_prompt=raw_prompt,
        parsed_params=json.dumps(parsed),
    ))

    for idea in plan.get("ideas", [])[:count]:
        offset = int(idea.get("day_offset", 0))
        session.add(CoreIdea(
            campaign_id=campaign.id,
            sequence=idea.get("sequence", 0),
            concept=idea.get("concept", ""),
            suggested_date=starts + timedelta(days=offset),
        ))
    session.commit()
    return campaign


def generate_variations(campaign_id: int, session: Session) -> List[PostVariation]:
    """For every core idea, generate a caption per target platform, on-brand."""
    campaign = session.get(Campaign, campaign_id)
    voice = session.get(BrandVoice, campaign.brand_voice_id) if campaign.brand_voice_id else None
    voice_block = render_voice_prompt(voice)
    platforms = json.loads(campaign.target_platforms or "[]")

    ideas = session.exec(
        select(CoreIdea).where(CoreIdea.campaign_id == campaign_id)
        .order_by(CoreIdea.sequence)
    ).all()

    # map platform -> a connected account id (first active one)
    accounts = session.exec(
        select(SocialAccount).where(
            SocialAccount.workspace_id == campaign.workspace_id,
            SocialAccount.is_active == True,  # noqa: E712
        )
    ).all()
    acct_by_platform = {}
    for a in accounts:
        acct_by_platform.setdefault(a.platform, a.id)

    created: List[PostVariation] = []
    for idea in ideas:
        # one call generates ALL platform variants for this idea, so they stay
        # consistent in message while differing in format
        limits = {p: PLATFORM_LIMITS.get(p, 2200) for p in platforms}
        prompt = f"""{voice_block}

Core idea for this post: "{idea.concept}"

Write one tailored version for EACH platform. Adapt length, format, and style
to each platform's norms — not just truncation. LinkedIn = professional depth,
Twitter = punchy, Instagram = visual+caption, etc. Stay on brand voice above.

Character limits: {json.dumps(limits)}

Return ONLY JSON:
{{"variations": [
  {{"platform": "linkedin",
    "caption": "...",
    "hashtags": ["#x"],
    "media_suggestion": "what image/video would work"}}
]}}
One entry per platform: {', '.join(platforms)}"""

        result = call_claude(prompt, max_tokens=2500, expect_json=True)
        for v in result.get("variations", []):
            platform = v.get("platform", "")
            caption = v.get("caption", "")
            var = PostVariation(
                core_idea_id=idea.id, campaign_id=campaign_id,
                platform=platform,
                account_id=acct_by_platform.get(platform),
                caption=caption,
                hashtags=json.dumps(v.get("hashtags", [])),
                media_suggestion=v.get("media_suggestion", ""),
                char_count=len(caption),
                status=VariationStatus.GENERATED,
            )
            session.add(var)
            created.append(var)

    campaign.status = CampaignStatus.DRAFT
    session.add(campaign)
    session.commit()
    for v in created:
        session.refresh(v)
    return created


def schedule_variations(campaign_id: int, session: Session) -> List[PostVariation]:
    """Assign concrete datetimes to every variation using the best-time
    predictor, per account, spread across the campaign window."""
    campaign = session.get(Campaign, campaign_id)
    variations = session.exec(
        select(PostVariation).where(PostVariation.campaign_id == campaign_id)
    ).all()

    # group by account so we distribute per-account without clustering
    by_account = {}
    for v in variations:
        by_account.setdefault(v.account_id, []).append(v)

    for account_id, vars_ in by_account.items():
        if not account_id:
            # no connected account for this platform; fall back to idea dates
            for v in vars_:
                idea = session.get(CoreIdea, v.core_idea_id)
                v.scheduled_at = idea.suggested_date
                v.predicted_engagement_score = 0.5
                session.add(v)
            continue

        slots = posting_time.distribute(
            account_id, len(vars_),
            campaign.starts_at, campaign.ends_at, session,
        )
        for v, when in zip(vars_, slots):
            v.scheduled_at = when
            v.predicted_engagement_score = posting_time.score_slot(account_id, when, session)
            session.add(v)

    session.commit()
    for v in variations:
        session.refresh(v)
    return variations


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None
