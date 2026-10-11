"""FastAPI app. No accounts, no database: each browser gets an in-memory session.

A session holds only the normalized statement and its results (plus, while
waiting on a clarifying question, the redacted text). Uploads are parsed in
memory and never written to disk. Sessions expire after SESSION_TTL_SECONDS.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import pipeline
from .config import BENCHMARK_MIXES, settings
from .extract import UnsupportedFile
from .redact import redact
from .schema import Statement

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Advisor Audit", docs_url=None, redoc_url=None)
STATIC = Path(__file__).parent / "static"
COOKIE = "aa_sid"

_sessions: dict[str, dict] = {}
_lock = threading.Lock()


def _sweep() -> None:
    cutoff = time.time() - settings.session_ttl_seconds
    with _lock:
        for sid in [k for k, v in _sessions.items() if v["touched"] < cutoff]:
            del _sessions[sid]


def _session(request: Request, response: Response) -> tuple[str, dict]:
    _sweep()
    sid = request.cookies.get(COOKIE)
    with _lock:
        if not sid or sid not in _sessions:
            sid = secrets.token_urlsafe(24)
            _sessions[sid] = {}
        sess = _sessions[sid]
        sess["touched"] = time.time()
    response.set_cookie(COOKIE, sid, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                        max_age=settings.session_ttl_seconds)
    return sid, sess


def _check_profile(p: str) -> str:
    if p not in BENCHMARK_MIXES:
        raise HTTPException(400, "Risk profile must be conservative, balanced, or growth.")
    return p


def _finish(sess: dict, outcome: pipeline.NormalizeOutcome, profile: str, text: str) -> dict:
    if outcome.statement is None or (outcome.question and not sess.get("clarified")):
        sess.update(pending_text=text, profile=profile, statement=None, results=None)
        return {"status": "needs_clarification", "question": outcome.question}
    res = pipeline.results(outcome.statement, profile, outcome)
    sess.pop("pending_text", None)
    sess.update(statement=outcome.statement.model_dump(mode="json"), profile=profile, results=res)
    return {"status": "done", "results": res}


@app.middleware("http")
async def no_store(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/privacy")
def privacy():
    return FileResponse(STATIC / "privacy.html")


@app.get("/healthz")
def healthz():
    return {"ok": True, "provider": settings.model_provider}


@app.post("/api/analyze")
def analyze(request: Request, response: Response, file: UploadFile = File(...), risk_profile: str = Form(...)):
    profile = _check_profile(risk_profile)
    _, sess = _session(request, response)
    data = file.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, "That file is too large (15 MB max).")
    try:
        text, counts = pipeline.read_upload(file.filename or "upload", data)
    except UnsupportedFile as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logging.warning("extract failed: %s", type(e).__name__)  # never log content
        raise HTTPException(400, "We couldn't read that file. Try a PDF, CSV, XLSX, or a clear photo.")
    finally:
        del data
        file.file.close()
    sess["clarified"] = False
    outcome = pipeline.normalize(text)
    out = _finish(sess, outcome, profile, text)
    out["redactions"] = counts
    return out


@app.post("/api/clarify")
def clarify(request: Request, response: Response, answer: str = Form(...)):
    _, sess = _session(request, response)
    text = sess.get("pending_text")
    if not text:
        raise HTTPException(409, "Nothing is waiting for a clarification. Upload a statement first.")
    sess["clarified"] = True
    outcome = pipeline.normalize(text, clarification=redact(answer[:500])[0])
    return _finish(sess, outcome, sess["profile"], text)


@app.post("/api/profile")
def change_profile(request: Request, response: Response, risk_profile: str = Form(...)):
    """Re-run the comparison for a different risk profile without re-uploading."""
    profile = _check_profile(risk_profile)
    _, sess = _session(request, response)
    if not sess.get("statement"):
        raise HTTPException(409, "Upload a statement first.")
    s = Statement.model_validate(sess["statement"])
    res = pipeline.results(s, profile)
    res["warnings"] = sess["results"]["warnings"] if sess.get("results") else []
    res["engine"] = sess["results"].get("engine") if sess.get("results") else None
    sess.update(profile=profile, results=res)
    return {"status": "done", "results": res}


@app.delete("/api/session")
def delete_session(request: Request):
    sid = request.cookies.get(COOKIE)
    with _lock:
        existed = _sessions.pop(sid, None) is not None if sid else False
    resp = JSONResponse({"deleted": existed})
    resp.delete_cookie(COOKIE)
    return resp
