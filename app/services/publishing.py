"""
Publishing service: dispatches posts to social media platform APIs.
Each platform has its own publisher function. In production, plug in
real OAuth tokens and platform SDK calls.
"""
import logging
import json
from datetime import datetime
from sqlmodel import Session
from app.core.database import engine
from app.models.user import Post, PostAccount, PostStatus, SocialAccount, Platform

logger = logging.getLogger(__name__)


def publish_post(post_id: int):
    """Called by APScheduler when a post's scheduled time arrives."""
    with Session(engine) as session:
        post = session.get(Post, post_id)
        if not post or post.status != PostStatus.SCHEDULED:
            return

        post.status = PostStatus.PROCESSING
        session.add(post)
        session.commit()

        post_accounts = session.exec(
            __import__("sqlmodel", fromlist=["select"]).select(PostAccount)
            .where(PostAccount.post_id == post_id)
        ).all()

        all_ok = True
        for pa in post_accounts:
            account = session.get(SocialAccount, pa.account_id)
            if not account or not account.is_active:
                pa.status = PostStatus.FAILED
                pa.error_message = "Account not found or inactive"
                all_ok = False
                continue

            try:
                platform_post_id = _dispatch(post, account)
                pa.status = PostStatus.PUBLISHED
                pa.platform_post_id = platform_post_id
                pa.published_at = datetime.utcnow()

                # Schedule analytics snapshot jobs (1h, 6h, 24h, 7d, 30d)
                try:
                    from app.services.analytics_fetcher import schedule_analytics_jobs
                    from app.core.scheduler import scheduler as _scheduler
                    session.commit()  # flush pa.id before scheduling
                    session.refresh(pa)
                    schedule_analytics_jobs(pa.id, pa.published_at, _scheduler)
                except Exception as sched_err:
                    logger.warning("Could not schedule analytics jobs: %s", sched_err)

            except Exception as e:
                logger.error(f"Failed to publish post {post_id} to {account.platform}: {e}")
                pa.status = PostStatus.FAILED
                pa.error_message = str(e)
                all_ok = False

            session.add(pa)

        post.status = PostStatus.PUBLISHED if all_ok else PostStatus.FAILED
        post.published_at = datetime.utcnow() if all_ok else None
        session.add(post)
        session.commit()
        logger.info(f"Post {post_id} publishing complete. Success={all_ok}")


def _dispatch(post: Post, account: SocialAccount) -> str:
    """Route to the correct platform publisher."""
    media_urls = json.loads(post.media_urls or "[]")

    if account.platform == Platform.FACEBOOK:
        return _publish_facebook(account, post.caption, media_urls, post.link_url)
    elif account.platform == Platform.INSTAGRAM:
        return _publish_instagram(account, post.caption, media_urls)
    elif account.platform == Platform.TWITTER:
        return _publish_twitter(account, post.caption, media_urls)
    elif account.platform == Platform.LINKEDIN:
        return _publish_linkedin(account, post.caption, media_urls, post.link_url)
    elif account.platform == Platform.TIKTOK:
        return _publish_tiktok(account, post.caption, media_urls)
    else:
        raise ValueError(f"Unsupported platform: {account.platform}")


def _publish_facebook(account: SocialAccount, caption: str, media_urls: list, link_url: str = None) -> str:
    """
    POST https://graph.facebook.com/v19.0/{page_id}/feed
    Requires pages_manage_posts permission.
    """
    import httpx
    page_id = account.platform_user_id
    payload = {"message": caption, "access_token": account.access_token}
    if link_url:
        payload["link"] = link_url
    # For images: POST to /photos endpoint
    response = httpx.post(f"https://graph.facebook.com/v19.0/{page_id}/feed", data=payload)
    response.raise_for_status()
    return response.json().get("id", "unknown")


def _publish_instagram(account: SocialAccount, caption: str, media_urls: list) -> str:
    """
    Instagram Graph API: create media container, then publish.
    Requires instagram_basic + instagram_content_publish.
    """
    import httpx
    ig_user_id = account.platform_user_id
    token = account.access_token

    if not media_urls:
        raise ValueError("Instagram requires at least one image or video")

    image_url = media_urls[0]
    container_resp = httpx.post(
        f"https://graph.facebook.com/v19.0/{ig_user_id}/media",
        data={"image_url": image_url, "caption": caption, "access_token": token},
    )
    container_resp.raise_for_status()
    container_id = container_resp.json()["id"]

    publish_resp = httpx.post(
        f"https://graph.facebook.com/v19.0/{ig_user_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
    )
    publish_resp.raise_for_status()
    return publish_resp.json().get("id", "unknown")


def _publish_twitter(account: SocialAccount, caption: str, media_urls: list) -> str:
    """
    Twitter API v2: POST /2/tweets
    Requires OAuth 2.0 with tweet.write scope.
    """
    import httpx
    headers = {"Authorization": f"Bearer {account.access_token}", "Content-Type": "application/json"}
    payload = {"text": caption[:280]}
    response = httpx.post("https://api.twitter.com/2/tweets", json=payload, headers=headers)
    response.raise_for_status()
    return response.json().get("data", {}).get("id", "unknown")


def _publish_linkedin(account: SocialAccount, caption: str, media_urls: list, link_url: str = None) -> str:
    """
    LinkedIn API: POST /v2/ugcPosts
    Requires w_member_social scope.
    """
    import httpx
    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    payload = {
        "author": f"urn:li:person:{account.platform_user_id}",
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": caption},
                "shareMediaCategory": "NONE",
            }
        },
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
    }
    response = httpx.post("https://api.linkedin.com/v2/ugcPosts", json=payload, headers=headers)
    response.raise_for_status()
    return response.headers.get("x-restli-id", "unknown")


def _publish_tiktok(account: SocialAccount, caption: str, media_urls: list) -> str:
    """
    TikTok Content Posting API.
    Requires video.publish scope.
    """
    # TikTok requires video upload in multiple steps; simplified stub here
    raise NotImplementedError("TikTok publishing requires video upload implementation")
