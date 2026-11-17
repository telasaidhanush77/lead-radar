import hmac
import json
import random
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from uuid import uuid4

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import queue
from app.config import get_settings
from app.dashboard import render_dashboard
from app.db import get_pool
from app.ingest import parse_csv, row_key, store_lead, verify
from app.log import request_id, setup_logging
from app.worker import start_background

MAX_BODY_BYTES = 5 * 1024 * 1024

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    stop = start_background(settings) if settings.run_worker else None
    yield
    if stop:
        stop.set()


app = FastAPI(title="Lead Radar", lifespan=lifespan)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid4().hex
    token = request_id.set(rid)
    try:
        response = await call_next(request)
    finally:
        request_id.reset(token)
    response.headers["X-Request-ID"] = rid
    return response


def get_conn() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:  # commits on success, rolls back on exception
        yield conn


async def raw_body(request: Request) -> bytes:
    # ponytail: reads the whole body before checking size; stream + cap if uploads grow.
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(413, "body too large")
    return body


def require_admin(authorization: str | None = Header(None)) -> None:
    token = get_settings().admin_token
    given = (authorization or "").removeprefix("Bearer ")
    if not token or not hmac.compare_digest(given, token):  # no token configured = locked
        raise HTTPException(401, "admin token required")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/webhooks/leads")
def webhook_leads(
    body: bytes = Depends(raw_body),
    x_timestamp: str | None = Header(None),
    x_signature: str | None = Header(None),
    idempotency_key: str | None = Header(None),
    conn: psycopg.Connection = Depends(get_conn),
):
    if not (x_timestamp and x_signature and idempotency_key):
        raise HTTPException(400, "X-Timestamp, X-Signature and Idempotency-Key are required")
    if not verify(get_settings().webhook_secret, x_timestamp, body, x_signature, now=time.time()):
        raise HTTPException(401, "bad or expired signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(422, "body must be JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(422, "body must be a JSON object")
    lead_id = store_lead(conn, "webhook", idempotency_key, payload)
    if lead_id is None:
        return JSONResponse({"status": "duplicate"}, status_code=200)
    return JSONResponse({"status": "accepted", "id": lead_id}, status_code=202)


@app.post("/imports", dependencies=[Depends(require_admin)])
def import_csv(body: bytes = Depends(raw_body), conn: psycopg.Connection = Depends(get_conn)):
    try:
        rows = parse_csv(body.decode("utf-8"))
    except UnicodeDecodeError:
        raise HTTPException(422, "CSV must be UTF-8") from None
    accepted = sum(store_lead(conn, "csv", row_key(r), r) is not None for r in rows)
    return {"accepted": accepted, "duplicates": len(rows) - accepted}


@app.get("/", response_class=HTMLResponse)
def dashboard(conn: psycopg.Connection = Depends(get_conn)) -> str:
    return render_dashboard(conn)


@app.post("/jobs/{job_id}/retry", dependencies=[Depends(require_admin)])
def retry_job(job_id: int, conn: psycopg.Connection = Depends(get_conn)) -> dict:
    if not queue.retry(conn, job_id):
        raise HTTPException(404, "no dead job with that id")
    return {"retried": True}


DEMO_COMPANIES = 80


@app.post("/demo/run", dependencies=[Depends(require_admin)])
def demo_run(conn: psycopg.Connection = Depends(get_conn)) -> dict:
    """Feed a fresh batch of synthetic leads through the real ingest path."""
    from bench.generate import generate

    seed = random.randrange(10**9)  # new seed each click, so keys never collide with old runs
    accepted = 0
    for lead in generate(seed, DEMO_COMPANIES):
        payload = {k: v for k, v in lead.items() if not k.startswith("_")}  # drop ground truth
        accepted += store_lead(conn, "demo", lead["_key"], payload) is not None
    return {"accepted": accepted}
