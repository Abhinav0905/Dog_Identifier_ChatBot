"""Build a local, linked review of captured UI answers and server traces."""
from collections import Counter
from pathlib import Path
import argparse
import html
import json

STYLE = """body{font:16px/1.55 system-ui,sans-serif;max-width:1180px;margin:40px auto;padding:0 24px;color:#172b36;background:#f7f9fa}h1{font-size:30px}h2{font-size:21px;margin-top:32px}table{border-collapse:collapse;width:100%;background:white}td,th{padding:12px;border-bottom:1px solid #dbe3e8;text-align:left;vertical-align:top}a{color:#075f89}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef3f6;padding:14px;font-size:13px}details{border:1px solid #dbe3e8;border-radius:8px;margin:10px 0;padding:12px;background:white}summary{cursor:pointer;font-weight:650}.PASS{color:#166534}.PARTIAL{color:#875400}.FAIL{color:#b91c1c}.muted{color:#5b6b75}.answer{white-space:pre-wrap;background:white;padding:20px;border-left:4px solid #408bb0}img{max-width:100%;border:1px solid #dbe3e8}code{overflow-wrap:anywhere}"""


def esc(value):
    return html.escape(str(value))


def document(title, body):
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>' + esc(title) + '</title><style>' + STYLE + '</style><body>' + body + '</body></html>'


def build(folder):
    ui = json.loads((folder / "ui-results.json").read_text())
    backend = json.loads((folder / "backend-traces.json").read_text())["turns"]
    judgments = json.loads((folder / "judgments.json").read_text()) if (folder / "judgments.json").exists() else {}
    rows = []
    for index, answer in enumerate(ui):
        raw = backend[index]
        assert raw["question"] == answer["question"], (index, "Question order mismatch")
        trace = raw["trace"]
        assert trace and trace["source_revision"] == answer["source_revision"]
        verdict = judgments.get(answer["id"], {"verdict": "UNREVIEWED", "reason": "Review pending"})
        model_spans = [s for s in trace["spans"] if s["name"].startswith("model.")]
        result = {**answer, **verdict, "trace_id": trace["trace_id"], "model_calls": len(model_spans),
                  "backend_metadata": raw["metadata"], "trace_spans": len(trace["spans"])}
        rows.append(result)
        json_name = answer["id"] + "-trace.json"
        (folder / json_name).write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n")
        sections = []
        for span in trace["spans"]:
            sections.append('<details><summary>' + esc(f'{span["sequence"]}. {span["name"]} · {span["status"]} · {span.get("latency_ms", "—")} ms') + '</summary><pre>' + esc(json.dumps(span, ensure_ascii=False, indent=2)) + '</pre></details>')
        body = f'<p><a href="review.html">← All 40 questions</a></p><h1>{esc(answer["id"])} · <span class="{esc(verdict["verdict"])}">{esc(verdict["verdict"])}</span></h1><p>{esc(answer["question"])}</p><p><b>Expected:</b> {esc(answer["expected"])}</p><p><b>Verdict:</b> {esc(verdict["reason"])}</p><h2>Displayed answer</h2><div class="answer">{esc(answer["response"])}</div><p><a href="{esc(answer["screenshot"])}">Browser screenshot</a> · <a href="{json_name}">Trace JSON</a></p><h2>Execution trace</h2><p>Build <code>{esc(trace["source_revision"])}</code> · {len(model_spans)} model calls · {len(trace["spans"])} spans · {trace["dropped_spans"]} dropped spans.</p><p class="muted">Execution “ok” means a step returned normally. Answer completeness is judged separately. Inputs and outputs are bounded and redacted; private model reasoning is excluded.</p>' + ''.join(sections)
        (folder / (answer["id"] + ".html")).write_text(document(answer["id"] + " execution trace", body))
    counts = {name: sum(r["verdict"] == name for r in rows) for name in ("PASS", "PARTIAL", "FAIL")}
    if any(r["verdict"] == "UNREVIEWED" for r in rows):
        counts["UNREVIEWED"] = sum(r["verdict"] == "UNREVIEWED" for r in rows)
    summary = {"questions": len(rows), "counts": counts, "source_revisions": sorted({r["source_revision"] for r in rows}),
               "mixed_versions": False, "execution": "actual_browser_ui", "trace_coverage": sum(bool(r["trace_id"]) for r in rows),
               "model_calls": sum(r["model_calls"] for r in rows), "spans": sum(r["trace_spans"] for r in rows),
               "judgment_method": "Assistant review against saved expectations, displayed answers and recorded evidence; not independent clinical validation."}
    (folder / "review.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    table = ''.join(f'<tr><td><a href="{r["id"]}.html">{r["id"]}</a></td><td>{esc(r["question"])}</td><td class="{r["verdict"]}">{r["verdict"]}</td><td>{esc(r["reason"])}</td><td>{r["model_calls"]}</td></tr>' for r in rows)
    cards = '<div style="display:flex;gap:18px;margin:24px 0">' + ''.join(f'<div style="background:white;border:1px solid #dbe3e8;border-radius:10px;padding:14px 30px"><div class="{k}" style="font-size:34px;font-weight:750">{v}</div>{k.title()}</div>' for k,v in counts.items()) + '</div>'
    body = '<h1>Ask Dorjee · 40-question traced UI replay</h1><p>One build. Original questions and order. Every displayed answer links to its saved screenshot and private server trace.</p>' + cards + f'<p><b>Build {esc(", ".join(summary["source_revisions"]))}</b> · {summary["trace_coverage"]}/{len(rows)} answers linked · {summary["model_calls"]} model calls · {summary["spans"]} trace steps</p><p><a href="findings.html">What the traces revealed</a> · <a href="run-verification.json">Run verification</a></p><p class="muted">Pass means the saved expectation was met; refusal tests may pass without finding a provider. This synthetic long-conversation suite is not a production error-rate estimate or a clinical sign-off. Trace values are bounded and redacted.</p><table><thead><tr><th>Case</th><th>Question</th><th>Verdict</th><th>Reason</th><th>Model calls</th></tr></thead><tbody>' + table + '</tbody></table>'
    (folder / "review.html").write_text(document("Ask Dorjee traced UI review", body))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    print(json.dumps(build(parser.parse_args().folder), indent=2))
