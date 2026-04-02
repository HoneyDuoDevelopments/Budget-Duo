"""
Budget Duo Scraper Service — Internal API
Receives scrape commands from the backend, runs Playwright, returns results.
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import asyncio
import logging
import uuid
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Budget Duo Scraper v1.0")

# In-memory session store for active scraper runs
# Key: session_id, Value: session dict
sessions: dict[str, dict] = {}


class AppleStartRequest(BaseModel):
    apple_id: Optional[str] = None      # If not provided, reads from env
    password: Optional[str] = None      # If not provided, reads from env
    start_date: str                     # YYYY-MM-DD
    end_date: str                       # YYYY-MM-DD
    backfill: bool = False


class AppleVerifyRequest(BaseModel):
    session_id: str
    code: str             # 6-digit 2FA code


class SynchronyStartRequest(BaseModel):
    username: str
    password: str


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():
    return {"status": "ok", "service": "scraper", "version": "1.0"}


# ============================================================
# APPLE CARD — Semi-automated (2FA)
# ============================================================

@app.post("/api/scrape/apple/start")
async def apple_start(body: AppleStartRequest):
    session_id = str(uuid.uuid4())

    # Resolve credentials — request body overrides env vars
    apple_id = body.apple_id or os.environ.get("SCRAPER_APPLE_USERNAME", "")
    apple_password = body.password or os.environ.get("SCRAPER_APPLE_PASSWORD", "")

    if not apple_id or not apple_password:
        raise HTTPException(
            status_code=400,
            detail="Apple credentials not provided and not found in environment. Run fetch-vault-creds.sh on the host."
        )

    sessions[session_id] = {
        "id": session_id,
        "provider": "apple",
        "status": "starting",
        "balance": None,
        "available": None,
        "transactions": [],
        "error": None,
        "start_date": body.start_date,
        "end_date": body.end_date,
        "backfill": body.backfill,
    }

    asyncio.create_task(_run_apple_scraper(
        session_id, apple_id, apple_password,
        body.start_date, body.end_date, body.backfill
    ))

    return {"session_id": session_id, "status": "starting"}


@app.post("/api/scrape/apple/verify")
async def apple_verify(body: AppleVerifyRequest):
    """
    Submit 2FA code for an active Apple Card session.
    The Playwright process is polling for this code.
    """
    session = sessions.get(body.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session["status"] != "awaiting_2fa":
        raise HTTPException(status_code=400, detail=f"Session not awaiting 2FA (status: {session['status']})")

    session["2fa_code"] = body.code
    session["status"] = "verifying_2fa"
    return {"status": "verifying_2fa"}


# ============================================================
# SYNCHRONY — Fully automated (no 2FA)
# ============================================================

class SynchronyStartRequest(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None


@app.post("/api/scrape/synchrony/start")
async def synchrony_start(body: SynchronyStartRequest):
    session_id = str(uuid.uuid4())

    username = body.username or os.environ.get("SCRAPER_SYNCHRONY_USERNAME", "")
    password = body.password or os.environ.get("SCRAPER_SYNCHRONY_PASSWORD", "")

    if not username or not password:
        raise HTTPException(
            status_code=400,
            detail="Synchrony credentials not provided and not found in environment. Run fetch-vault-creds.sh on the host."
        )

    sessions[session_id] = {
        "id": session_id,
        "provider": "synchrony",
        "status": "starting",
        "accounts": [],
        "error": None,
    }

    asyncio.create_task(_run_synchrony_scraper(
        session_id, username, password
    ))

    return {"session_id": session_id, "status": "starting"}


# ============================================================
# SESSION STATUS — Polling endpoint
# ============================================================

@app.get("/api/scrape/status/{session_id}")
def get_status(session_id: str):
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Return status without sensitive fields
    safe = {k: v for k, v in session.items() if k not in ("2fa_code",)}
    return safe


# ============================================================
# SCRAPER IMPLEMENTATIONS (placeholder — filled in next steps)
# ============================================================

async def _run_apple_scraper(
    session_id: str, apple_id: str, password: str,
    start_date: str, end_date: str, backfill: bool
):
    """Apple Card scraper — Playwright automation."""
    session = sessions[session_id]
    try:
        from app.scrapers.apple_card import scrape_apple_card
        result = await scrape_apple_card(session, apple_id, password, start_date, end_date, backfill)
        session.update(result)
    except Exception as e:
        logger.exception(f"Apple scraper failed: {e}")
        session["status"] = "error"
        session["error"] = str(e)


async def _run_synchrony_scraper(session_id: str, username: str, password: str):
    """Synchrony scraper — Playwright automation."""
    session = sessions[session_id]
    try:
        from app.scrapers.synchrony import scrape_synchrony
        result = await scrape_synchrony(session, username, password)
        session.update(result)
    except Exception as e:
        logger.exception(f"Synchrony scraper failed: {e}")
        session["status"] = "error"
        session["error"] = str(e)