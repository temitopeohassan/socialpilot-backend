"""
Best-time-to-post prediction.

Strategy: start from research-backed platform priors, then refine per account
from its own PostAnalytics as data accumulates (a simple online update). The
predictor exposes:

  - best_slots(account, n)         top N (weekday, hour) slots
  - score_slot(account, dt)        engagement score for a specific datetime
  - distribute(account, n, window) spread N posts across the best slots in a
                                   window without clustering them

This is deliberately a transparent heuristic model rather than a black box —
users can see WHY a time was chosen, which matters for trust.
"""
import json
from datetime import datetime, timedelta
from typing import List, Tuple
from sqlmodel import Session, select
from app.models.ai_models import PostingHeuristic

# Platform priors: (weekday, hour) -> relative engagement weight.
# Weekday: 0=Mon..6=Sun. Condensed from widely-cited industry benchmarks;
# these are starting points that get overwritten by real data per account.
PLATFORM_PRIORS = {
    "linkedin": [(1, 8), (1, 10), (2, 9), (2, 11), (3, 8), (3, 10), (4, 9)],
    "instagram": [(0, 11), (1, 13), (2, 11), (3, 14), (4, 10), (5, 10), (6, 19)],
    "facebook": [(2, 9), (3, 13), (4, 11), (5, 13), (6, 12), (0, 15)],
    "twitter": [(1, 9), (2, 12), (3, 9), (3, 17), (4, 8), (0, 10)],
    "tiktok": [(1, 18), (2, 19), (3, 21), (4, 20), (5, 11), (6, 16)],
}


def seed_heuristics(account_id: int, platform: str, session: Session):
    """Seed an account with platform priors if it has none yet."""
    existing = session.exec(
        select(PostingHeuristic).where(PostingHeuristic.account_id == account_id)
    ).first()
    if existing:
        return

    priors = PLATFORM_PRIORS.get(platform, PLATFORM_PRIORS["instagram"])
    # Highest-ranked prior gets the top score, decaying down the list.
    for rank, (dow, hour) in enumerate(priors):
        score = round(1.0 - (rank / max(len(priors), 1)) * 0.5, 3)  # 1.0..0.5
        session.add(PostingHeuristic(
            account_id=account_id, platform=platform,
            day_of_week=dow, hour=hour,
            engagement_score=score, sample_size=0,
        ))
    session.commit()


def best_slots(account_id: int, session: Session, n: int = 7) -> List[dict]:
    """Top N posting slots for an account, highest engagement first."""
    rows = session.exec(
        select(PostingHeuristic)
        .where(PostingHeuristic.account_id == account_id)
        .order_by(PostingHeuristic.engagement_score.desc())
    ).all()
    return [
        {"day_of_week": r.day_of_week, "hour": r.hour,
         "score": r.engagement_score, "confidence": _confidence(r.sample_size)}
        for r in rows[:n]
    ]


def _confidence(sample_size: int) -> str:
    if sample_size >= 30:
        return "high"
    if sample_size >= 8:
        return "medium"
    return "low"  # still using priors


def score_slot(account_id: int, when: datetime, session: Session) -> float:
    """Engagement score (0..1) for a specific datetime, nearest-hour match."""
    rows = session.exec(
        select(PostingHeuristic).where(
            PostingHeuristic.account_id == account_id,
            PostingHeuristic.day_of_week == when.weekday(),
        )
    ).all()
    if not rows:
        return 0.5
    # closest hour wins
    best = min(rows, key=lambda r: abs(r.hour - when.hour))
    hour_gap = abs(best.hour - when.hour)
    penalty = min(hour_gap * 0.08, 0.4)
    return round(max(best.engagement_score - penalty, 0.1), 3)


def distribute(
    account_id: int, count: int, window_start: datetime,
    window_end: datetime, session: Session,
) -> List[datetime]:
    """Place `count` posts across the best slots within [start, end] without
    clustering. Returns concrete datetimes, soonest first."""
    slots = best_slots(account_id, session, n=14)
    if not slots:
        slots = [{"day_of_week": d, "hour": 10} for d in range(7)]

    chosen: List[datetime] = []
    cursor = window_start
    slot_idx = 0
    # walk forward day by day, dropping posts on the best remaining slot
    guard = 0
    while len(chosen) < count and cursor <= window_end and guard < 400:
        guard += 1
        slot = slots[slot_idx % len(slots)]
        # find next date matching this slot's weekday at/after cursor
        days_ahead = (slot["day_of_week"] - cursor.weekday()) % 7
        candidate = (cursor + timedelta(days=days_ahead)).replace(
            hour=slot["hour"], minute=0, second=0, microsecond=0
        )
        if candidate < window_start:
            candidate += timedelta(days=7)
        if window_start <= candidate <= window_end and candidate not in chosen:
            chosen.append(candidate)
            cursor = candidate + timedelta(hours=18)  # avoid same-day cluster
        slot_idx += 1
        if slot_idx % len(slots) == 0:
            cursor += timedelta(days=1)

    chosen.sort()
    return chosen[:count]


def reinforce(account_id: int, when: datetime, engagement: float, session: Session):
    """Online update: after a post's real engagement is known, nudge the
    matching slot's score toward observed performance (exponential moving
    average). This is how priors get replaced by truth over time."""
    row = session.exec(
        select(PostingHeuristic).where(
            PostingHeuristic.account_id == account_id,
            PostingHeuristic.day_of_week == when.weekday(),
            PostingHeuristic.hour == when.hour,
        )
    ).first()
    if not row:
        row = PostingHeuristic(
            account_id=account_id, platform="", day_of_week=when.weekday(),
            hour=when.hour, engagement_score=0.5, sample_size=0,
        )
    alpha = 0.3  # learning rate
    row.engagement_score = round(
        (1 - alpha) * row.engagement_score + alpha * max(0.0, min(1.0, engagement)), 3
    )
    row.sample_size += 1
    row.updated_at = datetime.utcnow()
    session.add(row)
    session.commit()
