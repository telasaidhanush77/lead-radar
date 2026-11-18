import hmac
import json
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg.types.json import Jsonb

from app import queue
from app.brief import approve_brief, create_brief, get_brief
from app.config import get_settings
from app.dashboard import render_dashboard
from app.db import get_pool, migrate
from app.ingest import parse_csv, row_key, store_lead, verify
from app.log import request_id, setup_logging
from app.worker import start_background

MAX_BODY_BYTES = 5 * 1024 * 1024
DEMO_CACHE = Path(__file__).resolve().parent.parent / "bench" / "classify_cache.jsonl"

setup_logging()


def load_demo_cache(conn: psycopg.Connection, path: Path = DEMO_CACHE) -> int:
    """Seed classify_cache with LLM answers computed offline, so the public demo shows real
    Gemini/Groq results without calling them. Returns rows added."""
    if not path.exists():
        return 0
    added = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        added += conn.execute(
            "INSERT INTO classify_cache (provider, content_hash, result, latency_ms) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (row["provider"], row["content_hash"], Jsonb(row["result"]), row.get("latency_ms")),
        ).rowcount
    return added


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    with get_pool().connection() as conn:  # deploys have no separate migrate step
        migrate(conn)
        load_demo_cache(conn)
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
def healthz(conn: psycopg.Connection = Depends(get_conn)) -> dict:
    conn.execute("SELECT 1")  # a dead database should fail the platform's health check
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


@app.post("/demo/run", dependencies=[Depends(require_admin)])
def demo_run(conn: psycopg.Connection = Depends(get_conn)) -> dict:
    """Feed a fresh batch of synthetic leads through the real ingest path."""
    from bench.generate import demo_leads

    accepted = 0
    for lead in demo_leads(uuid4().hex[:8]):  # fresh keys, same texts the cache covers
        payload = {k: v for k, v in lead.items() if not k.startswith("_")}  # drop ground truth
        accepted += store_lead(conn, "demo", lead["_key"], payload) is not None
    return {"accepted": accepted}


@app.post("/companies/{company_id}/brief", status_code=201,
          dependencies=[Depends(require_admin)])
def new_brief(company_id: int, conn: psycopg.Connection = Depends(get_conn)) -> dict:
    row = create_brief(conn, company_id)
    if row is None:
        raise HTTPException(404, "no scored company with that id")
    return row


@app.post("/briefs/{brief_id}/approve", dependencies=[Depends(require_admin)])
def approve(brief_id: int, conn: psycopg.Connection = Depends(get_conn)) -> dict:
    row = approve_brief(conn, brief_id)
    if row is None:
        raise HTTPException(404, "no brief with that id")
    return row


@app.get("/briefs/{brief_id}", dependencies=[Depends(require_admin)])
def read_brief(brief_id: int, conn: psycopg.Connection = Depends(get_conn)) -> dict:
    row = get_brief(conn, brief_id)
    if row is None:
        raise HTTPException(404, "no brief with that id")
    return row
