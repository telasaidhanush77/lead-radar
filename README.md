# Lead Radar

**A small production-style GTM data pipeline:** inbound B2B leads arrive messy (a signed
webhook or a CSV), get cleaned and deduplicated into companies, enriched, **scored on how
badly they need compliance automation** (ISO 27001, SOC 2, TISAX, GDPR), routed to the right
rep, and synced to a CRM, idempotently. It's one Python service on Postgres, with a
reproducible benchmark behind every number below.

Built as a portfolio project for a *Backend Engineer, GTM Engineering* role. All data is
synthetic.

```
 webhook (HMAC) ─┐                     Postgres = data store + job queue (FOR UPDATE SKIP LOCKED)
 CSV import ─────┴─► raw_leads ─► process_lead ─► enrich ─► score ─► route ─► crm_sync
                     (verbatim,     normalize      Gemini /   rules,   region,   HubSpot or
                      idempotent)   dedupe/merge   Groq /     explain- sticky    fake table,
                                                   rules      able     owner,    hash-skip
                                                                       alert job
```

## Results

<!-- results:start -->
Measured 2026-11-18 at commit `d224f36`. Fuzzy threshold **92** (tuned on seed 1); numbers below are held-out seed 2.

| Pipeline | |
|---|---|
| Leads in → true companies → found | 788 → 300 → 318 |
| Dedupe precision / recall / F1 | 0.998 / 0.940 / 0.968 |
| Throughput (ingest → CRM, 4 workers, laptop + Docker) | 14 leads/s |
| Dead jobs | 0 |
| Re-run: new alerts / new CRM records / new companies | 0 / 0 / 0 |

| Classifier (60 hand-labelled) | Industry acc. | Signal F1 | p50 latency | Fallbacks | Cost / 1k leads* |
|---|---|---|---|---|---|
| rules | 42% | 0.37 | 0 ms | 0 | $0.000 |
| gemini (`gemini-3.5-flash-lite`) | 92% | 0.83 | 1012 ms | 0 | $0.108 |
| groq (`openai/gpt-oss-120b`) | 93% | 0.87 | 761 ms | 0 | $0.131 |

Leakage check: rules score 76% on generated descriptions vs the hand-labelled figure above, so only the hand-labelled number is reported.

*At the provider's paid list price (see `bench/pricing.py`); this project runs on free tiers, so actual spend is $0.
<!-- results:end -->

How the numbers are kept honest:
- **Dedupe:** the fuzzy-match threshold is tuned on seed 1 and reported on **held-out seed 2**.
  Precision/recall are pairwise against the generator's ground truth (`_entity`).
- **Classifiers:** scored on **60 hand-written descriptions** (`bench/labelled.jsonl`): English,
  German, French and Dutch, ~10 deliberately tricky. They are never produced by the generator.
  The benchmark also scores the keyword rules on generated text and prints both figures, so the
  leakage this avoids is visible (see the line under the results table).
- **Idempotency:** the benchmark re-runs every lead through the whole pipeline and **fails** if
  that creates a single new alert, CRM record or company.
- **Throughput** is measured on a laptop with Postgres in Docker Desktop. Profiling shows ~97%
  of the time is spent waiting on database round trips (~10 per job at ~2.5 ms each on Docker for
  Windows). Python is not the bottleneck, and more workers scale it (`SKIP LOCKED`).
- **Cost** uses each provider's paid list price (`bench/pricing.py`, with source and date). The
  project itself runs on free tiers, so actual spend is $0.

## Quickstart

```bash
docker compose up --build          # Postgres + app on http://localhost:8000
```
Open the dashboard, enter the admin token (`change-me` by default), click **Run demo**.

Without Docker for the app (Windows has no `make`, so the commands are given in full):

```bash
docker compose up -d db
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"     # bin/ on macOS/Linux
pytest -q                          # 190+ tests against a real Postgres
ruff check .
python -m bench.run                # rules only, offline
python -m bench.run --providers rules gemini groq   # with GEMINI_API_KEY / GROQ_API_KEY in .env
```

Send a signed webhook (HMAC-SHA256 over `timestamp.body`, rejected after 5 minutes):
```bash
python scripts/send_webhook.py http://localhost:8000 change-me    # -> 202 accepted
```

**Deploy ($0):** `render.yaml` runs the Docker image on Render's free plan with Neon Postgres.
The worker runs as a thread in the web process (the free plan has no worker services), and the
public demo replays LLM answers the benchmark pre-computed for the demo dataset, so visitors
never spend API quota (until the benchmark has run with API keys, the demo uses the rules). The free plan sleeps
when idle, so the first request can take ~50 s.

## How scoring works

Transparent points, so a rep can read *why* a lead is hot:

| Rule | Points | Why it signals compliance need |
|---|---|---|
| HQ in Europe (EU27 + UK, CH, NO, IS, LI) | 15 | GDPR; buyers ask for ISO 27001 |
| 20–500 employees | 15 | Gets security questionnaires, has no GRC team |
| Automotive supplier | 30 | OEMs require **TISAX**, a hard deadline |
| US enterprise customers | 20 | US procurement asks for **SOC 2** |
| Hiring security/compliance roles | 20 | Investing in security right now |
| Regulated industry | 15 | Compliance is mandatory |
| Senior contact (C-level/VP/Director) | 10 | Can buy or champion |

Tier **A ≥ 70** → AE alert (once per company, ever) · **B ≥ 40** → BDR queue · **C** → nurture.

## Design choices

- **Postgres as the queue.** Each job's writes and its `done` mark commit in one transaction;
  failures back off exponentially and land in a visible dead-letter list with a Retry button.
- **Idempotent everywhere:** webhook idempotency keys (`ON CONFLICT DO NOTHING`), contact upserts
  by email, a compare-and-set `alerted_at` for the one-time alert, content-hash skip for CRM
  writes. CRM sync locks the company row and commits the CRM id before syncing contacts, and a
  create is never retried after a 5xx, so retries and parallel workers don't duplicate records.
- **Entity resolution:** exact domain → fuzzy name (`rapidfuzz`) within the same country →
  new company. Per-field provenance: the newest non-null value wins, by the time the lead
  was received rather than the order it was processed in.
- **LLMs as an option, not a dependency.** One `Classifier` interface: keyword rules (default,
  offline), Gemini, Groq. Replies are strictly validated against a closed label set. Any
  429/5xx/bad JSON falls back to rules and is counted. Results are cached by content hash.
- **A research agent, kept on a short leash.** `POST /companies/{id}/brief` runs a bounded
  tool-calling loop (Groq): `fetch_page` reads the company's *own* site (homepage, careers,
  case studies), then `finish` writes an account brief and first email that cite those pages.
  Guardrails: domain allow-list and public-IP check on every redirect hop (no SSRF), size and
  time caps, page text treated as untrusted, at most 6 turns with the last one forced to
  `finish`, and a stop after 3 failed fetches. Every step is stored (`agent_steps`) and
  returned, and the result is a **draft a human approves**. Nothing is ever sent.
- **No ORM, no frontend framework.** Hand-written SQL; one server-rendered, escaped HTML page.

## Limitations

- Fuzzy matching scans companies in the same country (O(n) per lead); `pg_trgm` is the next
  step past ~50k companies. Dedupe runs under one advisory lock (simple and correct, but
  serialised).
- The agent only reads the company's own website. Large corporate sites often block bots
  (HTTP 403/429), in which case it falls back to the template brief. SMB sites usually work.
- One-way CRM sync; HubSpot gets standard fields only (the score rides in `description`).
- The AE alert is its own job, so a Slack outage only delays it. A crash between sending and
  committing can still repeat one alert; a delivery-id dedupe on the receiving side would fix it.
- Synthetic data: realistic mess, but not real-world distributions.
