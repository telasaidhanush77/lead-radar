"""The benchmark. Every number in the README comes from this script.

    python -m bench.run                      # rules only, offline, ~1 min
    python -m bench.run --providers rules gemini groq   # + LLMs, keys in .env, paced

1. tune the fuzzy threshold on seed 1      2. report dedupe + throughput on held-out seed 2
3. re-run everything: assert 0 new alerts and 0 new CRM records
4. classifiers on the 60 hand-labelled descriptions: accuracy, latency, cost per 1k leads
"""

import argparse
import json
import logging
import os
import statistics
import subprocess
import threading
import time
from collections import Counter
from dataclasses import replace
from datetime import date
from math import comb
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.classify import RulesClassifier, content_hash
from app.config import Settings, get_settings
from app.db import migrate
from app.ingest import store_lead
from app.worker import drain, run_once
from bench.generate import demo_leads, generate
from bench.pricing import PRICES, cost_per_1k

HERE = Path(__file__).resolve().parent
THRESHOLDS = [80, 84, 86, 88, 90, 92, 95]
PACE_S = {"gemini": 4.0, "groq": 3.0}  # free-tier friendly: Groq free = 30 req/min, 8K tok/min
N_COMPANIES = 300
WORKERS = 4


# ---- metrics (pure) -----------------------------------------------------------------------
def pairwise_prf(pred: list, truth: list) -> tuple[float, float, float]:
    """Pairwise precision/recall/F1 for clustering. Two leads are a pair if they share an id.
    A None prediction (unassigned) pairs with nothing. O(n) by counting group sizes."""
    if len(pred) != len(truth):
        raise ValueError("pred and truth differ in length")

    def pairs(groups: Counter) -> int:
        return sum(comb(n, 2) for n in groups.values())

    true_pairs = pairs(Counter(truth))
    pred_pairs = pairs(Counter(p for p in pred if p is not None))
    correct = pairs(Counter((p, t) for p, t in zip(pred, truth, strict=True) if p is not None))
    precision = correct / pred_pairs if pred_pairs else 1.0
    recall = correct / true_pairs if true_pairs else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def micro_f1(pred: list[set], truth: list[set]) -> float:
    tp = sum(len(p & t) for p, t in zip(pred, truth, strict=True))
    fp = sum(len(p - t) for p, t in zip(pred, truth, strict=True))
    fn = sum(len(t - p) for p, t in zip(pred, truth, strict=True))
    return 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 1.0


# ---- pipeline runs ------------------------------------------------------------------------
def load_dotenv(path: Path = HERE.parent / ".env") -> None:
    """Minimal .env reader (KEY=VALUE lines) so API keys never go on the command line."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#"):
            os.environ.setdefault(key.strip(), value.split("#")[0].strip())


def reset(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        migrate(conn)


def drain_parallel(pool: ConnectionPool, settings: Settings, workers: int) -> None:
    """N workers on one queue; SKIP LOCKED hands each a different job. A worker that finds
    the queue empty keeps waiting while others still run, since they enqueue follow-ups."""
    def work() -> None:
        while True:
            if run_once(pool, settings):
                continue
            with pool.connection() as conn:
                busy = conn.execute("SELECT count(*) AS n FROM jobs "
                                    "WHERE status IN ('queued', 'running')").fetchone()["n"]
            if busy == 0:
                return
            time.sleep(0.02)

    threads = [threading.Thread(target=work) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def run_pipeline(pool: ConnectionPool, settings: Settings, leads: list[dict],
                 workers: int = WORKERS) -> float:
    """Ingest through the real store_lead path, then drain the queue. Returns seconds."""
    start = time.perf_counter()
    with pool.connection() as conn:
        for lead in leads:
            payload = {k: v for k, v in lead.items() if not k.startswith("_")}
            store_lead(conn, "bench", lead["_key"], payload)
    drain_parallel(pool, settings, workers)
    return time.perf_counter() - start


def predictions(pool: ConnectionPool, leads: list[dict]) -> list:
    with pool.connection() as conn:
        rows = conn.execute("SELECT r.idempotency_key AS k, c.company_id FROM raw_leads r "
                            "JOIN contacts c ON c.id = r.contact_id").fetchall()
    by_key = {r["k"]: r["company_id"] for r in rows}
    return [by_key.get(lead["_key"]) for lead in leads]


def dedupe_run(url: str, settings: Settings, leads: list[dict]) -> dict:
    reset(url)
    with ConnectionPool(url, kwargs={"row_factory": dict_row}, min_size=1,
                        max_size=WORKERS + 2) as pool:
        seconds = run_pipeline(pool, settings, leads)
        pred = predictions(pool, leads)
        with pool.connection() as conn:
            dead = conn.execute("SELECT count(*) AS n FROM jobs WHERE status='dead'"
                                ).fetchone()["n"]
    p, r, f1 = pairwise_prf(pred, [lead["_entity"] for lead in leads])
    return {"precision": p, "recall": r, "f1": f1, "seconds": seconds, "dead_jobs": dead,
            "leads": len(leads), "companies_true": len({lead["_entity"] for lead in leads}),
            "companies_found": len({x for x in pred if x is not None})}


def idempotency_check(url: str, settings: Settings) -> dict:
    """Reprocess every raw lead through the whole chain on the existing data."""
    with ConnectionPool(url, kwargs={"row_factory": dict_row}, min_size=1, max_size=4) as pool:
        def snapshot() -> dict:
            with pool.connection() as conn:
                return conn.execute(
                    "SELECT (SELECT count(*) FROM companies WHERE alerted_at IS NOT NULL)"
                    " AS alerts,"
                    " (SELECT count(*) FROM fake_crm) AS crm,"
                    " (SELECT count(*) FROM companies) AS companies").fetchone()
        before = snapshot()
        with pool.connection() as conn:
            conn.execute("INSERT INTO jobs (kind, payload) SELECT 'process_lead', "
                         "jsonb_build_object('raw_lead_id', id) FROM raw_leads")
        drain(pool, settings)
        after = snapshot()
    return {k: after[k] - before[k] for k in before}


# ---- classifiers --------------------------------------------------------------------------
def make_classifier(provider: str, settings: Settings):
    if provider == "rules":
        return RulesClassifier()
    from app.llm import make_llm_classifier

    return make_llm_classifier(replace(settings, llm_provider=provider))


def warm_demo_cache(provider: str, settings: Settings, fast: bool) -> list[dict]:
    """LLM answers for every distinct demo description, so the public demo (DEMO_MODE)
    replays real answers instead of falling back to rules. Not scored."""
    texts = sorted({lead["description"] for lead in demo_leads("x") if lead.get("description")})
    clf, cache = make_classifier(provider, settings), []
    for i, text in enumerate(texts):
        if i and not fast:
            time.sleep(PACE_S[provider])
        result = clf.classify(text)
        if not clf.last_was_fallback:
            cache.append({"provider": provider, "content_hash": content_hash(text),
                          "result": result.to_json(), "latency_ms": None})
    return cache


def rules_generated_accuracy(leads: list[dict]) -> float:
    """Rules scored on generator text: shown next to the hand-labelled score as the
    leakage check (same author wrote the templates and the keywords)."""
    rows = [lead for lead in leads if lead.get("description")]
    rules = RulesClassifier()
    hits = sum(rules.classify(r["description"]).industry == r["_labels"]["industry"]
               for r in rows)
    return hits / len(rows)


def classifier_run(provider: str, settings: Settings, rows: list[dict], fast: bool) -> dict:
    clf = make_classifier(provider, settings)
    preds, latencies, cache = [], [], []
    for i, row in enumerate(rows):
        if provider in PACE_S and i and not fast:
            time.sleep(PACE_S[provider])
        start = time.perf_counter()
        result = clf.classify(row["text"])
        latencies.append((time.perf_counter() - start) * 1000)
        preds.append(result)
        if provider != "rules" and not clf.last_was_fallback:
            cache.append({"provider": provider, "content_hash": content_hash(row["text"]),
                          "result": result.to_json(), "latency_ms": round(latencies[-1])})
    out = {
        "industry_accuracy": sum(p.industry == r["industry"] for p, r in zip(preds, rows,
                                                                             strict=True))
        / len(rows),
        "signal_f1": micro_f1([set(p.signals) for p in preds], [set(r["signals"]) for r in rows]),
        "p50_latency_ms": statistics.median(latencies),
        "fallbacks": getattr(clf, "fallbacks", 0),
        "cost_per_1k_usd": 0.0,
    }
    if provider in PRICES:
        price, calls = PRICES[provider], max(1, len(rows) - out["fallbacks"])
        out["model"] = price["model"]
        out["cost_per_1k_usd"] = cost_per_1k(price["in_per_m"], price["out_per_m"],
                                             clf.usage["in"] / calls, clf.usage["out"] / calls)
    return out | {"_cache": cache}


# ---- main ---------------------------------------------------------------------------------
def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, cwd=HERE.parent, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def summarize(results: dict) -> list[list[str]]:
    d, idem = results["dedupe"], results["idempotency"]
    rows = [
        ["Dedupe F1 (held-out seed 2)", f"{d['f1']:.3f}"],
        ["Dedupe precision / recall", f"{d['precision']:.3f} / {d['recall']:.3f}"],
        ["Pipeline throughput", f"{d['leads'] / d['seconds']:.0f} leads/s"],
        ["Re-run: new alerts / CRM records", f"{idem['alerts']} / {idem['crm']}"],
    ]
    for name, c in results["classifiers"].items():
        rows.append([f"{name} industry accuracy (hand-labelled)",
                     f"{c['industry_accuracy']:.0%}"])
    return rows


def markdown(results: dict) -> str:
    d = results["dedupe"]
    lines = [
        f"Measured {results['date']} at commit `{results['commit']}`. Fuzzy threshold "
        f"**{results['threshold']}** (tuned on seed 1); numbers below are held-out seed 2.",
        "",
        "| Pipeline | |", "|---|---|",
        f"| Leads in → true companies → found | {d['leads']} → {d['companies_true']} → "
        f"{d['companies_found']} |",
        f"| Dedupe precision / recall / F1 | {d['precision']:.3f} / {d['recall']:.3f} / "
        f"{d['f1']:.3f} |",
        f"| Throughput (ingest → CRM, {WORKERS} workers, laptop + Docker) | "
        f"{d['leads'] / d['seconds']:.0f} leads/s |",
        f"| Dead jobs | {d['dead_jobs']} |",
        f"| Re-run: new alerts / new CRM records / new companies | "
        f"{results['idempotency']['alerts']} / {results['idempotency']['crm']} / "
        f"{results['idempotency']['companies']} |",
        "",
        "| Classifier (60 hand-labelled) | Industry acc. | Signal F1 | p50 latency | "
        "Fallbacks | Cost / 1k leads* |",
        "|---|---|---|---|---|---|",
    ]
    for name, c in results["classifiers"].items():
        label = f"{name} (`{c['model']}`)" if "model" in c else name
        lines.append(f"| {label} | {c['industry_accuracy']:.0%} | {c['signal_f1']:.2f} | "
                     f"{c['p50_latency_ms']:.0f} ms | {c['fallbacks']} | "
                     f"${c['cost_per_1k_usd']:.3f} |")
    if "rules_generated_accuracy" in results:
        lines += ["", f"Leakage check: rules score {results['rules_generated_accuracy']:.0%} on "
                      "generated descriptions vs the hand-labelled figure above, so only the "
                      "hand-labelled number is reported."]
    lines += ["", "*At the provider's paid list price (see `bench/pricing.py`); "
                  "this project runs on free tiers, so actual spend is $0."]
    return "\n".join(lines)


def write_readme(table: str, path: Path = HERE.parent / "README.md") -> None:
    """Replace the block between the results markers, so README numbers are never hand-typed."""
    text = path.read_text(encoding="utf-8")
    start, end = "<!-- results:start -->", "<!-- results:end -->"
    head, _, rest = text.partition(start)
    _, _, tail = rest.partition(end)
    path.write_text(f"{head}{start}\n{table}\n{end}{tail}", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--providers", nargs="+", default=["rules"],
                        choices=["rules", "gemini", "groq"])
    parser.add_argument("--fast", action="store_true", help="no free-tier pacing")
    args = parser.parse_args()
    load_dotenv()
    logging.basicConfig(level=logging.WARNING)
    url = os.environ.get("BENCH_DATABASE_URL",
                         "postgresql://lead:lead@localhost:5433/lead_test")
    base = replace(get_settings(), database_url=url, llm_provider="rules", crm_provider="fake",
                   backoff_base_s=0, slack_webhook_url="", demo_mode=False)

    print("tuning threshold on seed 1 ...")
    seed1 = generate(1, N_COMPANIES)
    tuning = {t: dedupe_run(url, replace(base, fuzzy_threshold=t), seed1)["f1"]
              for t in THRESHOLDS}
    best = max(THRESHOLDS, key=lambda t: (round(tuning[t], 4), t))  # tie -> stricter
    print("  F1 by threshold:", {t: round(f, 3) for t, f in tuning.items()}, "-> best", best)

    print("held-out seed 2 ...")
    settings = replace(base, fuzzy_threshold=best)
    dedupe = dedupe_run(url, settings, generate(2, N_COMPANIES))
    idem = idempotency_check(url, settings)
    if any(idem.values()):
        raise SystemExit(f"IDEMPOTENCY FAILED: re-run changed {idem}")

    rows = [json.loads(line) for line in (HERE / "labelled.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    classifiers, cache = {}, []
    for provider in args.providers:
        print(f"classifier: {provider} ...")
        try:
            result = classifier_run(provider, settings, rows, args.fast)
        except ValueError as exc:  # missing key
            print(f"  skipped: {exc}")
            continue
        cache += result.pop("_cache")
        classifiers[provider] = result
        if provider != "rules":
            print(f"  warming demo cache for {provider} ...")
            cache += warm_demo_cache(provider, settings, args.fast)

    results = {"date": date.today().isoformat(), "commit": git_commit(), "threshold": best,
               "tuning_f1": tuning, "dedupe": dedupe, "idempotency": idem,
               "classifiers": classifiers,
               "rules_generated_accuracy": rules_generated_accuracy(generate(2, N_COMPANIES))}
    results["summary"] = summarize(results)
    (HERE / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    if cache:  # rewrite, de-duplicated, so re-runs don't grow the file
        path = HERE / "classify_cache.jsonl"
        old = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()
               if x.strip()] if path.exists() else []
        merged = {(r["provider"], r["content_hash"]): r for r in old + cache}
        path.write_text("".join(json.dumps(r) + "\n" for r in merged.values()),
                        encoding="utf-8")
    table = markdown(results)
    write_readme(table)
    print()
    print(table)


if __name__ == "__main__":
    main()
