"""
AI-first domain models.

These extend the base models (user.py) with the concepts that make the
platform agentic rather than a manual composer with an AI button:

  - BrandVoice         persistent voice/tone memory applied to every generation
  - Campaign           a multi-post plan generated from a single prompt
  - CampaignBrief      the original intent + parsed planning parameters
  - PostVariation      per-platform adaptation of one core idea
  - ApprovalRequest    AI-assisted review/approval workflow state
  - PostingHeuristic   learned best-time-to-post signal per account/platform
  - AgentRun           an audit trail of what the agent did for a given prompt
"""
from sqlmodel import SQLModel, Field, Relationship
from typing import Optional, List
from datetime import datetime, time
from enum import Enum


# ─── Enums ────────────────────────────────────────────────────────────────────

class CampaignStatus(str, Enum):
    PLANNING = "planning"        # agent is still drafting
    DRAFT = "draft"              # plan ready, awaiting review
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    ACTIVE = "active"            # some posts published, some pending
    COMPLETED = "completed"
    ARCHIVED = "archived"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"
    AUTO_APPROVED = "auto_approved"  # passed AI policy checks, no human needed


class VariationStatus(str, Enum):
    GENERATED = "generated"
    EDITED = "edited"            # human touched it after generation
    APPROVED = "approved"
    REJECTED = "rejected"


# ─── Brand Voice (memory) ─────────────────────────────────────────────────────

class BrandVoice(SQLModel, table=True):
    """
    Persistent brand-voice memory for a workspace. Injected into every AI
    generation so output stays on-brand without re-specifying tone each time.
    Built up from explicit settings AND learned from approved/edited posts.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspace.id", index=True)
    name: str = "Default"
    is_active: bool = True

    # Explicit voice definition
    description: str = ""                 # "Warm, witty, expert-but-approachable"
    tone_attributes: str = "[]"           # JSON array: ["friendly","confident"]
    target_audience: str = ""             # "Early-career devs in fintech"
    writing_sample: str = ""              # a paragraph the brand likes

    # Hard rules the AI must respect
    do_rules: str = "[]"                  # JSON array of "always" rules
    dont_rules: str = "[]"                # JSON array of "never" rules
    banned_words: str = "[]"              # JSON array
    preferred_emoji_level: str = "some"   # none | some | heavy
    preferred_hashtag_count: int = 5

    # Learned signal (updated as users approve/edit AI output)
    learned_summary: str = ""             # AI-maintained rolling summary
    sample_count: int = 0                 # how many approved posts informed it

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ─── Campaign (plan from a single prompt) ─────────────────────────────────────

class Campaign(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspace.id", index=True)
    created_by: int = Field(foreign_key="user.id")
    name: str
    goal: str = ""                        # parsed objective
    status: CampaignStatus = CampaignStatus.PLANNING
    brand_voice_id: Optional[int] = Field(default=None, foreign_key="brandvoice.id")

    # Planning window
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None

    # The agent's plan rationale (shown to the user for transparency)
    strategy_summary: str = ""
    target_platforms: str = "[]"          # JSON array of platforms
    post_count: int = 0

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CampaignBrief(SQLModel, table=True):
    """The original natural-language prompt plus the structured params the
    agent parsed out of it. Kept so plans can be regenerated/explained."""
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    raw_prompt: str                       # exactly what the user typed
    parsed_params: str = "{}"             # JSON: platforms, count, themes, dates
    clarifications: str = "[]"            # JSON: Q&A if agent had to ask
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ─── Post variations (platform-specific) ──────────────────────────────────────

class CoreIdea(SQLModel, table=True):
    """One conceptual post within a campaign. Spawns N platform variations."""
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    sequence: int = 0                     # order within the campaign
    concept: str                          # the core message/angle
    suggested_date: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PostVariation(SQLModel, table=True):
    """A platform-tailored rendering of a CoreIdea. This is what eventually
    becomes a Post once approved + scheduled."""
    id: Optional[int] = Field(default=None, primary_key=True)
    core_idea_id: int = Field(foreign_key="coreidea.id", index=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    platform: str                         # facebook|instagram|twitter|linkedin|tiktok
    account_id: Optional[int] = Field(default=None, foreign_key="socialaccount.id")

    caption: str
    hashtags: str = "[]"                  # JSON array
    media_suggestion: str = ""            # AI description of ideal media
    char_count: int = 0
    status: VariationStatus = VariationStatus.GENERATED

    # Scheduling (filled by best-time predictor)
    scheduled_at: Optional[datetime] = None
    predicted_engagement_score: float = 0.0

    # Link to the real Post once it's committed to the publishing pipeline
    post_id: Optional[int] = Field(default=None, foreign_key="post.id")

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ─── Approval workflow ────────────────────────────────────────────────────────

class ApprovalRequest(SQLModel, table=True):
    """AI-assisted approval. The agent runs policy/brand/quality checks and
    either auto-approves or routes to a human with its findings attached."""
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspace.id", index=True)
    campaign_id: Optional[int] = Field(default=None, foreign_key="campaign.id")
    variation_id: Optional[int] = Field(default=None, foreign_key="postvariation.id")

    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_by: int = Field(foreign_key="user.id")
    assigned_to: Optional[int] = Field(default=None, foreign_key="user.id")
    decided_by: Optional[int] = Field(default=None, foreign_key="user.id")

    # AI pre-screen results (the agent's "opinion" before a human looks)
    ai_risk_score: float = 0.0            # 0=safe ... 1=needs attention
    ai_findings: str = "[]"               # JSON array of flagged issues
    ai_recommendation: str = ""           # approve | review | reject
    brand_alignment_score: float = 0.0    # how on-voice it is

    human_comment: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    decided_at: Optional[datetime] = None


# ─── Best-time-to-post intelligence ───────────────────────────────────────────

class PostingHeuristic(SQLModel, table=True):
    """Learned engagement signal per account + weekday + hour bucket.
    Seeded with platform priors, refined from this account's own analytics."""
    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="socialaccount.id", index=True)
    platform: str
    day_of_week: int                      # 0=Mon ... 6=Sun
    hour: int                             # 0-23 (account timezone)
    engagement_score: float = 0.0         # normalised 0..1
    sample_size: int = 0                  # how much data backs this
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ─── Agent audit trail ────────────────────────────────────────────────────────

class AgentRun(SQLModel, table=True):
    """Every natural-language request creates an AgentRun so the user can see
    what the agent understood, planned, and executed — and undo it."""
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspace.id", index=True)
    user_id: int = Field(foreign_key="user.id")
    prompt: str
    intent: str = ""                      # classified: plan_campaign|schedule|edit|ask
    plan: str = "{}"                       # JSON of what it decided to do
    actions_taken: str = "[]"             # JSON array of executed actions
    result_summary: str = ""
    campaign_id: Optional[int] = Field(default=None, foreign_key="campaign.id")
    success: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ─── Post Intelligence ────────────────────────────────────────────────────────

class PostIntelligenceReport(SQLModel, table=True):
    """
    Stored result of a Post Intelligence analysis. Subscribers can analyze
    any post text (their own or a competitor's) and get a structured report.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: Optional[int] = Field(default=None, foreign_key="workspace.id")
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")

    # Input
    caption: str                          # the analyzed text
    platform_hint: str = ""              # optional: which platform it's from
    niche_hint: str = ""                 # optional: industry/niche context
    image_provided: bool = False

    # Report (stored as JSON strings for flexibility)
    primary_intent: str = ""             # e.g. "drive_engagement"
    secondary_intent: str = ""
    hook_score: float = 0.0              # 0-10
    hook_type: str = ""                  # curiosity | number | claim | question | story
    hook_analysis: str = ""
    psychological_triggers: str = "[]"  # JSON array
    structure_analysis: str = "{}"      # JSON: hook/body/cta breakdown
    platform_fit: str = "{}"            # JSON: {platform: score}
    readability: str = "{}"             # JSON: grade, tone, formality
    predicted_engagement_type: str = "" # comments | shares | saves | likes
    brand_voice_fit: Optional[float] = None  # only if voice configured
    improvements: str = "[]"            # JSON array of specific suggestions
    overall_score: float = 0.0          # 0-10 composite
    summary: str = ""                   # 2-3 sentence executive summary

    # Compare mode
    is_comparison: bool = False
    comparison_caption: str = ""
    comparison_report: str = "{}"       # JSON report for caption B

    created_at: datetime = Field(default_factory=datetime.utcnow)


# ─── Website Tracking ─────────────────────────────────────────────────────────

class TrackerSite(SQLModel, table=True):
    """
    A website connected to SocialPilot tracking. One site per domain.
    Generates a unique tracker_id used in the JS snippet.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspace.id", index=True)
    name: str                          # human label e.g. "My Company Website"
    domain: str                        # e.g. "mycompany.com"
    tracker_id: str = Field(index=True, unique=True)  # UUID used in snippet
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PageView(SQLModel, table=True):
    """
    A single pageview event captured by the tracker snippet.
    One row per page visit.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    tracker_id: str = Field(index=True)     # links to TrackerSite
    session_id: str = Field(index=True)     # anonymous session UUID
    visitor_id: str = Field(index=True)     # fingerprint-based, stable across sessions

    # Page info
    url: str
    path: str                          # just the path portion
    title: str = ""
    referrer: str = ""
    referrer_domain: str = ""

    # UTM attribution (links social posts to website visits)
    utm_source: str = ""               # e.g. "linkedin"
    utm_medium: str = ""               # e.g. "social"
    utm_campaign: str = ""             # e.g. "summer_launch"
    utm_content: str = ""
    utm_term: str = ""

    # Device / browser
    browser: str = ""
    os: str = ""
    device_type: str = ""              # desktop | mobile | tablet
    country: str = ""
    city: str = ""

    # Timing
    duration_seconds: int = 0          # updated by exit/heartbeat event
    scroll_depth: int = 0              # 0–100 %
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)


class TrackingEvent(SQLModel, table=True):
    """
    A custom event fired by the tracker (click, form_submit, conversion, etc.)
    or a system event (session_start, session_end, outbound_click).
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    tracker_id: str = Field(index=True)
    session_id: str = Field(index=True)
    visitor_id: str = Field(index=True)
    event_name: str                    # e.g. "button_click", "form_submit", "conversion"
    event_category: str = ""           # e.g. "engagement", "conversion"
    properties: str = "{}"             # JSON blob of arbitrary event props
    url: str = ""
    utm_campaign: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)


class TrackingSession(SQLModel, table=True):
    """
    Aggregated session record built from pageviews.
    One row per unique session_id — updated as events arrive.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    tracker_id: str = Field(index=True)
    session_id: str = Field(index=True, unique=True)
    visitor_id: str = Field(index=True)

    # Session-level attribution
    entry_page: str = ""
    exit_page: str = ""
    referrer: str = ""
    utm_source: str = ""
    utm_campaign: str = ""

    page_count: int = 0
    duration_seconds: int = 0
    bounce: bool = True                # true if only one page viewed
    converted: bool = False            # true if a conversion event fired

    device_type: str = ""
    country: str = ""
    started_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    ended_at: Optional[datetime] = None
