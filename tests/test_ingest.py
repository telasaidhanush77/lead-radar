import json
import time

from app.ingest import parse_csv, sign, verify

SECRET = "test-secret"
ADMIN = {"Authorization": "Bearer test-admin"}


def signed(body: bytes, key: str = "k1", ts: float | None = None, secret: str = SECRET) -> dict:
    stamp = str(int(ts if ts is not None else time.time()))
    return {"X-Timestamp": stamp, "X-Signature": sign(secret, stamp, body),
            "Idempotency-Key": key, "Content-Type": "application/json"}


def post_lead(client, payload, **kw):
    body = json.dumps(payload).encode()
    return client.post("/webhooks/leads", content=body, headers=signed(body, **kw))


def test_verify_rejects_tampered_body():
    sig = sign(SECRET, "100", b"{}")
    assert verify(SECRET, "100", b"{}", sig, now=100)
    assert not verify(SECRET, "100", b'{"x":1}', sig, now=100)
    assert not verify(SECRET, "abc", b"{}", sig, now=100)


def test_valid_webhook_accepted_and_enqueued(client, db):
    r = post_lead(client, {"email": "a@acme.de"})
    assert r.status_code == 202 and r.json() == {"status": "accepted", "id": 1}
    job = db.execute("SELECT kind, payload FROM jobs").fetchone()
    assert job == {"kind": "process_lead", "payload": {"raw_lead_id": 1}}


def test_bad_signature_401(client, db):
    body = b'{"email":"a@acme.de"}'
    r = client.post("/webhooks/leads", content=body, headers=signed(body, secret="wrong"))
    assert r.status_code == 401
    assert db.execute("SELECT count(*) AS n FROM raw_leads").fetchone()["n"] == 0


def test_stale_timestamp_401(client):
    assert post_lead(client, {"email": "a@acme.de"}, ts=time.time() - 301).status_code == 401


def test_missing_headers_400(client):
    r = client.post("/webhooks/leads", content=b"{}", headers={"X-Timestamp": "1"})
    assert r.status_code == 400


def test_duplicate_key_200_and_first_body_wins(client, db):
    assert post_lead(client, {"email": "first@acme.de"}).status_code == 202
    r = post_lead(client, {"email": "second@acme.de"})
    assert r.status_code == 200 and r.json() == {"status": "duplicate"}
    rows = db.execute("SELECT payload FROM raw_leads").fetchall()
    assert rows == [{"payload": {"email": "first@acme.de"}}]
    assert db.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 1


def test_non_object_body_422(client):
    assert post_lead(client, ["not", "an", "object"]).status_code == 422
    body = b"not json"
    assert client.post("/webhooks/leads", content=body, headers=signed(body)).status_code == 422


def test_import_requires_token(client):
    r = client.post("/imports", content=b"email\na@x.de\n", headers={"Content-Type": "text/csv"})
    assert r.status_code == 401
    r = client.post("/imports", content=b"email\na@x.de\n",
                    headers={"Content-Type": "text/csv", "Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_parse_csv_drops_empty_rows_and_values():
    rows = parse_csv(" Email , Company \na@x.de,\n,\n\nb@y.de,Y AG\n")
    assert rows == [{"email": "a@x.de"}, {"email": "b@y.de", "company": "Y AG"}]


def test_import_semicolon_bom_csv(client, db):
    body = "﻿Email;Company\na@x.de;X GmbH\n".encode()
    r = client.post("/imports", content=body, headers={**ADMIN, "Content-Type": "text/csv"})
    assert r.status_code == 200 and r.json() == {"accepted": 1, "duplicates": 0}
    row = db.execute("SELECT source, payload FROM raw_leads").fetchone()
    assert row == {"source": "csv", "payload": {"email": "a@x.de", "company": "X GmbH"}}


def test_import_twice_all_duplicates(client):
    body = b"email,company\na@x.de,X\nb@y.de,Y\n"
    headers = {**ADMIN, "Content-Type": "text/csv"}
    client.post("/imports", content=body, headers=headers)
    assert client.post("/imports", content=body, headers=headers).json() == {
        "accepted": 0, "duplicates": 2}


def test_import_too_large_413(client):
    body = b"email\n" + b"a@x.de\n" * 800_000  # ~5.6 MB
    r = client.post("/imports", content=body, headers={**ADMIN, "Content-Type": "text/csv"})
    assert r.status_code == 413


def test_request_id_echoed(client):
    assert client.get("/healthz", headers={"X-Request-ID": "abc"}).headers["X-Request-ID"] == "abc"
    assert len(client.get("/healthz").headers["X-Request-ID"]) == 32
