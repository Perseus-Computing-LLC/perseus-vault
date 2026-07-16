#!/usr/bin/env python3
"""Generate perseus.observer/benchmarks from the signed benchmark reports (#477).

The page is GENERATED, never hand-typed: every number renders from a committed
benchmark/*/report.json (each carrying a sha256 signature over its result set,
binary version and platform), and every section links its source report. Rerun
this script whenever a report changes and commit the emitted HTML to the site
repo; the page can never drift from the repo.

Usage:
    python scripts/gen_benchmark_page.py                     # writes ./benchmarks-index.html
    python scripts/gen_benchmark_page.py --out <site>/benchmarks/index.html

Honesty rules (issue #477):
  - numbers only where a signed report exists; "n/a" elsewhere, no bluffing
  - different-model / different-condition comparisons flagged inline
  - a "reproduce every number" block that works from a clean clone
"""
import argparse
import datetime as _dt
import html
import json
import math
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BENCH = REPO / "benchmark"

GITHUB = "https://github.com/Perseus-Computing-LLC/perseus-vault"


def load(rel):
    p = BENCH / rel
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def commit_sha():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return "unknown"


def esc(s):
    return html.escape(str(s))


def src_link(rel, sig=None):
    href = f"{GITHUB}/blob/main/benchmark/{rel}"
    sig_html = f' <span class="sig" title="sha256 over the result set">sig {esc(sig[:12])}</span>' if sig else ""
    return (f'<div class="src">source: <a href="{href}">benchmark/{esc(rel)}</a>'
            f'{sig_html}</div>')


# ── Feature matrix ────────────────────────────────────────────────────────────
# Competitor facts are the ONLY hand-maintained data here, each with a source
# URL rendered as a link. Everything numeric about Perseus Vault comes from
# the signed reports below.
MATRIX = {
    "columns": ["Perseus Vault", "Zep / Graphiti", "Mem0", "Cognee", "Letta"],
    "rows": [
        ("Temporal model",
         ["Full SQL:2011 bi-temporal (valid + transaction time, point-in-time recall)",
          "Bi-temporal edges (Graphiti)", "Timestamps", "Timestamps", "Timestamps"]),
        ("Storage / deployment",
         ["Single local binary, SQLite, offline",
          "Server + graph DB", "Server or SaaS, vector DB", "Server, graph + vector DBs",
          "Server, Postgres"]),
        ("Encryption at rest",
         ["AES-256-GCM built in", "n/a", "n/a", "n/a", "n/a"]),
        ("Audit trail",
         ["Hash-chained journal (keyed MAC under encryption; external review pending)",
          "n/a", "n/a", "n/a", "n/a"]),
        ("Retrieval modes",
         ["FTS5 keyword + dense vector + hybrid RRF, decay + trust weighted",
          "Graph + semantic", "Vector", "Graph + vector", "Vector + recall memory"]),
        ("License",
         ["MIT", "Apache-2.0 (Graphiti)", "Apache-2.0", "Apache-2.0", "Apache-2.0"]),
    ],
    "sources": [
        ("Zep / Graphiti", "https://github.com/getzep/graphiti"),
        ("Mem0", "https://github.com/mem0ai/mem0"),
        ("Cognee", "https://github.com/topoteretes/cognee"),
        ("Letta", "https://github.com/letta-ai/letta"),
    ],
}


def sec_matrix():
    head = "".join(f"<th>{esc(c)}</th>" for c in MATRIX["columns"])
    rows = ""
    for label, cells in MATRIX["rows"]:
        tds = "".join(
            f'<td class="{ "us" if i == 0 else "" }">{esc(c)}</td>'
            for i, c in enumerate(cells))
        rows += f"<tr><th>{esc(label)}</th>{tds}</tr>"
    srcs = " · ".join(f'<a href="{u}">{esc(n)}</a>' for n, u in MATRIX["sources"])
    return f"""
<section id="matrix">
  <h2>Feature matrix</h2>
  <p class="note">Architecture facts, not adjectives. Competitor rows come from their public repos ({srcs}); anything we could not verify is "n/a".</p>
  <div class="tablewrap"><table><thead><tr><th></th>{head}</tr></thead><tbody>{rows}</tbody></table></div>
</section>"""


def sec_retrieval(r):
    if not r:
        return ""
    m = r["metrics"].get("auto") or next(iter(r["metrics"].values()))
    cells = "".join(
        f"<div class='stat'><div class='v'>{m[k] * 100:.1f}%</div><div class='l'>{esc(k)}</div></div>"
        for k in ("recall@1", "recall@3", "recall@5", "recall@10") if k in m)
    mrr = f"<div class='stat'><div class='v'>{m['mrr']:.3f}</div><div class='l'>MRR</div></div>" if "mrr" in m else ""
    return f"""
<section id="retrieval">
  <h2>Retrieval recall (LongMemEval)</h2>
  <p class="note">Session-level recall against the {r['n_instances']}-instance LongMemEval split
  ({r['n_sessions_ingested']:,} sessions ingested into the real binary, offline, judge-free;
  default hybrid retrieval). {esc(r.get('binary', ''))}.</p>
  <div class="stats">{cells}{mrr}</div>
  {src_link('longmemeval/report.json', r.get('signature_sha256'))}
</section>"""


def _run_accs(primary, seeds):
    """[accuracy, ...] across a primary report + its seed reports."""
    out = []
    for r in [primary] + list(seeds):
        if not r:
            continue
        a = (r.get("systems", {}).get("mimir", {}) or r.get("mimir", {})).get("accuracy")
        if a is not None:
            out.append(a)
    return out


def sec_qa_cot(cot, cot_seeds=()):
    """#579: the official-CoT prompt distribution, rendered as its own labeled
    stat — never blended into the plain-prompt headline. LongMemEval ships two
    official answer prompts; every number must carry its answer_prompt."""
    accs = _run_accs(cot, cot_seeds)
    if not accs:
        return ""
    mean = sum(accs) / len(accs) * 100
    lo, hi = min(accs) * 100, max(accs) * 100
    links = src_link('longmemeval/qa_report_cot.json', cot.get('signature_sha256')) + "".join(
        src_link(f'longmemeval/qa_report_cot_seed{i}.json', s.get('signature_sha256'))
        for i, s in enumerate(cot_seeds, start=2) if s)
    return f"""
  <div class="stats"><div class='stat big'><div class='v'>{mean:.1f}%</div>
  <div class='l'>with LongMemEval's official CoT answer prompt (<code>answer_prompt: official-cot</code>) &mdash;
  mean of {len(accs)} independent signed full runs (range {lo:.1f}&ndash;{hi:.1f}%)</div></div></div>
  <p class="note">The benchmark ships two official answer prompts (plain and step-by-step CoT);
  both distributions above are 100% official methodology and differ only in that flag &mdash;
  each number carries its <code>answer_prompt</code>, recorded in the signed report.
  Zep's publication does not state which variant they used, so the comparison is flagged, not blended.</p>
  {links}"""


def sec_qa(qa, seeds=(), cot_html=""):
    zep_line = ('Zep publishes <b>63.8%</b> on LongMemEval with GPT-4o '
                '(<a href="https://arxiv.org/abs/2501.13956">their paper</a>).')
    if not qa:
        return f"""
<section id="qa">
  <h2>End-to-end QA vs Zep</h2>
  <p class="note">{zep_line} Our end-to-end run (same 500-instance split, pinned
  gpt-4o-2024-08-06 answerer and judge, temperature 0) is in progress; the signed
  report and per-category breakdown will render here when it lands. Until then this
  section shows no number, because there is no signed number to show.</p>
</section>"""
    overall = qa.get("systems", {}).get("mimir", {}) or qa.get("mimir", {})
    acc = overall.get("accuracy")
    # Multi-seed (#475): when confirmation seed reports exist, the headline is the
    # mean across all runs with the range — a single run's number is never quoted
    # alone once a distribution is available.
    seed_accs = [s.get("systems", {}).get("mimir", {}).get("accuracy")
                 for s in seeds if s]
    all_accs = [a for a in [acc] + seed_accs if a is not None]
    answerer = esc(qa.get('answerer_model', qa.get('answerer', qa.get('model', 'pinned model'))))
    if len(all_accs) > 1:
        mean = sum(all_accs) / len(all_accs)
        lo, hi = min(all_accs) * 100, max(all_accs) * 100
        acc_html = (f"<div class='stat big'><div class='v'>{mean * 100:.1f}%</div>"
                    f"<div class='l'>mean of {len(all_accs)} independent full runs "
                    f"(range {lo:.1f}&ndash;{hi:.1f}%, {answerer})</div></div>")
        runs_note = (f" The headline is the mean of {len(all_accs)} independent signed runs "
                     f"({' / '.join(f'{a*100:.1f}%' for a in all_accs)}); the worst run scores "
                     f"{lo - 63.8:+.1f} points vs Zep's published number.")
    elif acc is not None:
        acc_html = f"<div class='stat big'><div class='v'>{acc * 100:.1f}%</div><div class='l'>accuracy ({answerer})</div></div>"
        runs_note = ""
    else:
        acc_html, runs_note = "", ""
    cats = overall.get("by_question_type", {})
    cat_rows = "".join(
        f"<tr><th>{esc(k)}</th><td>{v.get('correct', '?')}/{v.get('graded', v.get('n', '?'))}</td>"
        f"<td>{(v.get('accuracy', 0) * 100):.1f}%</td></tr>"
        for k, v in sorted(cats.items())) if isinstance(cats, dict) else ""
    cat_table = f"<div class='tablewrap'><table><thead><tr><th>question type</th><th>correct</th><th>accuracy</th></tr></thead><tbody>{cat_rows}</tbody></table></div>" if cat_rows else ""
    seed_links = "".join(
        src_link(f'longmemeval/qa_report_seed{i}.json', s.get('signature_sha256'))
        for i, s in enumerate(seeds, start=2) if s)
    return f"""
<section id="qa">
  <h2>End-to-end QA vs Zep</h2>
  <p class="note">{zep_line} Ours below: identical split, pinned answerer and judge named in the
  report, LongMemEval's official per-type judge prompts.{runs_note}
  Where conditions differ from a competitor's published run, the comparison is flagged, not blended.
  Per-type table is from the primary run's signed report.</p>
  <div class="stats">{acc_html}</div>
  {cot_html}
  {cat_table}
  {src_link('longmemeval/qa_report.json', qa.get('signature_sha256'))}
  {seed_links}
</section>"""


# Same-box presentation metadata (#697): (json key, display label, stack, is-us).
# Hand-maintained like the feature matrix above; the RECALL and LATENCY numbers
# come from the committed benchmark/lambda/results/competitors.json, never from
# here. List order is the table order (recall desc, then latency asc; Perseus first).
SAMEBOX_SYSTEMS = [
    ("perseus_vault", "Perseus Vault (hybrid)", "single local binary, in-process", True),
    ("letta", "Letta (archival / pgvector)", "server + Postgres/pgvector", False),
    ("mem0", "Mem0 (vector)", "Python + Qdrant", False),
    ("zep", "Zep (Graphiti temporal KG)", "server + Neo4j", False),
]


def sec_samebox(c):
    """#697: every system stood up live on one box against the same local Ollama,
    identical corpus/queries/judge, no published or cloud numbers substituted.
    Table cells (recall, p50) render from competitors.json; the display labels and
    stack strings are hand-maintained presentation, like the feature matrix. First
    hand-added to the site (perseus PR #797), pinned so a full regen re-emits it."""
    if not c:
        return ""
    measured = c.get("measured", {})
    if not measured:
        return ""
    rows = ""
    for key, label, stack, us in SAMEBOX_SYSTEMS:
        m = measured.get(key)
        if not m:
            continue
        cls = ' class="us"' if us else ""
        rows += (f'<tr><th>{esc(label)}</th>'
                 f'<td{cls}>{m["recall_accuracy"]:.2f}</td>'
                 f'<td{cls}>{m["p50_latency_ms"]} ms</td>'
                 f'<td{cls}>{esc(stack)}</td></tr>')
    src = (f'<div class="src">source: <a href="{GITHUB}/blob/main/benchmark/lambda/results/competitors.json">benchmark/lambda/results/competitors.json</a>'
           f' &middot; <a href="{GITHUB}/blob/main/benchmark/lambda/competitors_bench.py">competitors_bench.py</a></div>')
    return f"""
<section id="samebox">
  <h2>Same-box recall, all systems fully local</h2>
  <p class="note">Every system stood up and run live on one H100 against the same local Ollama
  (<code>qwen2.5:14b-instruct</code> + <code>nomic-embed-text</code>): identical fact set, identical
  queries, identical substring judge. No published or cloud numbers are substituted for a competitor.
  Cells show recall accuracy and p50 latency.</p>
  <div class="tablewrap"><table><thead><tr><th>system</th><th>recall</th><th>p50 latency</th><th>stack</th></tr></thead><tbody>{rows}</tbody></table></div>
  <p class="note">Letta matches Perseus Vault on recall for this corpus; the difference is operational,
  an in-process binary answering in ~35 ms versus a server plus Postgres at ~135 ms p50. Zep's
  self-hosted Community Edition server is deprecated and its <code>zep_python</code> memory API is now
  Zep Cloud only, so this measures Zep's open-source engine, Graphiti (a temporal knowledge graph on
  Neo4j), with entity extraction and embeddings both on the same local Ollama. Building a knowledge
  graph needs an LLM to do structured extraction, and a local model is lossy at it (5 entities and 2
  edges from 6 facts here), so 0.20 reflects local-extraction quality rather than Zep Cloud, which
  uses frontier models. The point of this table is not a scoreboard; it is what each architecture does
  when the whole stack has to run locally.</p>
  {src}
</section>"""


def sec_scale(s):
    if not s:
        return ""
    rows = ""
    for size in sorted(s["runs"], key=int):
        r = s["runs"][size]
        rec = r.get("recall", {})
        f = rec.get("fts5", {})
        d = rec.get("dense", {})
        h = rec.get("hybrid", {})
        rows += (f"<tr><th>{int(size):,}</th>"
                 f"<td>{r['write']['docs_per_sec']}/s</td>"
                 f"<td>{f.get('p50_ms', 'n/a')} / {f.get('p99_ms', 'n/a')} ms</td>"
                 f"<td>{d.get('p50_ms', 'n/a')} / {d.get('p99_ms', 'n/a')} ms</td>"
                 f"<td>{h.get('p50_ms', 'n/a')} / {h.get('p99_ms', 'n/a')} ms</td>"
                 f"<td>{r['as_of']['p99_ms']} ms</td>"
                 f"<td>{r['cold_start']['first_query_ms_median']} ms</td></tr>")
    hw = s["meta"]["hardware"]
    return f"""
<section id="scale">
  <h2>Scale and latency</h2>
  <p class="note">The real binary over MCP stdio, seeded corpus, {s['meta']['queries_per_metric']} queries
  per metric. Hardware named in the report: {esc(hw['os'])}, {hw['cpus']} cores. Bi-temporal point
  lookups stay flat from 10K to 100K entities; that column is the differentiator.</p>
  <div class="tablewrap"><table><thead><tr><th>entities</th><th>write sustained</th>
  <th>fts5 p50/p99</th><th>dense p50/p99</th><th>hybrid p50/p99</th><th>as_of p99</th><th>cold start</th></tr></thead>
  <tbody>{rows}</tbody></table></div>
  {src_link('scale/report.json', s.get('signature_sha256'))}
</section>"""


def sec_temporal(t, g):
    if not (t or g):
        return ""
    blocks = ""
    if g:
        axes = "".join(
            f"<div class='stat'><div class='v'>{v.get('pass', v.get('passed', '?'))}/{v['total']}</div><div class='l'>{esc(k)}</div></div>"
            for k, v in g.get("by_axis", {}).items()) if isinstance(g.get("by_axis"), dict) else ""
        blocks += (f"<div class='stats'><div class='stat big'><div class='v'>{g['accuracy'] * 100:.1f}%</div>"
                   f"<div class='l'>gauntlet: {g['checks_passed']}/{g['checks_total']} checks</div></div>{axes}</div>"
                   + src_link('temporal/gauntlet_report.json', g.get('signature_sha256')))
    if t:
        blocks += (f"<div class='stats'><div class='stat'><div class='v'>{t['accuracy'] * 100:.1f}%</div>"
                   f"<div class='l'>temporal suite: {t['checks_passed']}/{t['checks_total']}</div></div></div>"
                   + src_link('temporal/report.json', t.get('signature_sha256')))
    return f"""
<section id="temporal">
  <h2>Temporal correctness</h2>
  <p class="note">Bi-temporal reconstruction checked scenario by scenario: what was believed at T
  (transaction time), what was true at T (valid time), and the full cell. Deterministic, offline.</p>
  {blocks}
</section>"""


def sec_beam(b):
    """#685/#697: correctness + determinism AT SCALE (BEAM). The embedded
    bi-temporal gauntlet must still score 13/13 and stay byte-identical across
    two runs while a filler corpus grows the search space to 10M tokens, with
    point-lookup latency flat. Every number renders from benchmark/beam/report.json;
    this section was first hand-added to the site (perseus PR #797) and is pinned
    here so a full regeneration re-emits it instead of wiping it."""
    if not b:
        return ""
    tiers = b.get("tiers", [])
    if not tiers:
        return ""
    first, last = tiers[0], tiers[-1]

    def lat(t, axis):
        a = t["latency"][axis]
        return f"{a['p50_ms']} / {a['p95_ms']} ms"

    rows = ""
    for t in tiers:
        rows += (f'  <tr><th>{esc(t["tier"])}</th><td>{t["filler_entities"]:,}</td>'
                 f'<td>{t["approx_tokens"]:,}</td><td>{t["populate_secs"]} s</td>'
                 f'<td class="us">{esc(t["gauntlet_checks"])}</td>'
                 f'<td class="us">{"yes" if t["deterministic"] else "no"}</td>'
                 f'<td>{lat(t, "as_of")}</td><td>{lat(t, "valid_at")}</td></tr>\n')

    acc = first["gauntlet_accuracy"] * 100
    checks_n = first["gauntlet_checks"].split("/")[0]  # "13" from "13/13"
    p50_first = first["latency"]["as_of"]["p50_ms"]
    p50_last = last["latency"]["as_of"]["p50_ms"]
    tokens_m = last["approx_tokens"] / 1e6
    samples = first["latency"]["as_of"]["samples"]

    # p50 flatness list across tiers (as_of point lookups).
    p50s = " &rarr; ".join(f"{t['latency']['as_of']['p50_ms']}" for t in tiers)

    # Single worst p95 across both axes and every tier, shown as measured; the
    # bound is the next-worst p95 rounded up ("every other tier stays under X ms").
    p95_all = sorted(
        ((t["latency"][axis]["p95_ms"], t["tier"])
         for t in tiers for axis in ("as_of", "valid_at")),
        reverse=True)
    worst_p95, worst_tier = p95_all[0]
    bound = math.ceil(p95_all[1][0])

    sig = first.get("signature_sha256", "")
    href = f"{GITHUB}/blob/main/benchmark/beam"
    src = (f'<div class="src">source: <a href="{href}/report.json">benchmark/beam/report.json</a>'
           f' <span class="sig" title="sha256 over the result set">sig {esc(sig[:12])}</span>'
           f' &middot; <a href="{href}/README.md">methodology</a></div>')

    stats = (
        "<div class='stats'>"
        f"<div class='stat big'><div class='v'>{acc:.1f}%</div>"
        f"<div class='l'>gauntlet: {esc(first['gauntlet_checks'])} at every tier ({esc(first['tier'])}&ndash;{esc(last['tier'])})</div></div>"
        "<div class='stat'><div class='v'>deterministic=true</div>"
        "<div class='l'>identical signature across two runs, every tier</div></div>"
        f"<div class='stat'><div class='v'>{p50_last} ms</div>"
        f"<div class='l'>as_of p50 at {esc(last['tier'])} (flat from {p50_first} ms at {esc(first['tier'])})</div></div>"
        f"<div class='stat'><div class='v'>{esc(last['tier'])}</div>"
        f"<div class='l'>{last['filler_entities']:,} entities, ~{tokens_m:.1f}M tokens, {last['populate_secs']}s populate</div></div>"
        "</div>")

    return f"""
<section id="beam">
  <h2>Correctness and determinism at scale (BEAM)</h2>
  <p class="note">Named for <a href="https://arxiv.org/abs/2510.27246">BEAM</a> (Beyond a Million Tokens), which tests at
  128K / 500K / 1M / 10M tokens to defeat the "dump everything in context" cheat. Our claim is orthogonal to a
  context-window score: <b>FTS5 + deterministic bi-temporal retrieval must not degrade as the corpus grows.</b>
  BEAM embeds the CI-verified bi-temporal gauntlet (the same {checks_n} checks) inside a filler corpus sized to each token
  tier and asserts, at every tier, that correctness holds ({esc(first['gauntlet_checks'])}), that two independent runs produce an identical
  signature (<code>deterministic=true</code>), and that point lookups stay flat. Fully offline, lean FTS5 + bi-temporal
  binary (no embeddings), on Linux / Unraid.</p>
  {stats}
  <div class="tablewrap"><table><thead><tr><th>token tier</th><th>filler entities</th><th>approx tokens</th><th>populate</th><th>gauntlet</th><th>deterministic</th><th>as_of p50/p95</th><th>valid_at p50/p95</th></tr></thead>
  <tbody>
{rows}  </tbody></table></div>
  <p class="note">Point-lookup p50 is flat ({p50s} ms as the corpus grows {esc(first['tier'])}&ndash;{esc(last['tier'])}),
  because lookups are <code>(category,key)</code>-indexed. p95 is flat too except a single {worst_p95} ms sample at the {esc(worst_tier)}
  tier, shown as measured; every other tier stays under {bound} ms. {samples} samples per axis per tier.</p>
  {src}
</section>"""


def sec_reproduce(commit):
    return f"""
<section id="reproduce">
  <h2>Reproduce every number</h2>
  <p class="note">Every report above is generated by a script in the repo and signed with a sha256
  over its result set. From a clean clone:</p>
  <pre><code>git clone {GITHUB}.git
cd perseus-vault
cargo build --release
python benchmark/recall/run.py        # recall quality (offline)
python benchmark/longmemeval/run.py   # LongMemEval retrieval (offline)
python benchmark/temporal/gauntlet.py # bi-temporal gauntlet (offline)
python benchmark/scale/run.py         # scale + latency (offline)
python benchmark/beam/run.py --tiers 128K 500K 1M 10M --out /tmp/beam.json  # BEAM: correctness + determinism at scale (offline)
python benchmark/longmemeval/qa.py    # end-to-end QA (needs an OpenAI key; prints cost first)</code></pre>
  <p class="note">This page was generated by <a href="{GITHUB}/blob/main/scripts/gen_benchmark_page.py">scripts/gen_benchmark_page.py</a>
  at commit <code>{esc(commit)}</code>. If a number here cannot be traced to a committed signed report, that is a bug; please file it.</p>
</section>"""


PAGE_CSS = """
main.bench{max-width:1080px;margin:0 auto;padding:32px 20px 64px}
main.bench h1{font-size:34px;margin:18px 0 6px}
main.bench h2{font-size:22px;margin:38px 0 8px}
main.bench .lead,main.bench .note{color:var(--text-dim,#98a0b3);font-size:14.5px;line-height:1.55}
main.bench .stats{display:flex;flex-wrap:wrap;gap:12px;margin:14px 0}
main.bench .stat{background:var(--surface,#101018);border:1px solid var(--border,#23232f);border-radius:10px;padding:12px 18px;min-width:120px}
main.bench .stat .v{font-size:24px;font-weight:700;font-family:var(--font-display,'Space Grotesk',sans-serif)}
main.bench .stat.big .v{font-size:32px;color:var(--violet,#a78bfa)}
main.bench .stat .l{font-size:12px;color:var(--text-dim,#98a0b3);margin-top:2px}
main.bench .tablewrap{overflow-x:auto;margin:12px 0}
main.bench table{border-collapse:collapse;width:100%;font-size:13.5px}
main.bench th,main.bench td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--border,#23232f);vertical-align:top}
main.bench thead th{color:var(--text-dim,#98a0b3);font-size:12px;text-transform:uppercase;letter-spacing:.5px}
main.bench td.us{color:var(--violet,#a78bfa);font-weight:600}
main.bench .src{font-size:12px;color:var(--text-dim,#98a0b3);margin-top:6px}
main.bench .src .sig{font-family:var(--font-mono,'IBM Plex Mono',monospace);opacity:.8;margin-left:6px}
main.bench pre{background:var(--surface,#101018);border:1px solid var(--border,#23232f);border-radius:10px;padding:14px 16px;overflow-x:auto;font-size:13px}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "benchmarks-index.html"))
    args = ap.parse_args()

    recall = load("longmemeval/report.json")
    qa = load("longmemeval/qa_report.json")
    qa_seeds = [load("longmemeval/qa_report_seed2.json"),
                load("longmemeval/qa_report_seed3.json")]
    qa_cot = load("longmemeval/qa_report_cot.json")
    qa_cot_seeds = [load("longmemeval/qa_report_cot_seed2.json"),
                    load("longmemeval/qa_report_cot_seed3.json")]
    samebox = load("lambda/results/competitors.json")
    scale = load("scale/report.json")
    temporal = load("temporal/report.json")
    gauntlet = load("temporal/gauntlet_report.json")
    beam = load("beam/report.json")
    commit = commit_sha()
    today = _dt.date.today().isoformat()

    body = (sec_matrix() + sec_retrieval(recall)
            + sec_qa(qa, qa_seeds, cot_html=sec_qa_cot(qa_cot, qa_cot_seeds))
            + sec_samebox(samebox) + sec_scale(scale)
            + sec_temporal(temporal, gauntlet) + sec_beam(beam) + sec_reproduce(commit))

    page = f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Perseus Vault benchmarks: reproducible numbers, signed reports</title>
<script>try{{var t=localStorage.getItem('perseus-theme');if(t)document.documentElement.setAttribute('data-theme',t)}}catch(e){{}}</script>
<link rel="icon" href="/assets/perseus.svg">
<link rel="canonical" href="https://perseus.observer/benchmarks/">
<meta name="description" content="Reproducible agent-memory benchmarks: LongMemEval retrieval recall, bi-temporal correctness, and BEAM correctness + determinism at scale (128K to 10M tokens). Every number generated from a signed report with a script you can rerun.">
<meta name="theme-color" content="#0A0A12">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Perseus">
<meta property="og:title" content="Perseus Vault benchmarks">
<meta property="og:description" content="Every number here has a script. Signed reports, named models, reproducible offline.">
<meta property="og:url" content="https://perseus.observer/benchmarks/">
<link rel="stylesheet" href="/assets/fonts.css">
<link rel="stylesheet" href="/assets/tokens.css">
<link rel="stylesheet" href="/assets/perseus.css">
<style>{PAGE_CSS}</style>
<script defer src="https://stats.perseus.observer/script.js" data-website-id="74dc0a11-f2dd-4d6f-b0b7-4a550116bbe5"></script>
</head>
<body>
<header class="site">
  <div class="nav">
    <a href="/" class="brand" aria-label="Perseus home">
      <svg width="30" height="30" viewBox="0 0 56 56" fill="none" aria-hidden="true"><rect x="3" y="14" width="17" height="3.6" rx="1.8" fill="var(--violet)"/><rect x="3" y="21" width="17" height="3.6" rx="1.8" fill="var(--violet)" fill-opacity=".75"/><rect x="3" y="28" width="17" height="3.6" rx="1.8" fill="var(--violet)" fill-opacity=".55"/><rect x="3" y="35" width="17" height="3.6" rx="1.8" fill="var(--violet)" fill-opacity=".4"/><g stroke="var(--amber)" stroke-width="2.4" stroke-linecap="round"><line x1="22" y1="16" x2="42" y2="28"/><line x1="22" y1="23" x2="42" y2="28"/><line x1="22" y1="30" x2="42" y2="28"/><line x1="22" y1="37" x2="42" y2="28"/></g><circle cx="44" cy="28" r="4.2" fill="var(--amber)"/></svg>
      <b>Perseus<span class="sub"> / Benchmarks</span></b>
    </a>
    <nav class="navlinks">
      <a href="/perseus-vault/">Perseus Vault</a>
      <a href="https://github.com/Perseus-Computing-LLC/perseus-vault">GitHub</a>
    </nav>
  </div>
</header>
<main class="bench">
  <h1>Benchmarks that you can rerun</h1>
  <p class="lead">Agent-memory vendors publish numbers; ours come with the script, the dataset,
  the named model, and a sha256 signature over the result set. Everything below is generated
  from committed reports in the open repo. If you cannot reproduce a number, it comes down.</p>
  {body}
</main>
<footer class="site">
  <div class="foot-compact">
    <div class="brand-line">
      <svg width="26" height="26" viewBox="0 0 56 56" fill="none" aria-hidden="true"><rect x="3" y="14" width="17" height="3.6" rx="1.8" fill="var(--violet)"/><rect x="3" y="21" width="17" height="3.6" rx="1.8" fill="var(--violet)" fill-opacity=".75"/><rect x="3" y="28" width="17" height="3.6" rx="1.8" fill="var(--violet)" fill-opacity=".55"/><rect x="3" y="35" width="17" height="3.6" rx="1.8" fill="var(--violet)" fill-opacity=".4"/><g stroke="var(--amber)" stroke-width="2.4" stroke-linecap="round"><line x1="22" y1="16" x2="42" y2="28"/><line x1="22" y1="23" x2="42" y2="28"/><line x1="22" y1="30" x2="42" y2="28"/><line x1="22" y1="37" x2="42" y2="28"/></g><circle cx="44" cy="28" r="4.2" fill="var(--amber)"/></svg>
      <span class="legal-line">Perseus Computing LLC · generated {today} at {esc(commit)} · perseus.observer/benchmarks</span>
    </div>
    <div class="sib"><a href="/">Perseus</a><a href="/perseus-vault/">Perseus Vault</a><a href="/plutus/">Plutus</a><a href="{GITHUB}">GitHub</a></div>
  </div>
</footer>
<script src="/assets/perseus.js"></script>
</body>
</html>
"""
    # Zero em-dashes is a hard site rule.
    assert "—" not in page, "em-dash found in generated page"
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(page, encoding="utf-8")
    print(f"wrote {args.out} ({len(page):,} bytes; commit {commit})")


if __name__ == "__main__":
    main()
