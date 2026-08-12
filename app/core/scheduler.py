from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

jobstores = {
    "default": SQLAlchemyJobStore(url=settings.DATABASE_URL)
}

executors = {
    "default": ThreadPoolExecutor(10),
}

scheduler = BackgroundScheduler(
    jobstores=jobstores,
    executors=executors,
    job_defaults={"coalesce": False, "max_instances": 3},
)


def start_scheduler():
    scheduler.start()
    logger.info("Scheduler started")


def shutdown_scheduler():
    scheduler.shutdown()
    logger.info("Scheduler shutdown")


def schedule_post(post_id: int, run_date):
    from app.services.publishing import publish_post
    scheduler.add_job(
        publish_post,
        "date",
        run_date=run_date,
        args=[post_id],
        id=f"post_{post_id}",
        replace_existing=True,
    )


def cancel_scheduled_post(post_id: int):
    job_id = f"post_{post_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def schedule_rss_feed(feed_id: int, interval_hours: int = 1):
    from app.services.rss_service import sync_rss_feed
    scheduler.add_job(
        sync_rss_feed,
        "interval",
        hours=interval_hours,
        args=[feed_id],
        id=f"rss_{feed_id}",
        replace_existing=True,
    )
