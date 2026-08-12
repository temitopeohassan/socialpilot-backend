"""Post Intelligence API — analyze any post, compare two, or batch-analyze."""
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from pydantic import BaseModel
from typing import Optional, List

from app.core.database import get_session
from app.core.security import get_current_user
from app.models.user import User
from app.models.ai_models import PostIntelligenceReport
from app.services import post_intelligence

intelligence_router = APIRouter()


class AnalyzeRequest(BaseModel):
    caption: str
    platform_hint: str = ""         # linkedin | instagram | twitter | facebook | tiktok
    niche_hint: str = ""            # e.g. "fintech", "fashion", "SaaS"
    workspace_id: Optional[int] = None  # if provided, brand voice is applied


class CompareRequest(BaseModel):
    caption_a: str
    caption_b: str
    platform_hint: str = ""
    niche_hint: str = ""
    workspace_id: Optional[int] = None


class BatchRequest(BaseModel):
    captions: List[str]
    workspace_id: Optional[int] = None


@intelligence_router.post("/analyze")
async def analyze_post(
    body: AnalyzeRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Analyze any post — your own draft, a published post, or a competitor's.
    Returns a full intelligence report: intent, hook strength, psychological
    triggers, platform fit, predicted engagement type, and specific rewrites.
    """
    if not body.caption.strip():
        raise HTTPException(status_code=400, detail="Caption cannot be empty")
    if len(body.caption) > 5000:
        raise HTTPException(status_code=400, detail="Caption exceeds 5000 character limit")

    try:
        report = post_intelligence.analyze(
            caption=body.caption,
            workspace_id=body.workspace_id,
            user_id=current_user.id,
            platform_hint=body.platform_hint,
            niche_hint=body.niche_hint,
            session=session,
        )
        return post_intelligence._serialise(report)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@intelligence_router.post("/compare")
async def compare_posts(
    body: CompareRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Head-to-head comparison of two posts. Returns individual reports for each
    plus a comparative verdict: which wins, on what dimensions, and how to
    combine the best of both.
    """
    if not body.caption_a.strip() or not body.caption_b.strip():
        raise HTTPException(status_code=400, detail="Both captions are required")
    try:
        return post_intelligence.compare(
            caption_a=body.caption_a,
            caption_b=body.caption_b,
            workspace_id=body.workspace_id,
            user_id=current_user.id,
            platform_hint=body.platform_hint,
            niche_hint=body.niche_hint,
            session=session,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@intelligence_router.post("/batch")
async def batch_analyze(
    body: BatchRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Analyze up to 10 posts at once and surface cross-post patterns:
    recurring hook types, consistent weaknesses, average score, and
    batch-level recommendations. Useful for auditing a competitor's feed
    or reviewing a month of your own content.
    """
    if not body.captions:
        raise HTTPException(status_code=400, detail="At least one caption required")
    if len(body.captions) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 captions per batch")
    try:
        return post_intelligence.analyze_batch(
            captions=body.captions,
            workspace_id=body.workspace_id,
            user_id=current_user.id,
            session=session,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@intelligence_router.get("/history")
async def analysis_history(
    workspace_id: Optional[int] = None,
    limit: int = 20,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Past analyses for the current user, newest first."""
    query = select(PostIntelligenceReport).where(
        PostIntelligenceReport.user_id == current_user.id
    )
    if workspace_id:
        query = query.where(PostIntelligenceReport.workspace_id == workspace_id)
    query = query.order_by(PostIntelligenceReport.created_at.desc()).limit(limit)
    reports = session.exec(query).all()
    return [post_intelligence._serialise(r) for r in reports]


@intelligence_router.get("/report/{report_id}")
async def get_report(
    report_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Fetch a previously saved intelligence report by ID."""
    report = session.get(PostIntelligenceReport, report_id)
    if not report or report.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Report not found")
    return post_intelligence._serialise(report)
