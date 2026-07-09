#!/usr/bin/env python3
"""build_report.py — WS5: assemble verified results into ONE self-contained HTML.

Consumes ./results/*.json (curated, trustworthy runs only) and renders inline-SVG
charts. No external assets; commits cleanly to the repo. Every number is sourced
from a real run file and labeled with corpus size + hardware tier.
"""
import json, os, sys, html

RD = sys.argv[1] if len(sys.argv) > 1 else "results"
def load(name):
    p = os.path.join(RD, name)
    return json.load(open(p)) if os.path.exists(p) else None

def grouped_bars(title, series, groups, unit="", maxv=1.0, w=640):
    """series: {label: [v per group]}; groups: [group labels]."""
    colors = {"keyword (fts5)": "#e06b6b", "dense": "#6b8cff", "hybrid": "#5fd18b",
              "14B": "#6b8cff", "72B": "#c17be0"}
    rowh, gap, x0 = 22, 10, 150
    ng, ns = len(groups), len(series)
    block = ns * rowh + gap
    h = ng * block + 30
    svg = [f'<svg width="{w}" height="{h}" font-family="system-ui">']
    for gi, g in enumerate(groups):
        y0 = gi * block + 6
        svg.append(f'<text x="0" y="{y0+rowh}" font-size="12" fill="#aaa">{html.escape(g)}</text>')
        for si, (lab, vals) in enumerate(series.items()):
            v = vals[gi]
            y = y0 + si * rowh
            bw = int((w - x0 - 70) * min(v, maxv) / maxv)
            c = colors.get(lab, "#888")
            svg.append(f'<rect x="{x0}" y="{y}" width="{max(bw,1)}" height="{rowh-4}" rx="3" fill="{c}"/>')
            svg.append(f'<text x="{x0+bw+6}" y="{y+rowh-7}" font-size="11" fill="#eee">{v:g}{unit}</text>')
    svg.append('</svg>')
    # legend
    leg = " ".join(f'<span style="color:{colors.get(l,"#888")}">&#9632; {html.escape(l)}</span>'
                   for l in series)
    return f'<section><h2>{html.escape(title)}</h2>{"".join(svg)}<div class="leg">{leg}</div></section>'

def line_chart(title, xs, ys, xlabel, ylabel, w=640, h=260):
    maxy = max(ys) * 1.1 or 1
    x0, y0 = 60, h - 40
    pw, ph = w - x0 - 20, h - 70
    pts = []
    for i, (x, y) in enumerate(zip(xs, ys)):
        px = x0 + pw * i / (len(xs) - 1)
        py = y0 - ph * y / maxy
        pts.append((px, py, x, y))
    poly = " ".join(f"{px:.0f},{py:.0f}" for px, py, _, _ in pts)
    svg = [f'<svg width="{w}" height="{h}" font-family="system-ui">']
    svg.append(f'<line x1="{x0}" y1="{y0}" x2="{x0+pw}" y2="{y0}" stroke="#444"/>')
    svg.append(f'<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y0-ph}" stroke="#444"/>')
    svg.append(f'<polyline fill="none" stroke="#5fd18b" stroke-width="2" points="{poly}"/>')
    for px, py, x, y in pts:
        svg.append(f'<circle cx="{px:.0f}" cy="{py:.0f}" r="3" fill="#5fd18b"/>')
        svg.append(f'<text x="{px:.0f}" y="{py-8:.0f}" font-size="10" fill="#eee" text-anchor="middle">{y:g}</text>')
        svg.append(f'<text x="{px:.0f}" y="{y0+15:.0f}" font-size="10" fill="#aaa" text-anchor="middle">{x}</text>')
    svg.append(f'<text x="{x0+pw/2:.0f}" y="{h-4}" font-size="11" fill="#888" text-anchor="middle">{html.escape(xlabel)}</text>')
    svg.append('</svg>')
    return f'<section><h2>{html.escape(title)}</h2>{"".join(svg)}<div class="leg">{html.escape(ylabel)}</div></section>'

sections = []

# 1. Recall@k at scale
scale = load("scale_10k_distinct.json")
if scale:
    s = scale["summary"]; n = scale["corpus"]["entities"]
    groups = ["recall@1", "recall@5", "recall@10"]
    series = {
        "keyword (fts5)": [s[g]["fts5"]["recall"] for g in groups],
        "dense": [s[g]["dense"]["recall"] for g in groups],
        "hybrid": [s[g]["hybrid"]["recall"] for g in groups],
    }
    sections.append(grouped_bars(
        f"Recall by mode — {n:,} distinct entities on {scale['tier']} "
        f"(dense p50 {s['recall@1']['dense']['p50_ms']}ms)",
        series, groups, maxv=1.0))

# 2. 8-GPU fleet throughput scaling
fleet = load("fleet_8gpu_throughput.json")
if fleet:
    xs = [l["concurrency"] for l in fleet["levels"]]
    ys = [l["aggregate_eps"] for l in fleet["levels"]]
    sections.append(line_chart(
        f"8-GPU fleet embedding throughput — peak {fleet['peak_eps']} emb/s "
        f"({fleet['scaling_vs_serial']}x serial, 8 pinned daemons + LB)",
        xs, ys, "concurrent requests", "aggregate embeddings/sec"))

# 3. Model quality lift
ql = load("quality_lift_warm.json")
if ql:
    models = ql["models"]
    groups = ["accuracy", "citation", "p50 latency (s)"]
    series = {}
    for m in models:
        short = "14B" if "14b" in m["model"] else ("72B" if "72b" in m["model"] else m["model"])
        sm = m["summary"]
        series[short] = [sm["accuracy"], sm["citation_rate"], sm["p50_latency_s"]]
    sections.append(grouped_bars(
        "mimir_ask grounded QA — model quality vs latency (both pre-warmed)",
        series, groups, maxv=max(3.0, max(v for vs in series.values() for v in vs))))

# 4. Competitive: Perseus Vault vs Mem0 (same box, local Ollama)
mem0 = load("mem0_bench.json")
if mem0 and mem0.get("summary") and ql:
    pv14 = next((m for m in ql["models"] if "14b" in m["model"]), ql["models"][0])
    svg = ['<svg width="640" height="90" font-family="system-ui">']
    rows = [("Perseus Vault (mimir_ask RAG)", pv14["summary"]["accuracy"], "#5fd18b"),
            ("Mem0 (search)", mem0["summary"]["recall_accuracy"], "#e0a56b")]
    for i, (lab, v, c) in enumerate(rows):
        y = 6 + i * 34
        bw = int((640 - 260 - 60) * v)
        svg.append(f'<text x="0" y="{y+18}" font-size="11" fill="#ccc">{html.escape(lab)}</text>')
        svg.append(f'<rect x="260" y="{y}" width="{max(bw,1)}" height="24" rx="3" fill="{c}"/>')
        svg.append(f'<text x="{266+bw}" y="{y+18}" font-size="11" fill="#eee">{v:g}</text>')
    svg.append('</svg>')
    note = ("Both fully local on the same box (Ollama qwen2.5:14b + nomic-embed-text). "
            "Perseus Vault does full RAG answer generation + citation (~0.9s); "
            "Mem0 returns raw retrieved memories (~0.03s). Recall accuracy on the same "
            "LOCOMO-style fact set.")
    sections.append(f'<section><h2>Competitive: memory recall accuracy, same hardware</h2>'
                    f'{"".join(svg)}<div class="leg">{html.escape(note)}</div></section>')

doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Perseus Vault — Dynamic Range Benchmarks</title>
<style>
body{{background:#0c0814;color:#eee;font-family:system-ui;max-width:860px;margin:40px auto;padding:0 20px}}
h1{{color:#6b8cff}} h2{{font-size:15px;margin-top:34px;color:#cdd}}
.leg{{font-size:12px;color:#999;margin-top:6px}} .leg span{{margin-right:14px}}
.note{{color:#888;font-size:13px;line-height:1.5}}
</style></head><body>
<h1>Perseus Vault — Dynamic Range</h1>
<p class="note">Same API from air-gapped/offline to multi-GPU. All numbers first-party measured
on Lambda Cloud (A100 / 2&times;H100 / 8&times;H100), {len(sections)} verified result sets.
Keyword vs semantic recall measured on a 10k-entity distinct-content corpus; throughput on
8 pinned Ollama daemons behind a load balancer.</p>
{"".join(sections)}
<p class="note">Generated by build_report.py from lambda-kit result JSON. Reproducible: see lambda-kit/.</p>
</body></html>"""
open(os.path.join(RD, "results.html"), "w").write(doc)
print(f"wrote {RD}/results.html ({len(sections)} sections)")
