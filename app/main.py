from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.routers import (
    auth, posts, accounts, scheduler, media, analytics, ai, rss, team,
    agent, campaigns, brand_voice, approvals, timing, intelligence,
)
from app.routers.tracking import ingest_router, script_router, tracking_router
from fastapi.staticfiles import StaticFiles
import os
from app.core.database import create_db_and_tables
from app.core.scheduler import start_scheduler, shutdown_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(
    title="SocialPilot AI",
    description=(
        "AI-first social media management. Describe what you want in natural "
        "language and the agent plans campaigns, writes platform-specific "
        "variations in your brand voice, predicts the best times to post, and "
        "routes everything through AI-assisted approval."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tracker.js is embedded on any customer website
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── AI-first surface (primary) ────────────────────────────────────────────────
app.include_router(agent.router, prefix="/api/agent", tags=["agent"])
app.include_router(campaigns.router, prefix="/api/campaigns", tags=["campaigns"])
app.include_router(brand_voice.router, prefix="/api/brand-voice", tags=["brand-voice"])
app.include_router(approvals.router, prefix="/api/approvals", tags=["approvals"])
app.include_router(timing.router, prefix="/api/timing", tags=["timing"])
app.include_router(intelligence.intelligence_router, prefix="/api/intelligence", tags=["intelligence"])
app.include_router(ingest_router, prefix="/track", tags=["tracker-ingest"])
app.include_router(script_router, prefix="", tags=["tracker-script"])
app.include_router(tracking_router, prefix="/api/tracking", tags=["tracking"])

# ── Primitive / supporting endpoints ──────────────────────────────────────────
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(posts.router, prefix="/api/posts", tags=["posts"])
app.include_router(accounts.router, prefix="/api/accounts", tags=["accounts"])
app.include_router(scheduler.router, prefix="/api/scheduler", tags=["scheduler"])
app.include_router(media.router, prefix="/api/media", tags=["media"])
app.include_router(analytics.router, prefix="/api/analytics", tags=["analytics"])
app.include_router(ai.router, prefix="/api/ai", tags=["ai-tools"])
app.include_router(rss.router, prefix="/api/rss", tags=["rss"])
app.include_router(team.router, prefix="/api/team", tags=["team"])


# Serve static files (tracker.js)
_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "2.0.0", "mode": "ai-first"}
