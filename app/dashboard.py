"""The one server-rendered page. Every database value goes through esc() (html.escape)."""

import json
from html import escape
from pathlib import Path

import psycopg

from app.score import WEIGHTS

RESULTS = Path(__file__).resolve().parent.parent / "bench" / "results.json"
MAX_SCORE = sum(WEIGHTS.values())


def esc(value: object) -> str:
    return escape("" if value is None else str(value))


def _counts(conn: psycopg.Connection) -> dict:
    return conn.execute("""
        SELECT (SELECT count(*) FROM raw_leads)                              AS leads,
               (SELECT count(*) FROM companies)                              AS companies,
               (SELECT count(*) FROM scores)                                 AS scored,
               (SELECT count(*) FROM crm_sync)                               AS synced,
               (SELECT count(*) FROM contacts WHERE company_id IS NULL)      AS unassigned,
               (SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')) AS pending,
               (SELECT count(*) FROM jobs WHERE status = 'dead')             AS dead
    """).fetchone()


def _tiers(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT tier, count(*) AS n FROM scores GROUP BY tier").fetchall()
    return {"A": 0, "B": 0, "C": 0} | {r["tier"]: r["n"] for r in rows}


def _benchmark() -> list:
    try:
        return json.loads(RESULTS.read_text(encoding="utf-8")).get("summary", [])
    except (OSError, ValueError):
        return []


def _stage(label: str, key: str, value: int, note: str = "") -> str:
    return (f'<li class="stage"><span class="num" data-count="{key}">{value:,}</span>'
            f'<span class="lbl">{label}</span>{f"<span class=note>{note}</span>" if note else ""}'
            f"</li>")


def _top_rows(conn: psycopg.Connection) -> str:
    rows = conn.execute("""
        SELECT c.id, c.canonical_name, c.country, c.owner, s.total, s.breakdown
        FROM scores s JOIN companies c ON c.id = s.company_id
        WHERE s.tier = 'A' ORDER BY s.total DESC, c.id LIMIT 20
    """).fetchall()
    if not rows:
        return '<tr><td colspan="4" class="empty">No tier-A leads yet. Run the demo.</td></tr>'
    out = []
    for r in rows:
        chips = "".join(f'<span class="chip">{esc(rule)} <b>+{esc(pts)}</b></span>'
                        for rule, pts in r["breakdown"].items())
        width = round(100 * r["total"] / MAX_SCORE)
        out.append(
            f'<tr><td><div class="co"><a href="/companies/{esc(r["id"])}">'
            f'{esc(r["canonical_name"])}</a></div>'
            f'<div class="meta">{esc(r["country"] or "—")} · {esc(r["owner"])}</div></td>'
            f'<td class="score"><span class="mono">{esc(r["total"])}</span>'
            f'<span class="bar"><i style="width:{width}%"></i></span></td>'
            f'<td class="why">{chips}</td></tr>')
    return "".join(out)


def _signal_rows(conn: psycopg.Connection) -> str:
    from app.signals import signals_report

    rows = signals_report(conn, limit=10)
    if not rows:
        return ('<li class="empty">No live signals yet. Real companies hiring for '
                'ISO 27001 / TISAX / SOC 2 roles appear here after a signals run.</li>')
    out = []
    for r in rows:
        url = r["example_url"] or ""
        link = (f'<a href="{esc(url)}" rel="noopener nofollow" target="_blank">'
                f'{esc(r["example_title"])}</a>' if url.startswith("https://")
                else esc(r["example_title"]))
        out.append(
            f'<li class="signal"><div><div class="co"><a href="/companies/{esc(r["id"])}">'
            f'{esc(r["company"])}</a></div>'
            f'<div class="meta">{esc(r["country"] or "—")} · {esc(r["postings"])} '
            f'open role(s) · {link}</div></div>'
            f'<span class="mono sig-score">{esc(r["score"])}</span></li>')
    return "".join(out)


def _dead_rows(conn: psycopg.Connection) -> str:
    rows = conn.execute("SELECT id, kind, attempts, left(last_error, 200) AS err FROM jobs "
                        "WHERE status = 'dead' ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        return '<li class="empty">Nothing dead. Every job finished or is retrying.</li>'
    return "".join(
        f'<li class="dead"><div><span class="mono">#{esc(r["id"])}</span> {esc(r["kind"])} '
        f'<span class="meta">after {esc(r["attempts"])} tries</span>'
        f'<div class="err">{esc(r["err"])}</div></div>'
        f'<button class="btn small" data-retry="{esc(r["id"])}">Retry</button></li>'
        for r in rows)


def render_dashboard(conn: psycopg.Connection) -> str:
    c, t = _counts(conn), _tiers(conn)
    total_tiers = sum(t.values()) or 1
    tier_bar = "".join(f'<i class="t{k}" style="width:{100 * v / total_tiers:.1f}%"></i>'
                       for k, v in t.items())
    bench = "".join(f"<dt>{esc(k)}</dt><dd class='mono'>{esc(v)}</dd>" for k, v in _benchmark())
    bench_html = (f"<dl class='bench'>{bench}</dl>" if bench else
                  '<p class="empty">Run <code>python -m bench.run</code> to measure.</p>')
    return PAGE.format(
        stages="".join([
            _stage("leads in", "leads", c["leads"]),
            _stage("companies", "companies", c["companies"],
                   f'{c["unassigned"]:,} unassigned contacts'),
            _stage("scored", "scored", c["scored"]),
            _stage("synced to CRM", "synced", c["synced"]),
        ]),
        pending=c["pending"], dead=c["dead"], dead_class="bad" if c["dead"] else "",
        tier_bar=tier_bar,
        tier_a=t["A"], tier_b=t["B"], tier_c=t["C"],
        top_rows=_top_rows(conn), dead_rows=_dead_rows(conn), bench=bench_html,
        signal_rows=_signal_rows(conn),
    )


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lead Radar</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@500&family=IBM+Plex+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #f4f3ef; --surface: #ffffff; --ink: #121212; --muted: #6b6a66; --line: #dcdad3;
  --a: #ff4f00; --b: #2b2b2b; --c: #c9c6bd; --ok: #1f7a4d;
  --sans: "IBM Plex Sans", system-ui, sans-serif; --mono: "IBM Plex Mono", ui-monospace, monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #111110; --surface: #1a1a19; --ink: #eeeee8; --muted: #9a998f; --line: #2e2e2b;
          --b: #d9d7cf; --c: #4a4944; }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.45 var(--sans); }}
main {{ max-width: 1180px; margin: 0 auto; padding: 28px 16px 64px; }}
header {{ display: flex; flex-wrap: wrap; gap: 16px; align-items: end; justify-content: space-between;
         border-bottom: 2px solid var(--ink); padding-bottom: 14px; }}
h1 {{ font-size: clamp(28px, 5vw, 44px); line-height: 1; margin: 0; letter-spacing: -0.03em; }}
h1 small {{ display: block; font-size: 13px; font-weight: 400; color: var(--muted);
           letter-spacing: 0; margin-top: 8px; }}
h2 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .12em; color: var(--muted);
     margin: 0 0 12px; font-weight: 600; }}
.mono, .num {{ font-family: var(--mono); font-variant-numeric: tabular-nums; }}
.admin {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
input {{ font: inherit; padding: 8px 10px; border: 1px solid var(--line); background: var(--surface);
        color: var(--ink); width: 170px; }}
.btn {{ font: 600 14px var(--sans); padding: 9px 14px; border: 2px solid var(--ink);
       background: var(--ink); color: var(--bg); cursor: pointer; transition: transform .12s; }}
.btn:hover {{ transform: translateY(-1px); }} .btn:active {{ transform: translateY(1px); }}
.btn:focus-visible, input:focus-visible {{ outline: 3px solid var(--a); outline-offset: 2px; }}
.btn.small {{ padding: 5px 10px; font-size: 13px; background: transparent; color: var(--ink); }}
#msg {{ font-size: 13px; color: var(--muted); min-height: 1em; }}
.pipeline {{ list-style: none; padding: 0; margin: 28px 0; display: grid;
            grid-template-columns: repeat(4, 1fr); border: 1px solid var(--line); background: var(--surface); }}
.stage {{ padding: 18px 20px; border-right: 1px solid var(--line); position: relative; }}
.stage:last-child {{ border-right: 0; }}
.stage:not(:last-child)::after {{ content: "→"; position: absolute; right: -9px; top: 22px;
  background: var(--surface); color: var(--muted); font-size: 14px; }}
.num {{ display: block; font-size: clamp(26px, 4vw, 38px); line-height: 1.1; }}
.lbl {{ color: var(--muted); font-size: 13px; }} .note {{ display: block; font-size: 12px; color: var(--muted); }}
.status {{ display: flex; gap: 18px; font-size: 13px; color: var(--muted); margin: -14px 0 24px; }}
.status b {{ color: var(--ink); }} .status .bad b {{ color: var(--a); }}
.grid {{ display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 24px; align-items: start; }}
section {{ background: var(--surface); border: 1px solid var(--line); padding: 20px; }}
.side {{ display: grid; gap: 24px; }}
.tablewrap {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; }}
td {{ padding: 12px 8px; border-top: 1px solid var(--line); vertical-align: top; }}
.co {{ font-weight: 600; }} .meta {{ color: var(--muted); font-size: 13px; }}
.score {{ white-space: nowrap; width: 110px; }} .score .mono {{ font-size: 20px; color: var(--a); }}
.bar {{ display: block; height: 4px; background: var(--line); margin-top: 6px; }}
.bar i {{ display: block; height: 100%; background: var(--a); }}
.chip {{ display: inline-block; font-size: 12px; border: 1px solid var(--line); padding: 2px 7px;
        margin: 0 4px 4px 0; }}
.chip b {{ font-family: var(--mono); font-weight: 500; }}
.tiers {{ display: flex; height: 14px; margin-bottom: 12px; background: var(--line); }}
.tiers i {{ display: block; height: 100%; }} .tA {{ background: var(--a); }}
.tB {{ background: var(--b); }} .tC {{ background: var(--c); }}
.legend {{ display: flex; flex-wrap: wrap; gap: 6px 14px; justify-content: space-between; font-size: 13px; }}
.legend span::before {{ content: ""; display: inline-block; width: 9px; height: 9px; margin-right: 6px; }}
.lA::before {{ background: var(--a); }} .lB::before {{ background: var(--b); }} .lC::before {{ background: var(--c); }}
ul.deadlist {{ list-style: none; padding: 0; margin: 0; }}
.dead {{ display: flex; gap: 10px; justify-content: space-between; align-items: start;
        padding: 10px 0; border-top: 1px solid var(--line); }}
.err {{ font-family: var(--mono); font-size: 12px; color: var(--a); word-break: break-word; }}
.empty {{ color: var(--muted); font-size: 14px; }}
.live {{ border-color: var(--a); }}
.signal {{ display: flex; gap: 10px; justify-content: space-between; align-items: start;
          padding: 10px 0; border-top: 1px solid var(--line); }}
.signal a, .co a {{ color: var(--ink); }} .sig-score {{ color: var(--a); font-size: 18px; }}
.bench {{ display: grid; grid-template-columns: 1fr auto; gap: 6px 12px; margin: 0; font-size: 14px; }}
.bench dt {{ color: var(--muted); }} .bench dd {{ margin: 0; text-align: right; }}
code {{ font-family: var(--mono); font-size: 13px; }}
footer {{ margin-top: 32px; font-size: 13px; color: var(--muted); }}
@media (max-width: 820px) {{
  .grid {{ grid-template-columns: minmax(0, 1fr); }}
  .pipeline {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .stage:nth-child(2) {{ border-right: 0; }} .stage:nth-child(-n+2) {{ border-bottom: 1px solid var(--line); }}
  .stage::after {{ display: none; }}
}}
@media (max-width: 560px) {{
  tr {{ display: block; border-top: 1px solid var(--line); padding: 10px 0; }}
  td {{ display: block; border-top: 0; padding: 3px 0; }} .score {{ width: auto; }}
  .bar {{ max-width: 160px; }} section {{ padding: 16px; }} input {{ flex: 1; min-width: 0; }}
}}
@media (prefers-reduced-motion: reduce) {{ .btn {{ transition: none; }} }}
</style></head>
<body><main>
<header>
  <h1>Lead Radar<small>Inbound leads → deduped → scored on compliance need → routed → CRM</small></h1>
  <div class="admin">
    <input id="token" type="password" placeholder="admin token" aria-label="Admin token" autocomplete="off">
    <button class="btn" id="demo">Run demo</button>
    <span id="msg" role="status"></span>
  </div>
</header>
<ul class="pipeline" aria-label="Pipeline counts">{stages}</ul>
<p class="status"><span>in the queue <b class="mono">{pending}</b></span>
  <span class="{dead_class}">dead-letter <b class="mono">{dead}</b></span></p>
<div class="grid">
  <section aria-labelledby="top-h"><h2 id="top-h">Tier A — alert the AE</h2>
    <div class="tablewrap"><table><tbody>{top_rows}</tbody></table></div></section>
  <div class="side">
    <section aria-labelledby="sig-h" class="live"><h2 id="sig-h">Live hiring signals · real companies</h2>
      <ul class="deadlist">{signal_rows}</ul>
      <p class="meta">Postings via <a href="https://www.arbeitnow.com" rel="noopener">Arbeitnow</a>, refreshed daily.</p></section>
    <section aria-labelledby="tier-h"><h2 id="tier-h">Tier mix</h2>
      <div class="tiers" role="img" aria-label="A {tier_a}, B {tier_b}, C {tier_c}">{tier_bar}</div>
      <div class="legend"><span class="lA">A → AE <b class="mono">{tier_a}</b></span>
        <span class="lB">B → BDR <b class="mono">{tier_b}</b></span>
        <span class="lC">C → nurture <b class="mono">{tier_c}</b></span></div></section>
    <section aria-labelledby="bench-h"><h2 id="bench-h">Last benchmark</h2>{bench}</section>
    <section aria-labelledby="dead-h"><h2 id="dead-h">Dead-letter jobs</h2>
      <ul class="deadlist">{dead_rows}</ul></section>
  </div>
</div>
<footer>Synthetic data only. Built as a portfolio project for a GTM engineering role.</footer>
</main>
<script>
const token = document.getElementById("token"), msg = document.getElementById("msg");
try {{ token.value = sessionStorage.getItem("lr_token") || ""; }} catch (e) {{}}
token.addEventListener("input", () => {{ try {{ sessionStorage.setItem("lr_token", token.value); }} catch (e) {{}} }});
async function post(url) {{
  msg.textContent = "Working…";
  const r = await fetch(url, {{ method: "POST", headers: {{ Authorization: "Bearer " + token.value }} }});
  msg.textContent = r.ok ? "Done. Refreshing…" : r.status === 401 ? "Admin token required" : "Failed (" + r.status + ")";
  if (r.ok) setTimeout(() => location.reload(), 700);
}}
document.getElementById("demo").onclick = () => post("/demo/run");
document.querySelectorAll("[data-retry]").forEach(b => b.onclick = () => post("/jobs/" + b.dataset.retry + "/retry"));
if ({pending} > 0) setTimeout(() => {{ if (document.activeElement !== token) location.reload(); }}, 2500);
</script>
</body></html>
"""
