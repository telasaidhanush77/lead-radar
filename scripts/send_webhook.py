"""Send one signed test lead to a running Lead Radar.

    python scripts/send_webhook.py http://localhost:8000 change-me
"""

import json
import sys
import time
import uuid

import httpx

from app.ingest import sign

LEAD = {
    "email": "jan.weber@mueller-auto.de", "name": "Jan Weber", "title": "CISO",
    "company": "Müller Automotive GmbH", "country": "Germany", "employees": "250",
    "description": "Tier-1 supplier of brake systems for BMW. Hiring a security engineer.",
}


def main(base_url: str, secret: str) -> None:
    body = json.dumps(LEAD).encode()
    ts = str(int(time.time()))
    response = httpx.post(f"{base_url.rstrip('/')}/webhooks/leads", content=body, timeout=30,
                          headers={"X-Timestamp": ts, "X-Signature": sign(secret, ts, body),
                                   "Idempotency-Key": f"manual-{uuid.uuid4().hex[:8]}",
                                   "Content-Type": "application/json"})
    print(response.status_code, response.text)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
