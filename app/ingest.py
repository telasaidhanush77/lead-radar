"""Getting leads in: webhook signature checks, storing raw payloads once, CSV parsing."""

import csv
import hashlib
import hmac
import io
import json

import psycopg
from psycopg.types.json import Jsonb

from app.queue import enqueue

TOLERANCE_S = 300  # reject signed requests older than 5 minutes (replay protection)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """Hex HMAC-SHA256 over 'timestamp.' + body. Signing the timestamp stops replays of old
    captured requests; signing the body stops tampering."""
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str, now: float,
           tolerance_s: int = TOLERANCE_S) -> bool:
    if not secret or not timestamp.isdigit() or abs(now - int(timestamp)) > tolerance_s:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)  # constant-time


def store_lead(conn: psycopg.Connection, source: str, idempotency_key: str,
               payload: dict) -> int | None:
    """Insert the raw lead and enqueue processing, or return None if the key was seen before.
    The UNIQUE constraint decides, so two concurrent retries cannot both get in."""
    row = conn.execute(
        "INSERT INTO raw_leads (source, idempotency_key, payload) VALUES (%s, %s, %s) "
        "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
        (source, idempotency_key, Jsonb(payload)),
    ).fetchone()
    if row is None:
        return None
    enqueue(conn, "process_lead", {"raw_lead_id": row["id"]})
    return row["id"]


def parse_csv(text: str) -> list[dict]:
    """Rows as dicts with lowercased headers; empty values and empty rows dropped.
    Handles the Excel-in-Europe export: UTF-8 BOM and ';' as the delimiter."""
    text = text.removeprefix("﻿")
    header = text.split("\n", 1)[0]
    try:
        dialect = csv.Sniffer().sniff(header, delimiters=",;")
    except csv.Error:
        dialect = csv.excel  # single column: nothing to sniff
    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)
    if not rows:
        return []
    keys = [h.strip().lower() for h in rows[0]]
    out = []
    for values in rows[1:]:
        row = {k: v.strip() for k, v in zip(keys, values, strict=False) if k and v.strip()}
        if row:
            out.append(row)
    return out


def row_key(row: dict) -> str:
    """A CSV row has no idempotency key of its own, so its content is the key."""
    return "csv:" + hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
