from sqlmodel import SQLModel, Field, Relationship
from typing import Optional, List
from datetime import datetime
from enum import Enum


# ─── Enums ────────────────────────────────────────────────────────────────────

class UserRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class Platform(str, Enum):
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    TWITTER = "twitter"
    LINKEDIN = "linkedin"
    TIKTOK = "tiktok"


class PostStatus(str, Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    PROCESSING = "processing"
    PUBLISHED = "published"
    FAILED = "failed"


class PlanTier(str, Enum):
    STARTER = "starter"
    GROWTH = "growth"
    AGENCY = "agency"


# ─── User ────────────────────────────────────────────────────────────────────

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    name: str
    hashed_password: str
    avatar_url: Optional[str] = None
    plan: PlanTier = PlanTier.STARTER
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    workspaces: List["WorkspaceMember"] = Relationship(back_populates="user")


# ─── Workspace ───────────────────────────────────────────────────────────────

class Workspace(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    slug: str = Field(unique=True, index=True)
    logo_url: Optional[str] = None
    owner_id: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    members: List["WorkspaceMember"] = Relationship(back_populates="workspace")
    accounts: List["SocialAccount"] = Relationship(back_populates="workspace")
    posts: List["Post"] = Relationship(back_populates="workspace")


class WorkspaceMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspace.id")
    user_id: int = Field(foreign_key="user.id")
    role: UserRole = UserRole.EDITOR
    invited_at: datetime = Field(default_factory=datetime.utcnow)

    workspace: Optional[Workspace] = Relationship(back_populates="members")
    user: Optional[User] = Relationship(back_populates="workspaces")


# ─── Social Account ──────────────────────────────────────────────────────────

class SocialAccount(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspace.id")
    platform: Platform
    platform_user_id: str
    username: str
    display_name: str
    avatar_url: Optional[str] = None
    access_token: str
    refresh_token: Optional[str] = None
    token_expires_at: Optional[datetime] = None
    is_active: bool = True
    follower_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)

    workspace: Optional[Workspace] = Relationship(back_populates="accounts")
    posts: List["PostAccount"] = Relationship(back_populates="account")


# ─── Post ─────────────────────────────────────────────────────────────────────

class Post(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspace.id")
    created_by: int = Field(foreign_key="user.id")
    caption: str
    media_urls: Optional[str] = None  # JSON array of URLs
    link_url: Optional[str] = None
    status: PostStatus = PostStatus.DRAFT
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    labels: Optional[str] = None  # JSON array
    is_campaign: bool = False
    campaign_name: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    workspace: Optional[Workspace] = Relationship(back_populates="posts")
    accounts: List["PostAccount"] = Relationship(back_populates="post")


class PostAccount(SQLModel, table=True):
    """Links a post to specific social accounts for publishing"""
    id: Optional[int] = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="post.id")
    account_id: int = Field(foreign_key="socialaccount.id")
    status: PostStatus = PostStatus.SCHEDULED
    platform_post_id: Optional[str] = None
    error_message: Optional[str] = None
    published_at: Optional[datetime] = None

    post: Optional[Post] = Relationship(back_populates="accounts")
    account: Optional[SocialAccount] = Relationship(back_populates="posts")


# ─── Media ───────────────────────────────────────────────────────────────────

class MediaFile(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspace.id")
    uploaded_by: int = Field(foreign_key="user.id")
    filename: str
    original_name: str
    file_type: str  # image/video
    mime_type: str
    size_bytes: int
    url: str
    thumbnail_url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration_seconds: Optional[float] = None
    alt_text: Optional[str] = None
    tags: Optional[str] = None  # JSON array
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ─── RSS Feed ────────────────────────────────────────────────────────────────

class RSSFeed(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspace.id")
    name: str
    feed_url: str
    account_ids: str  # JSON array of social account IDs to post to
    caption_template: Optional[str] = None  # Use {title}, {url}, {description}
    is_active: bool = True
    sync_interval_hours: int = 1
    last_synced_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ─── Analytics ───────────────────────────────────────────────────────────────

class PostAnalytics(SQLModel, table=True):
    """
    Timestamped engagement snapshot for a published post on one account.
    Multiple snapshots captured over time (1h, 6h, 24h, 7d) so curves can
    be plotted and the best-time predictor can be reinforced with real data.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    post_account_id: int = Field(foreign_key="postaccount.id", index=True)
    post_id: int = Field(foreign_key="post.id", index=True)
    account_id: int = Field(foreign_key="socialaccount.id", index=True)
    platform: str = ""

    # Core metrics (normalised across platforms)
    likes: int = 0
    comments: int = 0
    shares: int = 0          # retweets / reposts / reshares
    saves: int = 0           # bookmarks (Instagram / LinkedIn)
    impressions: int = 0
    reach: int = 0
    clicks: int = 0
    video_views: int = 0
    profile_visits: int = 0

    # Derived
    engagement_rate: float = 0.0   # (likes+comments+shares) / reach
    snapshot_label: str = "1h"     # 1h | 6h | 24h | 7d | 30d
    raw_response: Optional[str] = None  # full platform API JSON

    fetched_at: datetime = Field(default_factory=datetime.utcnow)
    published_at: Optional[datetime] = None


# ─── Queue ────────────────────────────────────────────────────────────────────

class QueueSlot(SQLModel, table=True):
    """Defines recurring time slots for an account's content queue"""
    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="socialaccount.id")
    day_of_week: int  # 0=Monday ... 6=Sunday
    time_of_day: str  # "HH:MM" in UTC
    is_active: bool = True
