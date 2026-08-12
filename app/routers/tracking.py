"""
Website tracking API.

Public ingest endpoints (no auth — called by the tracker snippet):
  POST /track/pageview    incoming pageview
  POST /track/duration    exit/heartbeat with time-on-page
  POST /track/event       custom event

GET  /tracker.js          serves the tracker script with the correct ingest URL

Authenticated dashboard endpoints:
  POST /tracking/sites           create a new tracked site
  GET  /tracking/sites           list sites for a workspace
  GET  /tracking/sites/{id}/snippet  get the embed snippet
  GET  /tracking/sites/{id}/overview  high-level metrics
  GET  /tracking/sites/{id}/timeseries
  GET  /tracking/sites/{id}/pages
  GET  /tracking/sites/{id}/referrers
  GET  /tracking/sites/{id}/utm
  GET  /tracking/sites/{id}/devices
  GET  /tracking/sites/{id}/geography
  GET  /tracking/sites/{id}/sessions
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from sqlmodel import Session
from pydantic import BaseModel
from typing import Optional
import os

from app.core.database import get_session
from app.core.security import get_current_user
from app.models.user import User
from app.services import tracker as tracker_svc

# ── Public ingest router (no auth, CORS open) ─────────────────────────────────
ingest_router = APIRouter()

@ingest_router.post("/pageview")
async def track_pageview(request: Request, session: Session = Depends(get_session)):
    try:
        payload = await request.json()
        payload["ip"] = request.client.host if request.client else ""
        tracker_svc.ingest_pageview(payload, session)
    except Exception:
        pass  # never return errors to the tracker
    return Response(content='{"ok":1}', media_type="application/json",
                    headers={"Access-Control-Allow-Origin": "*"})


@ingest_router.post("/duration")
async def track_duration(request: Request, session: Session = Depends(get_session)):
    try:
        payload = await request.json()
        tracker_svc.ingest_duration(payload, session)
    except Exception:
        pass
    return Response(content='{"ok":1}', media_type="application/json",
                    headers={"Access-Control-Allow-Origin": "*"})


@ingest_router.post("/event")
async def track_event(request: Request, session: Session = Depends(get_session)):
    try:
        payload = await request.json()
        tracker_svc.ingest_event(payload, session)
    except Exception:
        pass
    return Response(content='{"ok":1}', media_type="application/json",
                    headers={"Access-Control-Allow-Origin": "*"})


@ingest_router.options("/{path:path}")
async def ingest_preflight():
    """Handle CORS preflight from any domain."""
    return Response(
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }
    )


# ── Tracker script serve ───────────────────────────────────────────────────────
script_router = APIRouter()

@script_router.get("/tracker.js")
async def serve_tracker_script(request: Request):
    """Serve tracker.js with the correct ingest base URL injected."""
    script_path = os.path.join(os.path.dirname(__file__), "../static/tracker.js")
    try:
        with open(script_path) as f:
            content = f.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Tracker script not found")

    base_url = str(request.base_url).rstrip("/")
    content = content.replace("__INGEST_BASE_URL__", f"{base_url}/track")

    return PlainTextResponse(
        content=content,
        media_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── Authenticated tracking dashboard router ────────────────────────────────────
tracking_router = APIRouter()


class SiteCreate(BaseModel):
    workspace_id: int
    name: str
    domain: str


@tracking_router.post("/sites")
async def create_site(
    body: SiteCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Register a new website for tracking. Returns the site with its tracker_id."""
    site = tracker_svc.create_site(body.workspace_id, body.name, body.domain, session)
    return {
        **site.dict(),
        "snippet": tracker_svc.get_snippet(site.tracker_id),
    }


@tracking_router.get("/sites")
async def list_sites(
    workspace_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return tracker_svc.get_sites(workspace_id, session)


@tracking_router.get("/sites/{tracker_id}/snippet")
async def get_snippet(
    tracker_id: str,
    request: Request,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    site = tracker_svc.get_site(tracker_id, session)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")
    return {"snippet": tracker_svc.get_snippet(tracker_id)}


@tracking_router.get("/sites/{tracker_id}/overview")
async def site_overview(
    tracker_id: str,
    days: int = 30,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return tracker_svc.get_overview(tracker_id, days, session)


@tracking_router.get("/sites/{tracker_id}/timeseries")
async def site_timeseries(
    tracker_id: str,
    days: int = 30,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return tracker_svc.get_time_series(tracker_id, days, session)


@tracking_router.get("/sites/{tracker_id}/pages")
async def site_top_pages(
    tracker_id: str,
    days: int = 30,
    limit: int = 20,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return tracker_svc.get_top_pages(tracker_id, days, limit, session)


@tracking_router.get("/sites/{tracker_id}/referrers")
async def site_referrers(
    tracker_id: str,
    days: int = 30,
    limit: int = 20,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return tracker_svc.get_referrers(tracker_id, days, limit, session)


@tracking_router.get("/sites/{tracker_id}/utm")
async def site_utm(
    tracker_id: str,
    days: int = 30,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """UTM campaign attribution — connects social posts to website visits."""
    return tracker_svc.get_utm_attribution(tracker_id, days, session)


@tracking_router.get("/sites/{tracker_id}/devices")
async def site_devices(
    tracker_id: str,
    days: int = 30,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return tracker_svc.get_devices(tracker_id, days, session)


@tracking_router.get("/sites/{tracker_id}/geography")
async def site_geography(
    tracker_id: str,
    days: int = 30,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return tracker_svc.get_geography(tracker_id, days, session)


@tracking_router.get("/sites/{tracker_id}/sessions")
async def site_sessions(
    tracker_id: str,
    limit: int = 25,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return tracker_svc.get_recent_sessions(tracker_id, limit, session)
