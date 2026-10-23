"""Local web app: login -> pick meter -> fetch a year -> compare plans.

Binds to 127.0.0.1 only. Sessions live in memory; passwords are used once
at login and never stored. Start with ``alectra-plans`` (or
``python -m alectra_plans.server``).
"""

from __future__ import annotations

import asyncio
import csv
import io
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analysis
from .cache import UsageCache
from .client import AlectraError, AlectraSession, LoginFailed, TfaRequired
from .models import UsageRecord

STATIC_DIR = Path(__file__).parent / "static"
MAX_MONTHS = 36
SESSION_TTL_SECONDS = 45 * 60

app = FastAPI(title="alectra-plan-compare", docs_url=None, redoc_url=None)


# ------------------------------------------------------------------ state


@dataclass
class FetchJob:
    job_id: str
    session_id: str
    account_number: str
    meter_number: str
    months: list[tuple[int, int]]
    state: str = "running"  # running | done | error
    done: int = 0
    error: str | None = None
    entries: list[dict] = field(default_factory=list)
    records: list[UsageRecord] = field(default_factory=list)
    result: dict | None = None


@dataclass
class UiSession:
    session: AlectraSession
    meters: list = field(default_factory=list)
    jobs: dict[str, FetchJob] = field(default_factory=dict)
    last_used: float = field(default_factory=time.time)


SESSIONS: dict[str, UiSession] = {}
CACHE = UsageCache()


def _get_session(session_id: str) -> UiSession:
    ui = SESSIONS.get(session_id)
    if not ui:
        raise HTTPException(404, "Session expired; log in again.")
    ui.last_used = time.time()
    return ui


def _get_job(session_id: str, job_id: str) -> FetchJob:
    ui = _get_session(session_id)
    job = ui.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown fetch job.")
    return job


def _expire_sessions() -> None:
    now = time.time()
    for sid in [s for s, ui in SESSIONS.items() if now - ui.last_used > SESSION_TTL_SECONDS]:
        ui = SESSIONS.pop(sid)
        asyncio.create_task(ui.session.close())


async def _close_session(session_id: str) -> None:
    ui = SESSIONS.pop(session_id, None)
    if ui:
        await ui.session.close()


# ------------------------------------------------------------------ schemas


class LoginRequest(BaseModel):
    username: str
    password: str
    show_browser: bool = False


class TfaRequest(BaseModel):
    session_id: str
    code: str


class UsageRequest(BaseModel):
    session_id: str
    account_number: str
    meter_number: str
    months: int = 12


class TeslaRequest(BaseModel):
    kwh_per_month: float = 0.0
    per_month: dict[str, float] = {}


# ------------------------------------------------------------------ helpers


def _month_list(count: int) -> list[tuple[int, int]]:
    today = date.today().replace(day=1)
    out = []
    y, m = today.year, today.month
    for _ in range(count):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def _compute_analysis(job: FetchJob) -> dict:
    if job.result is None:
        job.result = analysis.analyze(job.records)
    return job.result


async def _run_fetch(job: FetchJob, ui: UiSession) -> None:
    try:
        entries = await ui.session.fetch_range(
            job.account_number,
            job.meter_number,
            start=date(*job.months[0], 1),
            end=date(*job.months[-1], 1),
            on_month_done=lambda done, total, key: _note(job, done, total, key),
            cache=CACHE,
        )
        job.entries = entries
        job.records = [UsageRecord.from_api(e) for e in entries]
        _compute_analysis(job)
        job.state = "done"
    except Exception as exc:  # surface to the UI, keep server alive
        job.state = "error"
        job.error = str(exc)
        try:
            await ui.session.close()
        except Exception:
            pass


def _note(job: FetchJob, done: int, total: int, key: str) -> None:
    job.done = done


# ------------------------------------------------------------------ routes


@app.post("/api/login")
async def login(req: LoginRequest):
    _expire_sessions()
    ui = UiSession(session=AlectraSession(headless=not req.show_browser))
    await ui.session.__aenter__()
    try:
        await ui.session.login(req.username, req.password)
    except TfaRequired as exc:
        sid = uuid.uuid4().hex
        SESSIONS[sid] = ui
        return {"status": "tfa_required", "session_id": sid, "message": str(exc)}
    except (LoginFailed, AlectraError) as exc:
        await ui.session.close()
        raise HTTPException(401, str(exc))
    except Exception as exc:
        await ui.session.close()
        raise HTTPException(500, f"Login failed: {exc}")

    sid = uuid.uuid4().hex
    try:
        ui.meters = await ui.session.list_meters()
    except AlectraError as exc:
        SESSIONS[sid] = ui  # keep the login; meter discovery can be retried
        return {
            "status": "ok_no_meters",
            "session_id": sid,
            "meters": [],
            "message": f"Logged in, but meter discovery failed: {exc}. "
            "You can still enter your account + meter number manually.",
        }
    SESSIONS[sid] = ui
    return {
        "status": "ok",
        "session_id": sid,
        "meters": [m.__dict__ for m in ui.meters],
    }


@app.post("/api/tfa")
async def tfa(req: TfaRequest):
    ui = _get_session(req.session_id)
    try:
        await ui.session.submit_tfa(req.code)
        ui.meters = await ui.session.list_meters()
    except Exception as exc:
        raise HTTPException(401, f"Verification failed: {exc}")
    return {"status": "ok", "meters": [m.__dict__ for m in ui.meters]}


@app.post("/api/usage")
async def start_usage(req: UsageRequest):
    ui = _get_session(req.session_id)
    months = min(max(req.months, 1), MAX_MONTHS)
    job = FetchJob(
        job_id=uuid.uuid4().hex,
        session_id=req.session_id,
        account_number=req.account_number,
        meter_number=req.meter_number,
        months=_month_list(months),
    )
    ui.jobs[job.job_id] = job
    asyncio.create_task(_run_fetch(job, ui))
    return {"job_id": job.job_id, "total": len(job.months)}


@app.get("/api/usage/status/{job_id}")
async def usage_status(session_id: str, job_id: str):
    job = _get_job(session_id, job_id)
    return {
        "state": job.state,
        "done": job.done,
        "total": len(job.months),
        "error": job.error,
    }


@app.get("/api/analysis/{job_id}")
async def get_analysis(session_id: str, job_id: str):
    job = _get_job(session_id, job_id)
    if job.state != "done":
        raise HTTPException(409, f"Fetch not finished ({job.state}).")
    return _compute_analysis(job)


@app.post("/api/analysis/{job_id}/tesla")
async def tesla(session_id: str, job_id: str, req: TeslaRequest):
    job = _get_job(session_id, job_id)
    if job.state != "done":
        raise HTTPException(409, f"Fetch not finished ({job.state}).")
    monthly = dict(req.per_month)
    if req.kwh_per_month:
        for m in job.months:
            monthly.setdefault(f"{m[0]:04d}-{m[1]:02d}", req.kwh_per_month)
    return analysis.simulate_tesla_shift(job.records, monthly)


@app.get("/api/export/{job_id}.csv")
async def export_csv(session_id: str, job_id: str):
    job = _get_job(session_id, job_id)
    if job.state != "done":
        raise HTTPException(409, "Fetch not finished.")
    result = _compute_analysis(job)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["month", "kwh", "tiered_cad", "tou_cad", "ulo_cad", "best", "alectra_billed_cad"])
    for m in result["months"]:
        p = m["plans"]
        writer.writerow(
            [m["key"], m["kwh"], p["tiered"], p["tou"], p["ulo"], m["best"] or "", m["api_amount"]]
        )
    totals = result["totals"]
    writer.writerow([])
    writer.writerow(["TOTAL", totals["kwh"], totals["tiered"], totals["tou"], totals["ulo"], totals["best"]])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=alectra_plans_{job_id[:8]}.csv"},
    )


@app.get("/api/demo")
async def demo(session_id: str = ""):
    """Build a job from bundled sample data so the UI works without an account."""
    import json

    samples = Path(__file__).parent.parent / "tests" / "data"
    entries: list[dict] = []
    for name in ("june_2025_usage.json", "january_2026_usage.json"):
        entries.extend(json.loads((samples / name).read_text())["Result"]["electricUsages"])
    if not entries:
        raise HTTPException(404, "No sample data bundled.")
    sid = session_id
    if sid not in SESSIONS:
        sid = uuid.uuid4().hex
        SESSIONS[sid] = UiSession(session=AlectraSession())
    ui = SESSIONS[sid]
    job = FetchJob(
        job_id=uuid.uuid4().hex,
        session_id=sid,
        account_number="DEMO",
        meter_number="DEMO",
        months=[(2025, 6), (2026, 1)],
        state="done",
    )
    job.entries = entries
    job.records = [UsageRecord.from_api(e) for e in entries]
    _compute_analysis(job)
    ui.jobs[job.job_id] = job
    return {"session_id": sid, "job_id": job.job_id, "state": "done"}


@app.post("/api/logout")
async def logout(session_id: str):
    await _close_session(session_id)
    return {"status": "ok"}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main() -> None:
    import uvicorn

    host, port = "127.0.0.1", 8765
    print(f"\n  alectra-plan-compare -> http://{host}:{port}\n")
    webbrowser.open(f"http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
