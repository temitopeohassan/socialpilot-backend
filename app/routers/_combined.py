from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlmodel import Session, select
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta
import json, os, uuid, shutil

from app.core.database import get_session
from app.core.security import get_current_user
from app.core.config import settings
from app.models.user import (
    User, SocialAccount, Platform, WorkspaceMember, MediaFile,
    PostAnalytics, PostAccount, QueueSlot, RSSFeed, Workspace,
)

# ─── Accounts Router ─────────────────────────────────────────────────────────
accounts_router = APIRouter()


class AccountCreate(BaseModel):
    workspace_id: int
    platform: Platform
    platform_user_id: str
    username: str
    display_name: str
    avatar_url: Optional[str] = None
    access_token: str
    refresh_token: Optional[str] = None


@accounts_router.get("")
async def list_accounts(
    workspace_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    accounts = session.exec(
        select(SocialAccount).where(SocialAccount.workspace_id == workspace_id)
    ).all()
    return accounts


@accounts_router.post("")
async def connect_account(
    data: AccountCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    account = SocialAccount(**data.dict())
    session.add(account)
    session.commit()
    session.refresh(account)

    # Seed best-time-to-post heuristics with platform priors so the predictor
    # has something to work with before this account accrues its own analytics.
    from app.services.posting_time import seed_heuristics
    seed_heuristics(account.id, account.platform.value if hasattr(account.platform, "value") else account.platform, session)

    return account


@accounts_router.delete("/{account_id}")
async def disconnect_account(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    account = session.get(SocialAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    account.is_active = False
    session.add(account)
    session.commit()
    return {"message": "Account disconnected"}


# ─── Media Router ─────────────────────────────────────────────────────────────
media_router = APIRouter()


@media_router.get("")
async def list_media(
    workspace_id: int,
    file_type: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    query = select(MediaFile).where(MediaFile.workspace_id == workspace_id)
    if file_type:
        query = query.where(MediaFile.file_type == file_type)
    query = query.order_by(MediaFile.created_at.desc()).offset((page - 1) * per_page).limit(per_page)
    return session.exec(query).all()


@media_router.post("/upload")
async def upload_media(
    workspace_id: int,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(file.filename)[1]
    filename = f"{uuid.uuid4()}{ext}"
    filepath = os.path.join(settings.UPLOAD_DIR, filename)

    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)

    size = os.path.getsize(filepath)
    file_type = "image" if file.content_type.startswith("image") else "video"

    media = MediaFile(
        workspace_id=workspace_id,
        uploaded_by=current_user.id,
        filename=filename,
        original_name=file.filename,
        file_type=file_type,
        mime_type=file.content_type,
        size_bytes=size,
        url=f"/uploads/{filename}",
    )
    session.add(media)
    session.commit()
    session.refresh(media)
    return media


@media_router.delete("/{media_id}")
async def delete_media(
    media_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    media = session.get(MediaFile, media_id)
    if not media:
        raise HTTPException(status_code=404, detail="File not found")
    filepath = os.path.join(settings.UPLOAD_DIR, media.filename)
    if os.path.exists(filepath):
        os.remove(filepath)
    session.delete(media)
    session.commit()
    return {"message": "File deleted"}


# ─── Analytics Router ────────────────────────────────────────────────────────
analytics_router = APIRouter()


@analytics_router.get("/overview")
async def analytics_overview(
    workspace_id: int,
    days: int = Query(30, ge=1, le=365),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """High-level publishing stats + aggregated engagement totals."""
    from app.models.user import Post, PostStatus, PostAnalytics, PostAccount
    since = datetime.utcnow() - timedelta(days=days)
    posts = session.exec(
        select(Post).where(Post.workspace_id == workspace_id, Post.created_at >= since)
    ).all()

    total = len(posts)
    published = sum(1 for p in posts if p.status == PostStatus.PUBLISHED)
    scheduled = sum(1 for p in posts if p.status == PostStatus.SCHEDULED)
    failed = sum(1 for p in posts if p.status == PostStatus.FAILED)
    drafts = sum(1 for p in posts if p.status == PostStatus.DRAFT)

    # Aggregate latest analytics snapshots for the workspace
    post_ids = [p.id for p in posts if p.status == PostStatus.PUBLISHED]
    total_likes = total_comments = total_shares = total_impressions = total_reach = total_clicks = 0
    avg_er = 0.0

    if post_ids:
        # Get the most recent snapshot per post_account
        analytics = session.exec(
            select(PostAnalytics)
            .where(
                PostAnalytics.post_id.in_(post_ids),
                PostAnalytics.snapshot_label == "24h",
            )
        ).all()
        if not analytics:
            # Fall back to any snapshot if 24h not available yet
            analytics = session.exec(
                select(PostAnalytics).where(PostAnalytics.post_id.in_(post_ids))
            ).all()

        for a in analytics:
            total_likes += a.likes
            total_comments += a.comments
            total_shares += a.shares
            total_impressions += a.impressions
            total_reach += a.reach
            total_clicks += a.clicks

        avg_er = round(sum(a.engagement_rate for a in analytics) / len(analytics), 4) if analytics else 0.0

    return {
        "total_posts": total,
        "published": published,
        "scheduled": scheduled,
        "failed": failed,
        "drafts": drafts,
        "days": days,
        "totals": {
            "likes": total_likes,
            "comments": total_comments,
            "shares": total_shares,
            "impressions": total_impressions,
            "reach": total_reach,
            "clicks": total_clicks,
            "avg_engagement_rate": avg_er,
        },
    }


@analytics_router.get("/performance")
async def analytics_performance(
    workspace_id: int,
    account_id: Optional[int] = None,
    days: int = Query(30, ge=1, le=365),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Per-day aggregated engagement for charting. Uses real PostAnalytics data
    where available, falls back to zeros (not fake random data)."""
    from app.models.user import Post, PostStatus, PostAnalytics
    from collections import defaultdict

    since = datetime.utcnow() - timedelta(days=days)
    query = select(PostAnalytics).where(PostAnalytics.fetched_at >= since)
    if account_id:
        query = query.where(PostAnalytics.account_id == account_id)
    else:
        # scope to workspace via post_ids
        post_ids = [p.id for p in session.exec(
            select(Post).where(Post.workspace_id == workspace_id)
        ).all()]
        if post_ids:
            query = query.where(PostAnalytics.post_id.in_(post_ids))

    rows = session.exec(query).all()

    # Aggregate by published_at date
    by_date = defaultdict(lambda: {"likes": 0, "comments": 0, "shares": 0,
                                   "impressions": 0, "reach": 0, "clicks": 0,
                                   "engagement_rate_sum": 0.0, "count": 0})
    for row in rows:
        key = (row.published_at or row.fetched_at).strftime("%Y-%m-%d")
        by_date[key]["likes"] += row.likes
        by_date[key]["comments"] += row.comments
        by_date[key]["shares"] += row.shares
        by_date[key]["impressions"] += row.impressions
        by_date[key]["reach"] += row.reach
        by_date[key]["clicks"] += row.clicks
        by_date[key]["engagement_rate_sum"] += row.engagement_rate
        by_date[key]["count"] += 1

    result = []
    for i in range(days):
        date = (since + timedelta(days=i + 1)).strftime("%Y-%m-%d")
        d = by_date.get(date, {})
        count = d.get("count", 0)
        result.append({
            "date": date,
            "likes": d.get("likes", 0),
            "comments": d.get("comments", 0),
            "shares": d.get("shares", 0),
            "impressions": d.get("impressions", 0),
            "reach": d.get("reach", 0),
            "clicks": d.get("clicks", 0),
            "avg_engagement_rate": round(d.get("engagement_rate_sum", 0) / count, 4) if count else 0.0,
        })
    return result


@analytics_router.get("/posts/{post_id}")
async def post_analytics_detail(
    post_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """All analytics snapshots for a single post, grouped by platform,
    ordered by snapshot time so the engagement curve can be plotted."""
    from app.models.user import PostAnalytics, PostAccount, SocialAccount
    from collections import defaultdict

    snapshots = session.exec(
        select(PostAnalytics)
        .where(PostAnalytics.post_id == post_id)
        .order_by(PostAnalytics.fetched_at)
    ).all()

    by_platform = defaultdict(list)
    for s in snapshots:
        by_platform[s.platform].append({
            "snapshot": s.snapshot_label,
            "fetched_at": s.fetched_at.isoformat(),
            "likes": s.likes,
            "comments": s.comments,
            "shares": s.shares,
            "saves": s.saves,
            "impressions": s.impressions,
            "reach": s.reach,
            "clicks": s.clicks,
            "video_views": s.video_views,
            "engagement_rate": s.engagement_rate,
        })

    return {"post_id": post_id, "by_platform": dict(by_platform)}


@analytics_router.get("/top-posts")
async def top_posts(
    workspace_id: int,
    metric: str = Query("impressions", pattern="^(impressions|likes|comments|shares|clicks|engagement_rate)$"),
    limit: int = Query(10, ge=1, le=50),
    days: int = Query(30, ge=1, le=365),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Top performing posts for a workspace by any metric."""
    from app.models.user import Post, PostStatus, PostAnalytics
    since = datetime.utcnow() - timedelta(days=days)

    post_ids = [p.id for p in session.exec(
        select(Post).where(
            Post.workspace_id == workspace_id,
            Post.status == PostStatus.PUBLISHED,
            Post.published_at >= since,
        )
    ).all()]
    if not post_ids:
        return []

    # Get best snapshot per post (24h preferred)
    rows = session.exec(
        select(PostAnalytics).where(
            PostAnalytics.post_id.in_(post_ids),
        ).order_by(PostAnalytics.fetched_at.desc())
    ).all()

    # Keep best snapshot per post
    best = {}
    for r in rows:
        if r.post_id not in best:
            best[r.post_id] = r

    # Sort by requested metric
    sorted_rows = sorted(best.values(), key=lambda r: getattr(r, metric, 0), reverse=True)[:limit]

    result = []
    for r in sorted_rows:
        post = session.get(Post, r.post_id)
        result.append({
            "post_id": r.post_id,
            "platform": r.platform,
            "caption_preview": (post.caption[:120] + "…") if post and len(post.caption) > 120 else (post.caption if post else ""),
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "likes": r.likes,
            "comments": r.comments,
            "shares": r.shares,
            "impressions": r.impressions,
            "reach": r.reach,
            "clicks": r.clicks,
            "engagement_rate": r.engagement_rate,
            "snapshot": r.snapshot_label,
        })
    return result


@analytics_router.post("/fetch-now/{post_account_id}")
async def trigger_analytics_fetch(
    post_account_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Manually trigger an immediate analytics fetch for a PostAccount.
    Useful for testing or when a user wants fresh data on demand."""
    from app.services.analytics_fetcher import fetch_and_store
    result = fetch_and_store(post_account_id, "manual", session)
    if not result:
        return {"success": False, "message": "Fetch failed — check account token and platform_post_id"}
    return {
        "success": True,
        "likes": result.likes,
        "comments": result.comments,
        "shares": result.shares,
        "impressions": result.impressions,
        "reach": result.reach,
        "engagement_rate": result.engagement_rate,
    }


# ─── Scheduler Router ────────────────────────────────────────────────────────
scheduler_router = APIRouter()


class QueueSlotCreate(BaseModel):
    account_id: int
    day_of_week: int
    time_of_day: str


@scheduler_router.get("/queue/{account_id}")
async def get_queue_slots(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    slots = session.exec(select(QueueSlot).where(QueueSlot.account_id == account_id)).all()
    return slots


@scheduler_router.post("/queue")
async def create_queue_slot(
    data: QueueSlotCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    slot = QueueSlot(**data.dict())
    session.add(slot)
    session.commit()
    session.refresh(slot)
    return slot


@scheduler_router.delete("/queue/{slot_id}")
async def delete_queue_slot(
    slot_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    slot = session.get(QueueSlot, slot_id)
    if slot:
        session.delete(slot)
        session.commit()
    return {"message": "Slot removed"}


# ─── RSS Router ──────────────────────────────────────────────────────────────
rss_router = APIRouter()


class RSSFeedCreate(BaseModel):
    workspace_id: int
    name: str
    feed_url: str
    account_ids: List[int]
    caption_template: Optional[str] = "{title}\n\n{url}"
    sync_interval_hours: int = 1


@rss_router.get("")
async def list_rss_feeds(
    workspace_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return session.exec(select(RSSFeed).where(RSSFeed.workspace_id == workspace_id)).all()


@rss_router.post("")
async def create_rss_feed(
    data: RSSFeedCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    feed = RSSFeed(
        workspace_id=data.workspace_id,
        name=data.name,
        feed_url=data.feed_url,
        account_ids=json.dumps(data.account_ids),
        caption_template=data.caption_template,
        sync_interval_hours=data.sync_interval_hours,
    )
    session.add(feed)
    session.commit()
    session.refresh(feed)

    from app.core.scheduler import schedule_rss_feed
    schedule_rss_feed(feed.id, data.sync_interval_hours)
    return feed


@rss_router.delete("/{feed_id}")
async def delete_rss_feed(
    feed_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    feed = session.get(RSSFeed, feed_id)
    if feed:
        feed.is_active = False
        session.add(feed)
        session.commit()
    return {"message": "Feed deactivated"}


# ─── Team Router ─────────────────────────────────────────────────────────────
team_router = APIRouter()


class InviteMember(BaseModel):
    workspace_id: int
    email: str
    role: str = "editor"


@team_router.get("/{workspace_id}/members")
async def list_members(
    workspace_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    members = session.exec(
        select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)
    ).all()
    result = []
    for m in members:
        user = session.get(User, m.user_id)
        if user:
            result.append({
                "id": m.id,
                "user_id": m.user_id,
                "name": user.name,
                "email": user.email,
                "role": m.role,
                "invited_at": m.invited_at,
            })
    return result


@team_router.delete("/members/{member_id}")
async def remove_member(
    member_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    member = session.get(WorkspaceMember, member_id)
    if member:
        session.delete(member)
        session.commit()
    return {"message": "Member removed"}
