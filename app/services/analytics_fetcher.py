"""
Platform analytics fetcher.

Pulls real engagement metrics from each platform's API using the OAuth
access token already stored on SocialAccount. Called by the scheduler at
multiple intervals after a post is published (1h, 6h, 24h, 7d, 30d) so we
can build an engagement curve, not just a single snapshot.

Each platform normalises its response into the same metric set:
  likes, comments, shares, saves, impressions, reach, clicks,
  video_views, profile_visits, engagement_rate

Platform-specific notes:
  Facebook/Instagram — Graph API Insights (requires business account)
  Twitter/X         — v2 Tweet metrics (requires Basic/Pro access tier)
  LinkedIn          — Share Statistics API (most permissive for organic posts)
  TikTok            — Content Posting API analytics (separate from creator API)
"""
import json
import logging
from datetime import datetime
from typing import Optional

import httpx
from sqlmodel import Session, select

from app.models.user import PostAccount, PostAnalytics, SocialAccount, Platform, PostStatus
from app.services.posting_time import reinforce

logger = logging.getLogger(__name__)

# Snapshot schedule in hours-after-publish
SNAPSHOT_SCHEDULE = {
    "1h": 1,
    "6h": 6,
    "24h": 24,
    "7d": 168,
    "30d": 720,
}


def fetch_and_store(post_account_id: int, snapshot_label: str, session: Session) -> Optional[PostAnalytics]:
    """
    Main entry point. Fetches metrics for one PostAccount at the given
    snapshot interval, stores a PostAnalytics row, and reinforces the
    best-time predictor with the normalised engagement rate.
    """
    pa = session.get(PostAccount, post_account_id)
    if not pa or pa.status != PostStatus.PUBLISHED:
        return None
    if not pa.platform_post_id:
        logger.warning("PostAccount %s has no platform_post_id — skipping analytics fetch", post_account_id)
        return None

    account = session.get(SocialAccount, pa.account_id)
    if not account or not account.is_active:
        return None

    platform = account.platform.value if hasattr(account.platform, "value") else account.platform

    try:
        metrics = _fetch(platform, account, pa.platform_post_id)
    except Exception as exc:
        logger.error("Analytics fetch failed for PostAccount %s (%s): %s", post_account_id, platform, exc)
        return None

    engagement_rate = _compute_engagement_rate(metrics)
    metrics["engagement_rate"] = engagement_rate

    row = PostAnalytics(
        post_account_id=post_account_id,
        post_id=pa.post_id,
        account_id=pa.account_id,
        platform=platform,
        snapshot_label=snapshot_label,
        published_at=pa.published_at,
        raw_response=json.dumps(metrics.get("_raw", {})),
        **{k: metrics.get(k, 0) for k in [
            "likes", "comments", "shares", "saves",
            "impressions", "reach", "clicks", "video_views", "profile_visits",
        ]},
        engagement_rate=engagement_rate,
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    # Feed real engagement back into the best-time predictor
    if pa.published_at and snapshot_label == "24h":
        normalised = min(engagement_rate * 10, 1.0)  # scale 0-10% ER -> 0-1
        reinforce(account.id, pa.published_at, normalised, session)

    return row


def _fetch(platform: str, account: SocialAccount, platform_post_id: str) -> dict:
    """Route to the right platform fetcher."""
    fetchers = {
        "facebook":  _fetch_facebook,
        "instagram": _fetch_instagram,
        "twitter":   _fetch_twitter,
        "linkedin":  _fetch_linkedin,
        "tiktok":    _fetch_tiktok,
    }
    fetcher = fetchers.get(platform)
    if not fetcher:
        raise ValueError(f"No analytics fetcher for platform: {platform}")
    return fetcher(account, platform_post_id)


# ─── Facebook ─────────────────────────────────────────────────────────────────

def _fetch_facebook(account: SocialAccount, post_id: str) -> dict:
    """
    GET /v19.0/{post_id}/insights
    Metrics: post_impressions, post_reach, post_engaged_users,
             post_clicks, post_reactions_by_type_total
    Requires: pages_read_engagement permission
    """
    token = account.access_token
    metrics = "post_impressions,post_reach,post_engaged_users,post_clicks,post_reactions_by_type_total"
    resp = httpx.get(
        f"https://graph.facebook.com/v19.0/{post_id}/insights",
        params={"metric": metrics, "access_token": token},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    raw = {item["name"]: item.get("values", [{}])[0].get("value", 0)
           for item in data.get("data", [])}

    reactions = raw.get("post_reactions_by_type_total", {})
    likes = sum(reactions.values()) if isinstance(reactions, dict) else 0

    # Comments and shares come from the post object itself
    post_resp = httpx.get(
        f"https://graph.facebook.com/v19.0/{post_id}",
        params={"fields": "comments.summary(true),shares", "access_token": token},
        timeout=15,
    )
    post_data = post_resp.json() if post_resp.status_code == 200 else {}

    return {
        "likes": likes,
        "comments": post_data.get("comments", {}).get("summary", {}).get("total_count", 0),
        "shares": post_data.get("shares", {}).get("count", 0),
        "impressions": raw.get("post_impressions", 0),
        "reach": raw.get("post_reach", 0),
        "clicks": raw.get("post_clicks", 0),
        "_raw": raw,
    }


# ─── Instagram ────────────────────────────────────────────────────────────────

def _fetch_instagram(account: SocialAccount, media_id: str) -> dict:
    """
    GET /v19.0/{media_id}/insights
    Metrics: impressions, reach, likes, comments, shares, saved, profile_visits
    Requires: instagram_manage_insights permission + business/creator account
    """
    token = account.access_token
    metrics = "impressions,reach,likes,comments,shares,saved,profile_visits"
    resp = httpx.get(
        f"https://graph.facebook.com/v19.0/{media_id}/insights",
        params={"metric": metrics, "access_token": token},
        timeout=15,
    )
    resp.raise_for_status()
    raw = {item["name"]: item.get("values", [{}])[0].get("value", 0)
           for item in resp.json().get("data", [])}

    return {
        "impressions": raw.get("impressions", 0),
        "reach": raw.get("reach", 0),
        "likes": raw.get("likes", 0),
        "comments": raw.get("comments", 0),
        "shares": raw.get("shares", 0),
        "saves": raw.get("saved", 0),
        "profile_visits": raw.get("profile_visits", 0),
        "_raw": raw,
    }


# ─── Twitter / X ──────────────────────────────────────────────────────────────

def _fetch_twitter(account: SocialAccount, tweet_id: str) -> dict:
    """
    GET /2/tweets/{id}?tweet.fields=public_metrics,non_public_metrics
    public_metrics: like_count, reply_count, retweet_count, quote_count, impression_count
    non_public_metrics: url_link_clicks, user_profile_clicks (requires elevated access)
    """
    token = account.access_token
    headers = {"Authorization": f"Bearer {token}"}

    resp = httpx.get(
        f"https://api.twitter.com/2/tweets/{tweet_id}",
        params={"tweet.fields": "public_metrics,non_public_metrics,organic_metrics"},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})

    pub = data.get("public_metrics", {})
    non_pub = data.get("non_public_metrics", {})

    return {
        "likes": pub.get("like_count", 0),
        "comments": pub.get("reply_count", 0),
        "shares": pub.get("retweet_count", 0) + pub.get("quote_count", 0),
        "impressions": pub.get("impression_count", 0),
        "clicks": non_pub.get("url_link_clicks", 0),
        "profile_visits": non_pub.get("user_profile_clicks", 0),
        "_raw": data,
    }


# ─── LinkedIn ─────────────────────────────────────────────────────────────────

def _fetch_linkedin(account: SocialAccount, share_urn: str) -> dict:
    """
    GET /v2/organizationalEntityShareStatistics or /v2/shareStatistics
    Metrics: impressionCount, uniqueImpressionsCount, clickCount,
             likeCount, commentCount, shareCount
    Requires: r_organization_social or r_member_social scope
    """
    token = account.access_token
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
    }

    # Try org stats first (page), fall back to member stats
    resp = httpx.get(
        "https://api.linkedin.com/v2/shareStatistics",
        params={"q": "shares", "shares[0]": share_urn},
        headers=headers,
        timeout=15,
    )

    if resp.status_code != 200:
        return {"likes": 0, "comments": 0, "shares": 0, "impressions": 0, "clicks": 0}

    element = resp.json().get("elements", [{}])[0]
    stats = element.get("totalShareStatistics", {})

    return {
        "impressions": stats.get("impressionCount", 0),
        "reach": stats.get("uniqueImpressionsCount", 0),
        "clicks": stats.get("clickCount", 0),
        "likes": stats.get("likeCount", 0),
        "comments": stats.get("commentCount", 0),
        "shares": stats.get("shareCount", 0),
        "_raw": stats,
    }


# ─── TikTok ───────────────────────────────────────────────────────────────────

def _fetch_tiktok(account: SocialAccount, video_id: str) -> dict:
    """
    POST /v2/video/query/
    Metrics: view_count, like_count, comment_count, share_count,
             reach, video_views, profile_visit
    Requires: video.list scope (TikTok for Business API)
    """
    token = account.access_token
    resp = httpx.post(
        "https://open.tiktokapis.com/v2/video/query/",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "filters": {"video_ids": [video_id]},
            "fields": ["view_count", "like_count", "comment_count", "share_count",
                       "reach", "profile_visit_count"],
        },
        timeout=15,
    )
    resp.raise_for_status()
    videos = resp.json().get("data", {}).get("videos", [{}])
    v = videos[0] if videos else {}

    return {
        "video_views": v.get("view_count", 0),
        "likes": v.get("like_count", 0),
        "comments": v.get("comment_count", 0),
        "shares": v.get("share_count", 0),
        "reach": v.get("reach", 0),
        "profile_visits": v.get("profile_visit_count", 0),
        "_raw": v,
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _compute_engagement_rate(metrics: dict) -> float:
    """(likes + comments + shares + saves) / reach, capped at 100%."""
    engagements = (metrics.get("likes", 0) + metrics.get("comments", 0)
                   + metrics.get("shares", 0) + metrics.get("saves", 0))
    reach = metrics.get("reach", 0) or metrics.get("impressions", 0)
    if not reach:
        return 0.0
    return round(min(engagements / reach, 1.0), 4)


def schedule_analytics_jobs(post_account_id: int, published_at: datetime, scheduler):
    """
    Register APScheduler jobs for each snapshot interval after a post is
    published. Called from publishing.py right after a successful publish.
    """
    from app.core.database import engine
    from datetime import timedelta

    for label, hours in SNAPSHOT_SCHEDULE.items():
        run_at = published_at + timedelta(hours=hours)
        if run_at <= datetime.utcnow():
            continue  # already past (e.g. backdated post)
        scheduler.add_job(
            _run_fetch_job,
            trigger="date",
            run_date=run_at,
            args=[post_account_id, label],
            id=f"analytics_{post_account_id}_{label}",
            replace_existing=True,
        )


def _run_fetch_job(post_account_id: int, snapshot_label: str):
    """Scheduler entry point (runs in background thread)."""
    from app.core.database import engine
    with Session(engine) as session:
        fetch_and_store(post_account_id, snapshot_label, session)
