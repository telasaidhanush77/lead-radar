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

## Live: real companies that need compliance right now

The synthetic data proves the plumbing. This proves the point: `python -m app.signals` pulls
**real job postings** from the [Arbeitnow](https://www.arbeitnow.com) job board API (free,
no key, mostly DACH), keeps roles whose title says TISAX, ISO 27001, SOC 2, ISMS,
information security or data protection, drops recruiting agencies and security *vendors*
hiring sales staff (they sell security, they don't buy it), collapses the same remote role
listed once per country, and runs each posting through the same pipeline. (The
[Adzuna](https://developer.adzuna.com/) API is wired in too and joins automatically when
`ADZUNA_APP_ID` / `ADZUNA_APP_KEY` are set.) A company advertising an
"ISMS Manager (ISO 27001)" is building the programme a compliance-automation vendor sells,
and it says so in public. Postings for the same company add up (a company hiring for TISAX
*and* ISO 27001 keeps both signals). The deployed app pulls again **every
24 hours on its own** (a self-rescheduling queue job; a unique partial index keeps it to one
pending run however often the free-tier host sleeps and restarts). Every company gets a page,
`/companies/{id}` (and the same as JSON at `/api/companies/{id}`), listing the score, why,
and the postings behind it, and the AE's Slack alert says *why now*: the role they are
hiring for. The table below is written by the script:

<!-- signals:start -->
Pulled 2026-11-19 from the [Arbeitnow](https://www.arbeitnow.com) job board API: **34** open security/compliance roles (TISAX, ISO 27001, SOC 2, ISMS, Information security, Data protection) among the newest 4,500 postings; 0 from recruiting agencies dropped.

| Company | Country | Score | Tier | Postings | Why | Example role |
|---|---|---|---|---|---|---|
| bunq | NL | 50 | B | 1 | hiring_security +20, eu_hq +15, regulated +15 | [Security Engineer](https://www.arbeitnow.nl/jobs/companies/bunq/security-engineer-amsterdam-384590) |
| Encord | GB | 50 | B | 1 | hiring_security +20, eu_hq +15, framework_role +15 | [Senior Security Engineer, GRC & Customer Trust](https://www.arbeitnow.co.uk/jobs/companies/encord/senior-security-engineer-grc-customer-trust-london-402675) |
| Unblu | CH | 50 | B | 1 | hiring_security +20, eu_hq +15, regulated +15 | [Security Engineer](https://www.arbeitnow.ch/jobs/companies/unblu/security-engineer-basel-72871) |
| Dashlane | FR | 45 | B | 2 | hiring_security +20, eu_hq +15, active_hiring +10 | [Staff Product Security Engineer](https://www.arbeitnow.fr/jobs/companies/dashlane/staff-product-security-engineer-paris-408629) |
| Mistral.ai | FR | 45 | B | 2 | hiring_security +20, eu_hq +15, active_hiring +10 | [Applied AI Engineer, CyberSecurity, Defense](https://www.arbeitnow.fr/jobs/companies/mistralai/applied-ai-engineer-cybersecurity-defense-paris-31994) |
| A-Gas | GB | 35 | C | 1 | hiring_security +20, eu_hq +15 | [Infrastructure and Security Engineer](https://www.arbeitnow.co.uk/jobs/companies/a-gas/infrastructure-and-security-engineer-portbury-33236) |
| Getyourguide | CH | 35 | C | 1 | hiring_security +20, eu_hq +15 | [Working Student, Security Engineer](https://www.arbeitnow.ch/jobs/companies/getyourguide/working-student-security-engineer-zurich-293662) |
| Happyrobot.ai | FR | 35 | C | 1 | hiring_security +20, eu_hq +15 | [Senior Application Security Engineer](https://www.arbeitnow.fr/jobs/companies/happyrobotai/senior-application-security-engineer-france-114613) |
| isaraerospace | DE | 35 | C | 1 | hiring_security +20, eu_hq +15 | [Senior Intelligence Officer, Information & Cybersecurity](https://www.arbeitnow.com/jobs/companies/isaraerospace/senior-intelligence-officer-information-cybersecurity-parsdorf-bavaria-185786) |
| Jamorie Consulting GmbH | DE | 35 | C | 1 | hiring_security +20, eu_hq +15 | [Lead Consultant Cyber Security (m/w/d)](https://www.arbeitnow.com/jobs/companies/jamorie-consulting-gmbh/lead-consultant-cyber-security-berlin-313351) |
| Janestreet | GB | 35 | C | 1 | hiring_security +20, eu_hq +15 | [Cybersecurity Engineer](https://www.arbeitnow.co.uk/jobs/companies/janestreet/cybersecurity-engineer-london-287765) |
| Ledger | FR | 35 | C | 1 | hiring_security +20, eu_hq +15 | [Head of Security Engineering](https://www.arbeitnow.fr/jobs/companies/ledger/head-of-security-engineering-paris-309363) |
| Namespace | CH | 35 | C | 1 | hiring_security +20, eu_hq +15 | [Security Engineer](https://www.arbeitnow.ch/jobs/companies/namespace/security-engineer-zurich-407809) |
| Neuverge Tron | DE | 35 | C | 1 | hiring_security +20, eu_hq +15 | [Cybersecurity System Engineer / Technical Expert](https://www.arbeitnow.com/jobs/companies/neuverge-tron/cybersecurity-system-engineer-technical-expert-munich-31611) |
| NXT Hero GmbH | DE | 35 | C | 1 | hiring_security +20, eu_hq +15 | [IT Security Officer (m/w/d)](https://www.arbeitnow.com/jobs/companies/nxt-hero-gmbh/it-security-officer-leipzig-233450) |
<!-- signals:end -->

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
| An open role names a framework (TISAX, ISO 27001, SOC 2, ISMS, GRC, NIS2, DORA, CISO) | 15 | Certification is under way *now* |
| 2+ distinct open security roles | 10 | A programme, not one vacancy |

The last two rules read the public job postings behind a company (the live signals below).
Postings say nothing about headcount or who to contact, so a real company found only
through a posting usually lands in tier B/C. That is deliberate: no score is invented for
data the pipeline doesn't have. Company size and contact enrichment is the next source.

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
- **Verified against a real HubSpot portal:** 5 companies + contacts synced, then the local
  `crm_sync` table wiped and everything synced again → every company matched to the same
  HubSpot id, zero duplicates. That run caught a real bug: a company with no domain (its
  contact used a hotmail address) was created twice; it is now matched by exact name.
- One-way CRM sync; HubSpot gets standard fields only (the score rides in `description`).
- The AE alert is its own job, so a Slack outage only delays it. A crash between sending and
  committing can still repeat one alert; a delivery-id dedupe on the receiving side would fix it.
- Synthetic data: realistic mess, but not real-world distributions.
