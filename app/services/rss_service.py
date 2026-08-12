"""RSS Feed sync service - parses feeds and creates posts."""
import feedparser
import json
import logging
from datetime import datetime
from sqlmodel import Session, select
from app.core.database import engine
from app.models.user import RSSFeed, Post, PostAccount, PostStatus

logger = logging.getLogger(__name__)


def sync_rss_feed(feed_id: int):
    with Session(engine) as session:
        feed = session.get(RSSFeed, feed_id)
        if not feed or not feed.is_active:
            return

        parsed = feedparser.parse(feed.feed_url)
        account_ids = json.loads(feed.account_ids or "[]")
        template = feed.caption_template or "{title}\n\n{url}"

        created = 0
        for entry in parsed.entries[:5]:  # max 5 per sync
            title = entry.get("title", "")
            url = entry.get("link", "")
            description = entry.get("summary", "")

            caption = template.replace("{title}", title).replace("{url}", url).replace("{description}", description[:200])

            post = Post(
                workspace_id=feed.workspace_id,
                created_by=0,  # system
                caption=caption,
                link_url=url,
                status=PostStatus.DRAFT,
            )
            session.add(post)
            session.commit()
            session.refresh(post)

            for account_id in account_ids:
                session.add(PostAccount(post_id=post.id, account_id=account_id, status=PostStatus.DRAFT))
            created += 1

        session.commit()
        feed.last_synced_at = datetime.utcnow()
        session.add(feed)
        session.commit()
        logger.info(f"RSS feed {feed_id} synced: {created} posts created")
