"""
Website tracker ingest service.

Handles all incoming events from the JS snippet:
  - pageview        A new page was loaded
  - duration        Exit/heartbeat — updates time-on-page
  - event           Custom or system event (click, conversion, etc.)

Also handles:
  - Site management (create, list, get snippet)
  - Analytics queries (sessions, top pages, referrers, UTM attribution,
    conversion funnel, device breakdown)
"""
import json
import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse
from collections import defaultdict

from sqlmodel import Session, select, func
from app.models.ai_models import (
    TrackerSite, PageView, TrackingEvent, TrackingSession,
)


# ─── Site management ──────────────────────────────────────────────────────────

def create_site(workspace_id: int, name: str, domain: str, session: Session) -> TrackerSite:
    """Create a new tracked site and generate its unique tracker_id."""
    tracker_id = uuid.uuid4().hex[:16]
    site = TrackerSite(
        workspace_id=workspace_id,
        name=name,
        domain=domain.lower().replace("https://", "").replace("http://", "").rstrip("/"),
        tracker_id=tracker_id,
    )
    session.add(site)
    session.commit()
    session.refresh(site)
    return site


def get_sites(workspace_id: int, session: Session):
    return session.exec(
        select(TrackerSite).where(TrackerSite.workspace_id == workspace_id)
    ).all()


def get_site(tracker_id: str, session: Session) -> Optional[TrackerSite]:
    return session.exec(
        select(TrackerSite).where(TrackerSite.tracker_id == tracker_id)
    ).first()


def get_snippet(tracker_id: str) -> str:
    """Return the embeddable JS snippet for a site."""
    return f"""<!-- SocialPilot Tracker -->
<script>
(function(w,d,s,id){{
  w._sp=w._sp||[];w._spId=id;
  var js=d.createElement(s);js.async=true;
  js.src='https://YOUR_SOCIALPILOT_DOMAIN/tracker.js';
  d.head.appendChild(js);
}})( window, document, 'script', '{tracker_id}' );
</script>"""


# ─── Event ingest ─────────────────────────────────────────────────────────────

def ingest_pageview(payload: dict, session: Session) -> PageView:
    """
    Process an incoming pageview event from the tracker snippet.
    Creates/updates the session record and stores the pageview.
    """
    tracker_id = payload.get("tid", "")
    session_id  = payload.get("sid", str(uuid.uuid4()))
    visitor_id  = payload.get("vid", _fingerprint(payload))
    url         = payload.get("url", "")
    path        = urlparse(url).path or "/"
    referrer    = payload.get("ref", "")
    referrer_domain = _extract_domain(referrer)

    # Parse UTM params from URL
    utms = _extract_utms(url)

    # User-agent parsing (lightweight)
    ua = payload.get("ua", "")
    device, browser, os_ = _parse_ua(ua)

    pv = PageView(
        tracker_id=tracker_id,
        session_id=session_id,
        visitor_id=visitor_id,
        url=url,
        path=path,
        title=payload.get("title", "")[:500],
        referrer=referrer[:500],
        referrer_domain=referrer_domain,
        utm_source=utms.get("utm_source", ""),
        utm_medium=utms.get("utm_medium", ""),
        utm_campaign=utms.get("utm_campaign", ""),
        utm_content=utms.get("utm_content", ""),
        utm_term=utms.get("utm_term", ""),
        device_type=device,
        browser=browser,
        os=os_,
        country=payload.get("country", ""),
        city=payload.get("city", ""),
        scroll_depth=int(payload.get("scroll", 0)),
    )
    session.add(pv)

    # Upsert session record
    _upsert_session(tracker_id, session_id, visitor_id, path, referrer,
                    utms, device, payload.get("country", ""), session)
    session.commit()
    session.refresh(pv)
    return pv


def ingest_duration(payload: dict, session: Session):
    """Update duration + scroll depth for an existing pageview (exit/heartbeat)."""
    sid = payload.get("sid")
    url = payload.get("url", "")
    duration = int(payload.get("duration", 0))
    scroll   = int(payload.get("scroll", 0))

    # Update most recent pageview for this session+url
    pvs = session.exec(
        select(PageView)
        .where(PageView.session_id == sid, PageView.url == url)
        .order_by(PageView.timestamp.desc())
    ).first()
    if pvs:
        pvs.duration_seconds = max(pvs.duration_seconds, duration)
        pvs.scroll_depth = max(pvs.scroll_depth, scroll)
        session.add(pvs)

    # Update session total duration
    sess = session.exec(
        select(TrackingSession).where(TrackingSession.session_id == sid)
    ).first()
    if sess:
        sess.duration_seconds = max(sess.duration_seconds, duration)
        sess.ended_at = datetime.utcnow()
        session.add(sess)

    session.commit()


def ingest_event(payload: dict, session: Session) -> TrackingEvent:
    """Process a custom or system event (conversion, click, form submit, etc.)."""
    tracker_id = payload.get("tid", "")
    session_id  = payload.get("sid", "")
    visitor_id  = payload.get("vid", "")
    event_name  = payload.get("event", "custom")
    url = payload.get("url", "")
    utms = _extract_utms(url)

    ev = TrackingEvent(
        tracker_id=tracker_id,
        session_id=session_id,
        visitor_id=visitor_id,
        event_name=event_name,
        event_category=payload.get("category", ""),
        properties=json.dumps(payload.get("props", {})),
        url=url,
        utm_campaign=utms.get("utm_campaign", ""),
    )
    session.add(ev)

    # Mark session as converted if it's a conversion event
    if event_name in ("conversion", "purchase", "signup", "subscribe"):
        sess = session.exec(
            select(TrackingSession).where(TrackingSession.session_id == session_id)
        ).first()
        if sess:
            sess.converted = True
            session.add(sess)

    session.commit()
    session.refresh(ev)
    return ev


# ─── Analytics queries ────────────────────────────────────────────────────────

def get_overview(tracker_id: str, days: int, session: Session) -> dict:
    """High-level metrics: pageviews, sessions, unique visitors, bounce rate, avg duration."""
    since = datetime.utcnow() - timedelta(days=days)

    pvs = session.exec(
        select(PageView).where(
            PageView.tracker_id == tracker_id,
            PageView.timestamp >= since,
        )
    ).all()

    sessions = session.exec(
        select(TrackingSession).where(
            TrackingSession.tracker_id == tracker_id,
            TrackingSession.started_at >= since,
        )
    ).all()

    pageviews      = len(pvs)
    unique_visitors = len(set(pv.visitor_id for pv in pvs))
    total_sessions  = len(sessions)
    bounces         = sum(1 for s in sessions if s.bounce)
    bounce_rate     = round(bounces / total_sessions * 100, 1) if total_sessions else 0
    avg_duration    = round(
        sum(s.duration_seconds for s in sessions) / total_sessions, 0
    ) if total_sessions else 0
    conversions     = sum(1 for s in sessions if s.converted)
    conversion_rate = round(conversions / total_sessions * 100, 2) if total_sessions else 0

    return {
        "pageviews": pageviews,
        "sessions": total_sessions,
        "unique_visitors": unique_visitors,
        "bounce_rate": bounce_rate,
        "avg_duration_seconds": avg_duration,
        "conversions": conversions,
        "conversion_rate": conversion_rate,
        "days": days,
    }


def get_time_series(tracker_id: str, days: int, session: Session) -> list:
    """Daily pageviews and sessions for charting."""
    since = datetime.utcnow() - timedelta(days=days)
    pvs = session.exec(
        select(PageView).where(
            PageView.tracker_id == tracker_id,
            PageView.timestamp >= since,
        )
    ).all()
    sessions_ = session.exec(
        select(TrackingSession).where(
            TrackingSession.tracker_id == tracker_id,
            TrackingSession.started_at >= since,
        )
    ).all()

    pv_by_day: dict = defaultdict(int)
    for pv in pvs:
        pv_by_day[pv.timestamp.strftime("%Y-%m-%d")] += 1

    sess_by_day: dict = defaultdict(int)
    for s in sessions_:
        sess_by_day[s.started_at.strftime("%Y-%m-%d")] += 1

    result = []
    for i in range(days):
        date = (since + timedelta(days=i + 1)).strftime("%Y-%m-%d")
        result.append({
            "date": date,
            "pageviews": pv_by_day.get(date, 0),
            "sessions": sess_by_day.get(date, 0),
        })
    return result


def get_top_pages(tracker_id: str, days: int, limit: int, session: Session) -> list:
    """Top pages by pageview count with avg duration and avg scroll depth."""
    since = datetime.utcnow() - timedelta(days=days)
    pvs = session.exec(
        select(PageView).where(
            PageView.tracker_id == tracker_id,
            PageView.timestamp >= since,
        )
    ).all()

    by_path: dict = defaultdict(lambda: {"views": 0, "duration_sum": 0, "scroll_sum": 0})
    for pv in pvs:
        key = pv.path or "/"
        by_path[key]["views"] += 1
        by_path[key]["duration_sum"] += pv.duration_seconds
        by_path[key]["scroll_sum"] += pv.scroll_depth

    result = []
    for path, d in by_path.items():
        views = d["views"]
        result.append({
            "path": path,
            "pageviews": views,
            "avg_duration_seconds": round(d["duration_sum"] / views),
            "avg_scroll_depth": round(d["scroll_sum"] / views),
        })
    result.sort(key=lambda x: x["pageviews"], reverse=True)
    return result[:limit]


def get_referrers(tracker_id: str, days: int, limit: int, session: Session) -> list:
    """Top referrer domains with visit counts."""
    since = datetime.utcnow() - timedelta(days=days)
    pvs = session.exec(
        select(PageView).where(
            PageView.tracker_id == tracker_id,
            PageView.timestamp >= since,
        )
    ).all()

    by_ref: dict = defaultdict(int)
    for pv in pvs:
        ref = pv.referrer_domain or "(direct)"
        by_ref[ref] += 1

    result = [{"referrer": k, "visits": v} for k, v in by_ref.items()]
    result.sort(key=lambda x: x["visits"], reverse=True)
    return result[:limit]


def get_utm_attribution(tracker_id: str, days: int, session: Session) -> list:
    """UTM campaign attribution — visits, sessions, and conversions per campaign."""
    since = datetime.utcnow() - timedelta(days=days)
    pvs = session.exec(
        select(PageView).where(
            PageView.tracker_id == tracker_id,
            PageView.timestamp >= since,
            PageView.utm_campaign != "",
        )
    ).all()

    sessions_ = session.exec(
        select(TrackingSession).where(
            TrackingSession.tracker_id == tracker_id,
            TrackingSession.started_at >= since,
            TrackingSession.utm_campaign != "",
        )
    ).all()

    by_campaign: dict = defaultdict(lambda: {
        "source": "", "medium": "", "visits": 0, "sessions": 0, "conversions": 0
    })

    for pv in pvs:
        k = pv.utm_campaign
        by_campaign[k]["source"]  = pv.utm_source
        by_campaign[k]["medium"]  = pv.utm_medium
        by_campaign[k]["visits"] += 1

    for s in sessions_:
        k = s.utm_campaign
        by_campaign[k]["sessions"] += 1
        if s.converted:
            by_campaign[k]["conversions"] += 1

    result = [{"campaign": k, **v} for k, v in by_campaign.items()]
    result.sort(key=lambda x: x["visits"], reverse=True)
    return result


def get_devices(tracker_id: str, days: int, session: Session) -> dict:
    """Device type, browser, and OS breakdown."""
    since = datetime.utcnow() - timedelta(days=days)
    pvs = session.exec(
        select(PageView).where(
            PageView.tracker_id == tracker_id,
            PageView.timestamp >= since,
        )
    ).all()

    devices: dict  = defaultdict(int)
    browsers: dict = defaultdict(int)
    oses: dict     = defaultdict(int)

    for pv in pvs:
        devices[pv.device_type or "unknown"] += 1
        browsers[pv.browser or "unknown"]    += 1
        oses[pv.os or "unknown"]             += 1

    def to_pct(d: dict) -> list:
        total = sum(d.values()) or 1
        return sorted(
            [{"label": k, "count": v, "pct": round(v / total * 100, 1)}
             for k, v in d.items()],
            key=lambda x: x["count"], reverse=True,
        )

    return {"devices": to_pct(devices), "browsers": to_pct(browsers), "os": to_pct(oses)}


def get_geography(tracker_id: str, days: int, session: Session) -> list:
    """Top countries by visit count."""
    since = datetime.utcnow() - timedelta(days=days)
    pvs = session.exec(
        select(PageView).where(
            PageView.tracker_id == tracker_id,
            PageView.timestamp >= since,
        )
    ).all()

    by_country: dict = defaultdict(int)
    for pv in pvs:
        by_country[pv.country or "Unknown"] += 1

    result = [{"country": k, "visits": v} for k, v in by_country.items()]
    result.sort(key=lambda x: x["visits"], reverse=True)
    return result[:20]


def get_recent_sessions(tracker_id: str, limit: int, session: Session) -> list:
    """Most recent sessions with key details."""
    rows = session.exec(
        select(TrackingSession)
        .where(TrackingSession.tracker_id == tracker_id)
        .order_by(TrackingSession.started_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "session_id": r.session_id[:8] + "…",
            "entry_page": r.entry_page,
            "referrer": r.referrer or "(direct)",
            "utm_source": r.utm_source,
            "utm_campaign": r.utm_campaign,
            "pages": r.page_count,
            "duration": r.duration_seconds,
            "device": r.device_type,
            "country": r.country,
            "converted": r.converted,
            "bounce": r.bounce,
            "started_at": r.started_at.isoformat(),
        }
        for r in rows
    ]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _upsert_session(tracker_id, session_id, visitor_id, path,
                    referrer, utms, device, country, session: Session):
    existing = session.exec(
        select(TrackingSession).where(TrackingSession.session_id == session_id)
    ).first()
    if existing:
        existing.page_count  += 1
        existing.bounce       = existing.page_count <= 1
        existing.exit_page    = path
        existing.ended_at     = datetime.utcnow()
        session.add(existing)
    else:
        session.add(TrackingSession(
            tracker_id=tracker_id,
            session_id=session_id,
            visitor_id=visitor_id,
            entry_page=path,
            exit_page=path,
            referrer=referrer[:500] if referrer else "",
            utm_source=utms.get("utm_source", ""),
            utm_campaign=utms.get("utm_campaign", ""),
            page_count=1,
            bounce=True,
            device_type=device,
            country=country,
        ))


def _extract_domain(url: str) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def _extract_utms(url: str) -> dict:
    from urllib.parse import parse_qs, urlparse
    try:
        qs = parse_qs(urlparse(url).query)
        return {k: qs[k][0] for k in ["utm_source", "utm_medium", "utm_campaign",
                                        "utm_content", "utm_term"] if k in qs}
    except Exception:
        return {}


def _fingerprint(payload: dict) -> str:
    """Create a stable visitor ID from IP + UA (no cookies)."""
    raw = f"{payload.get('ip', '')}{payload.get('ua', '')}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _parse_ua(ua: str) -> tuple:
    """Lightweight UA string parser — returns (device, browser, os)."""
    ua = ua.lower()

    if "mobile" in ua or "android" in ua and "tablet" not in ua:
        device = "mobile"
    elif "tablet" in ua or "ipad" in ua:
        device = "tablet"
    else:
        device = "desktop"

    if "edg" in ua:
        browser = "Edge"
    elif "chrome" in ua and "chromium" not in ua:
        browser = "Chrome"
    elif "firefox" in ua:
        browser = "Firefox"
    elif "safari" in ua and "chrome" not in ua:
        browser = "Safari"
    elif "opera" in ua or "opr" in ua:
        browser = "Opera"
    else:
        browser = "Other"

    if "windows" in ua:
        os_ = "Windows"
    elif "mac os" in ua:
        os_ = "macOS"
    elif "android" in ua:
        os_ = "Android"
    elif "ios" in ua or "iphone" in ua or "ipad" in ua:
        os_ = "iOS"
    elif "linux" in ua:
        os_ = "Linux"
    else:
        os_ = "Other"

    return device, browser, os_
