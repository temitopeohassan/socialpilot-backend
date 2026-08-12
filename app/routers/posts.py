from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import json

from app.core.database import get_session
from app.core.security import get_current_user
from app.core.scheduler import schedule_post, cancel_scheduled_post
from app.models.user import User, Post, PostStatus, PostAccount, Workspace, WorkspaceMember

router = APIRouter()


class PostCreate(BaseModel):
    workspace_id: int
    caption: str
    account_ids: List[int]
    media_urls: Optional[List[str]] = None
    link_url: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    labels: Optional[List[str]] = None
    is_campaign: bool = False
    campaign_name: Optional[str] = None


class PostUpdate(BaseModel):
    caption: Optional[str] = None
    media_urls: Optional[List[str]] = None
    link_url: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    labels: Optional[List[str]] = None


def _has_workspace_access(user_id: int, workspace_id: int, session: Session) -> bool:
    return session.exec(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    ).first() is not None


@router.get("")
async def list_posts(
    workspace_id: int,
    status: Optional[PostStatus] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if not _has_workspace_access(current_user.id, workspace_id, session):
        raise HTTPException(status_code=403, detail="Access denied")

    query = select(Post).where(Post.workspace_id == workspace_id)
    if status:
        query = query.where(Post.status == status)
    query = query.order_by(Post.created_at.desc()).offset((page - 1) * per_page).limit(per_page)
    posts = session.exec(query).all()

    return {"data": posts, "page": page, "per_page": per_page}


@router.post("")
async def create_post(
    data: PostCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if not _has_workspace_access(current_user.id, data.workspace_id, session):
        raise HTTPException(status_code=403, detail="Access denied")

    status = PostStatus.SCHEDULED if data.scheduled_at else PostStatus.DRAFT

    post = Post(
        workspace_id=data.workspace_id,
        created_by=current_user.id,
        caption=data.caption,
        media_urls=json.dumps(data.media_urls or []),
        link_url=data.link_url,
        status=status,
        scheduled_at=data.scheduled_at,
        labels=json.dumps(data.labels or []),
        is_campaign=data.is_campaign,
        campaign_name=data.campaign_name,
    )
    session.add(post)
    session.commit()
    session.refresh(post)

    for account_id in data.account_ids:
        pa = PostAccount(post_id=post.id, account_id=account_id, status=status)
        session.add(pa)
    session.commit()

    if data.scheduled_at:
        schedule_post(post.id, data.scheduled_at)

    return post


@router.get("/{post_id}")
async def get_post(
    post_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if not _has_workspace_access(current_user.id, post.workspace_id, session):
        raise HTTPException(status_code=403, detail="Access denied")
    return post


@router.patch("/{post_id}")
async def update_post(
    post_id: int,
    data: PostUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if not _has_workspace_access(current_user.id, post.workspace_id, session):
        raise HTTPException(status_code=403, detail="Access denied")

    if data.caption is not None:
        post.caption = data.caption
    if data.media_urls is not None:
        post.media_urls = json.dumps(data.media_urls)
    if data.link_url is not None:
        post.link_url = data.link_url
    if data.labels is not None:
        post.labels = json.dumps(data.labels)
    if data.scheduled_at is not None:
        post.scheduled_at = data.scheduled_at
        post.status = PostStatus.SCHEDULED
        cancel_scheduled_post(post_id)
        schedule_post(post.id, data.scheduled_at)

    post.updated_at = datetime.utcnow()
    session.add(post)
    session.commit()
    session.refresh(post)
    return post


@router.delete("/{post_id}")
async def delete_post(
    post_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if not _has_workspace_access(current_user.id, post.workspace_id, session):
        raise HTTPException(status_code=403, detail="Access denied")

    cancel_scheduled_post(post_id)
    session.delete(post)
    session.commit()
    return {"message": "Post deleted"}


class BulkPostRow(BaseModel):
    caption: str
    scheduled_at: Optional[datetime] = None
    media_urls: Optional[List[str]] = None


class BulkCreateRequest(BaseModel):
    workspace_id: int
    account_ids: List[int]
    posts: List[BulkPostRow]


@router.post("/bulk")
async def bulk_create_posts(
    data: BulkCreateRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if not _has_workspace_access(current_user.id, data.workspace_id, session):
        raise HTTPException(status_code=403, detail="Access denied")

    created = []
    for row in data.posts:
        status = PostStatus.SCHEDULED if row.scheduled_at else PostStatus.DRAFT
        post = Post(
            workspace_id=data.workspace_id,
            created_by=current_user.id,
            caption=row.caption,
            media_urls=json.dumps(row.media_urls or []),
            status=status,
            scheduled_at=row.scheduled_at,
        )
        session.add(post)
        session.commit()
        session.refresh(post)
        for account_id in data.account_ids:
            session.add(PostAccount(post_id=post.id, account_id=account_id, status=status))
        if row.scheduled_at:
            schedule_post(post.id, row.scheduled_at)
        created.append(post)

    session.commit()
    return {"created": len(created), "posts": created}
