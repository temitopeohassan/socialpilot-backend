"""AI-first API routers."""
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from pydantic import BaseModel
from typing import Optional, List
import json

from app.core.database import get_session
from app.core.security import get_current_user
from app.models.user import User
from app.models.ai_models import (
    BrandVoice, Campaign, CoreIdea, PostVariation, ApprovalRequest,
    AgentRun, CampaignStatus, VariationStatus,
)
from app.services import agent, campaign_planner, approval, posting_time, brand_voice


# ═══ Agent / Natural-language router ══════════════════════════════════════════
agent_router = APIRouter()


class AgentPrompt(BaseModel):
    workspace_id: int
    prompt: str
    auto_execute: bool = True


@agent_router.post("/chat")
async def agent_chat(
    body: AgentPrompt,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """The natural-language front door.
    e.g. 'Schedule three LinkedIn posts for next week about our new product'."""
    try:
        return agent.dispatch(
            body.prompt, body.workspace_id, current_user.id, session,
            auto_execute=body.auto_execute,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@agent_router.post("/interpret")
async def agent_interpret(
    body: AgentPrompt,
    current_user: User = Depends(get_current_user),
):
    """Preview how the agent interprets a prompt WITHOUT executing it."""
    return agent.interpret(body.prompt)


@agent_router.get("/runs")
async def list_runs(
    workspace_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    runs = session.exec(
        select(AgentRun).where(AgentRun.workspace_id == workspace_id)
        .order_by(AgentRun.created_at.desc()).limit(50)
    ).all()
    return runs


@agent_router.post("/runs/{run_id}/undo")
async def undo_run(
    run_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return agent.undo(run_id, session)


# ═══ Campaigns ════════════════════════════════════════════════════════════════
campaign_router = APIRouter()


class CampaignFromPrompt(BaseModel):
    workspace_id: int
    prompt: str


@campaign_router.post("/plan")
async def plan_from_prompt(
    body: CampaignFromPrompt,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Explicitly plan a campaign from a prompt (full pipeline)."""
    interp = agent.interpret(body.prompt)
    params = interp.get("params", {})
    if not params.get("platforms"):
        params["platforms"] = ["linkedin"]
    campaign = campaign_planner.plan_campaign(
        body.workspace_id, current_user.id, body.prompt, params, session
    )
    variations = campaign_planner.generate_variations(campaign.id, session)
    campaign_planner.schedule_variations(campaign.id, session)
    return {"campaign": campaign, "variation_count": len(variations)}


@campaign_router.get("")
async def list_campaigns(
    workspace_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return session.exec(
        select(Campaign).where(Campaign.workspace_id == workspace_id)
        .order_by(Campaign.created_at.desc())
    ).all()


@campaign_router.get("/{campaign_id}")
async def get_campaign(
    campaign_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Full campaign detail: ideas + variations grouped by idea."""
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    ideas = session.exec(
        select(CoreIdea).where(CoreIdea.campaign_id == campaign_id)
        .order_by(CoreIdea.sequence)
    ).all()
    variations = session.exec(
        select(PostVariation).where(PostVariation.campaign_id == campaign_id)
    ).all()

    var_by_idea = {}
    for v in variations:
        var_by_idea.setdefault(v.core_idea_id, []).append(v)

    return {
        "campaign": campaign,
        "strategy": campaign.strategy_summary,
        "ideas": [
            {"idea": idea, "variations": var_by_idea.get(idea.id, [])}
            for idea in ideas
        ],
    }


@campaign_router.post("/{campaign_id}/regenerate")
async def regenerate_variations(
    campaign_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Regenerate all platform variations (e.g. after brand voice changes)."""
    # clear old variations
    old = session.exec(
        select(PostVariation).where(PostVariation.campaign_id == campaign_id)
    ).all()
    for v in old:
        session.delete(v)
    session.commit()
    variations = campaign_planner.generate_variations(campaign_id, session)
    campaign_planner.schedule_variations(campaign_id, session)
    return {"variation_count": len(variations)}


@campaign_router.post("/{campaign_id}/submit-for-approval")
async def submit_for_approval(
    campaign_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Run AI pre-screen on every variation and create approval requests."""
    campaign = session.get(Campaign, campaign_id)
    variations = session.exec(
        select(PostVariation).where(PostVariation.campaign_id == campaign_id)
    ).all()

    results = []
    for v in variations:
        req = approval.screen_variation(
            v.id, campaign.workspace_id, current_user.id, session
        )
        results.append(req)

    auto = sum(1 for r in results if r.status.value == "auto_approved")
    pending = sum(1 for r in results if r.status.value == "pending")
    campaign.status = CampaignStatus.PENDING_APPROVAL
    session.add(campaign)
    session.commit()

    return {
        "total": len(results),
        "auto_approved": auto,
        "needs_review": pending,
        "message": f"{auto} auto-approved, {pending} routed for human review.",
    }


class VariationEdit(BaseModel):
    caption: Optional[str] = None
    scheduled_at: Optional[str] = None


@campaign_router.patch("/variations/{variation_id}")
async def edit_variation(
    variation_id: int,
    body: VariationEdit,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    var = session.get(PostVariation, variation_id)
    if not var:
        raise HTTPException(status_code=404, detail="Variation not found")
    if body.caption is not None:
        var.caption = body.caption
        var.char_count = len(body.caption)
        var.status = VariationStatus.EDITED
    if body.scheduled_at:
        from datetime import datetime
        var.scheduled_at = datetime.fromisoformat(body.scheduled_at.replace("Z", "+00:00")).replace(tzinfo=None)
    session.add(var)
    session.commit()
    session.refresh(var)
    return var


# ═══ Brand Voice ══════════════════════════════════════════════════════════════
voice_router = APIRouter()


class BrandVoiceUpsert(BaseModel):
    workspace_id: int
    name: str = "Default"
    description: str = ""
    tone_attributes: List[str] = []
    target_audience: str = ""
    writing_sample: str = ""
    do_rules: List[str] = []
    dont_rules: List[str] = []
    banned_words: List[str] = []
    preferred_emoji_level: str = "some"
    preferred_hashtag_count: int = 5


@voice_router.get("")
async def get_voice(
    workspace_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    voice = brand_voice.get_active_voice(workspace_id, session)
    return voice or {"message": "No brand voice configured yet."}


@voice_router.post("")
async def upsert_voice(
    body: BrandVoiceUpsert,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    existing = brand_voice.get_active_voice(body.workspace_id, session)
    if existing:
        voice = existing
    else:
        voice = BrandVoice(workspace_id=body.workspace_id)

    voice.name = body.name
    voice.description = body.description
    voice.tone_attributes = json.dumps(body.tone_attributes)
    voice.target_audience = body.target_audience
    voice.writing_sample = body.writing_sample
    voice.do_rules = json.dumps(body.do_rules)
    voice.dont_rules = json.dumps(body.dont_rules)
    voice.banned_words = json.dumps(body.banned_words)
    voice.preferred_emoji_level = body.preferred_emoji_level
    voice.preferred_hashtag_count = body.preferred_hashtag_count
    voice.is_active = True

    session.add(voice)
    session.commit()
    session.refresh(voice)
    return voice


@voice_router.post("/learn-from-samples")
async def learn_from_samples(
    workspace_id: int,
    samples: List[str],
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Bootstrap a brand voice by analyzing existing posts the brand likes."""
    from app.services.llm import call_claude
    joined = "\n---\n".join(samples[:10])
    result = call_claude(
        f"""Analyze these posts and extract the brand voice. Return ONLY JSON:
{{"description":"...","tone_attributes":["..."],"target_audience":"...",
  "do_rules":["..."],"dont_rules":["..."]}}

Posts:
{joined}""",
        max_tokens=800, expect_json=True,
    )
    voice = brand_voice.get_active_voice(workspace_id, session) or BrandVoice(workspace_id=workspace_id)
    voice.description = result.get("description", "")
    voice.tone_attributes = json.dumps(result.get("tone_attributes", []))
    voice.target_audience = result.get("target_audience", "")
    voice.do_rules = json.dumps(result.get("do_rules", []))
    voice.dont_rules = json.dumps(result.get("dont_rules", []))
    voice.writing_sample = samples[0] if samples else ""
    voice.is_active = True
    session.add(voice)
    session.commit()
    session.refresh(voice)
    return voice


# ═══ Approvals ════════════════════════════════════════════════════════════════
approval_router = APIRouter()


@approval_router.get("")
async def list_pending(
    workspace_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Pending approvals, highest AI risk first (triage order)."""
    reqs = approval.pending_for_workspace(workspace_id, session)
    enriched = []
    for r in reqs:
        var = session.get(PostVariation, r.variation_id) if r.variation_id else None
        enriched.append({
            "request": r,
            "findings": json.loads(r.ai_findings or "[]"),
            "variation": var,
        })
    return enriched


class Decision(BaseModel):
    decision: str  # approved | changes_requested | rejected
    comment: str = ""


@approval_router.post("/{request_id}/decide")
async def decide_approval(
    request_id: int,
    body: Decision,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    try:
        req = approval.decide(request_id, body.decision, current_user.id, session, body.comment)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # learning loop: on approval, fold into brand voice memory
    if body.decision == "approved" and req.variation_id:
        var = session.get(PostVariation, req.variation_id)
        voice = brand_voice.get_active_voice(req.workspace_id, session)
        if var and voice:
            brand_voice.learn_from_approval(
                voice, var.caption,
                was_edited=(var.status == VariationStatus.EDITED),
                original_caption=var.caption, session=session,
            )
    return req


# ═══ Posting-time intelligence ════════════════════════════════════════════════
timing_router = APIRouter()


@timing_router.get("/best-times/{account_id}")
async def best_times(
    account_id: int,
    n: int = 7,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Top recommended posting slots for an account, with confidence levels."""
    slots = posting_time.best_slots(account_id, session, n)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return [
        {**s, "day_name": days[s["day_of_week"]],
         "label": f"{days[s['day_of_week']]} {s['hour']:02d}:00"}
        for s in slots
    ]
