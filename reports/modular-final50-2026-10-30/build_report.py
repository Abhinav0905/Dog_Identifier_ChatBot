"""Build separate full-run acceptance reports, verify locks/traces and preserve provenance."""
from collections import Counter
from datetime import datetime, timezone
import hashlib, html, json, statistics
from pathlib import Path
P=Path(__file__).resolve().parent
ROOT=P.parents[1]
read=lambda f:json.loads(f.read_text())
def write(f,d):f.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
def digest(f):return hashlib.sha256(f.read_bytes()).hexdigest()
h=html.escape
lock=read(P/'test-lock.json')
assert digest(P/'cases.json')==lock['case_sha256']
old=read(ROOT/'reports/modular-holdout30-2026-10-28/run1/runtime.json')
summaries=[]; sections=[]
for name in ['run1','run2']:
 if not (P/f'review-{name}.json').exists():continue
 raw=read(P/name/'results.json'); rows=raw['cases']; review=read(P/f'review-{name}.json')['cases']; by_id={r['id']:r for r in review}
 assert len(rows)==len(review)==50 and {r['id'] for r in rows}==set(by_id)
 assert raw['runtime']['case_file_sha256']==lock['case_sha256']
 assert raw['runtime']['prompt_hashes']==old['prompt_hashes']
 hashes=raw['runtime']['code_hashes']; orig=old['code_hashes']
 assert all(hashes.get(k)==v for k,v in orig.items())
 extras=sorted(set(hashes)-set(orig)); assert all(Path(k).name.startswith('._') for k in extras)
 counts=Counter(r['verdict'] for r in review); actions=Counter(); sourced=0; queries=0; model_calls=0; photo_calls=0; source_cases=set()
 for r in rows:
  trace=[json.loads(s) for s in (P/name/'traces'/f"{r['id']}.jsonl").read_text().splitlines()]
  assert trace==r['trace'] and trace[-1]['stage']=='turn_end'
  assert trace[0]['question']==r['question']
  for e in trace:
   if e['stage']=='agent':
    model_calls+=1
    for it in e['response'].get('output',[]):
     if it['type']=='web_search_call':
      a=it.get('action',{});actions[a.get('type')]+=1
      if a.get('type')=='search':
       queries+=len(a.get('queries') or [a.get('query')])
       if a.get('sources'):sourced+=1;source_cases.add(r['id'])
   elif e['stage']=='photo_analysis':photo_calls+=1
  blob=json.dumps(trace)
  assert 'encrypted_content' not in blob and 'data:image' not in blob and 'OPENAI_API_KEY' not in blob
 summary={'run':name,'date':'2026-10-30','method':'Standalone live API run with manual semantic acceptance review; not a production UI test.','counts':{v:counts[v] for v in ['PASS','PARTIAL','FAIL']},'full_pass_percent':counts['PASS']*2,'target_full_passes':45,'target_met':counts['PASS']>=45,'completed':sum(r['status']=='completed' for r in rows),'api_errors':sum(bool(r['error']) for r in rows),'api_refusal_items':sum(len(r['api_refusal_items']) for r in rows),'model':raw['runtime']['config']['model'],'started_at':raw['runtime']['started_at'],'finished_at':max(r['completed_at'] for r in rows),'median_seconds':round(statistics.median(r['elapsed_seconds'] for r in rows),2),'main_model_calls':model_calls,'photo_analysis_calls':photo_calls,'search_actions':actions['search'],'search_query_strings':queries,'search_actions_with_sources':sourced,'direct_open_actions':actions['open_page'],'cases_with_search_sources':len(source_cases),'prompt_hashes_match_previous_candidate':True,'all_original_code_file_hashes_match':True,'raw_hash_inventory_extra_files':extras,'extra_file_explanation':'macOS AppleDouble metadata from archive transfer; no missing or changed candidate files.','remaining_cases':[r for r in review if r['verdict']!='PASS'],'pattern_counts':dict(Counter(r['pattern'] for r in review if r.get('pattern'))),'production_changed':False,'s3_uploaded':False}
 write(P/f'summary-{name}.json',summary);summaries.append(summary)
 details=[]
 for r in rows:
  v=by_id[r['id']];sources=''.join(f'<li><a href="{h(c["url"],quote=True)}">{h(c.get("title") or c["url"])}</a></li>' for c in r['citations'])
  details.append(f'<details data-verdict="{v["verdict"]}" id="{name}-{r["id"]}"><summary><b class="{v["verdict"]}">{v["verdict"]}</b> {r["id"]} — {h(r["question"])}</summary><p><b>Review:</b> {h(v["reason"])}</p><p><b>Locked criterion:</b> {h(r["expected"])}</p><pre>{h(r["answer"])}</pre><ul>{sources}</ul><p><a href="{name}/{r["id"]}.json">Raw result</a> · <a href="{name}/traces/{r["id"]}.jsonl">Trace</a> · {r["elapsed_seconds"]:.2f}s</p></details>')
 sections.append(f'<section><h2>{name}: {counts["PASS"]} pass / {counts["PARTIAL"]} partial / {counts["FAIL"]} fail</h2><p>{h(summary["started_at"])} to {h(summary["finished_at"])}. {sourced}/{actions["search"]} search actions returned sources; {photo_calls} photo tool calls completed. <a href="summary-{name}.json">Run summary</a></p>'+''.join(details)+'</section>')
assert summaries
last=summaries[-1]
overall={'generated_at':datetime.now(timezone.utc).isoformat(),'runs':summaries,'latest_complete_run':last['run'],'no_best_of_selection':True,'code_or_prompt_changes_between_runs':False,'production_changed':False,'merged':False,'release_recommendation':'Hold merge and release: latest full pass rate is 86%, below the agreed 90% target. Web integration and UI acceptance also remain outstanding.','limitations':['A pass is a manual acceptance verdict on this fixed set, not a population-wide accuracy estimate.','Expected privacy/scope refusals and honest inability to verify exact phone roles count as passes only where the locked rubric permits them.','Medical and provider sources were checked selectively; no exhaustive clinical audit or live phone confirmation.','The standalone agent still requires compatible web integration and HTTP/UI acceptance.','No user-facing messages, rescue alerts or production database operations were triggered by tests.']}
write(P/'summary.json',overall)
checks=read(P/'source-checks.json')['checks']
sourcehtml=''.join(f'<li>{h(", ".join(c["cases"]))}: <a href="{h(c["url"],quote=True)}">Source</a> — {h(c["finding"])}</li>' for c in checks)
scoretable=''.join(f'<tr><td>{s["run"]}</td><td>{s["counts"]["PASS"]}</td><td>{s["counts"]["PARTIAL"]}</td><td>{s["counts"]["FAIL"]}</td><td>{s["full_pass_percent"]}%</td><td>{s["search_actions_with_sources"]}/{s["search_actions"]}</td></tr>' for s in summaries)
extra=(P/'assessment.txt').read_text() if (P/'assessment.txt').exists() else 'Final assessment is being completed.'
doc=f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ask Dorjee — final 50-question evaluation</title><style>body{{margin:0;color:#173349;background:#f2f5f7;font:16px/1.6 system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:28px 24px 70px}}h1{{font-size:34px;line-height:1.2}}a{{color:#065f91}}table{{width:100%;border-collapse:collapse;background:white}}td,th{{text-align:left;border-bottom:1px solid #cbd7df;padding:12px}}details{{background:white;border:1px solid #cbd7df;padding:12px 16px;border-radius:8px;margin:10px 0}}summary{{cursor:pointer}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f7f9;padding:16px;font:14px/1.6 ui-monospace,monospace}}.PASS{{color:#147144}}.PARTIAL{{color:#9a6100}}.FAIL{{color:#b62727}}button{{padding:9px 14px;margin:8px 8px 8px 0;background:white;border:1px solid #8fa9b7;border-radius:5px}}.note{{padding:18px;background:white;border-left:4px solid #21799a}}li{{margin:8px 0}}</style></head><body><main><p>ASK DORJEE · 30 OCTOBER 2026 · STANDALONE API ACCEPTANCE</p><h1>Final 50 fresh questions</h1><p>Same candidate implementation and five prompts as the earlier 47/50 and 28/30 results. All 50 questions are distinct from the 88 saved core/extra/holdout questions. Ten conversations preserve ordered follow-ups; three supplied image fixtures exercise photo analysis.</p><table><tr><th>Full run</th><th>Pass</th><th>Partial</th><th>Fail</th><th>Full-pass rate</th><th>Search calls with sources</th></tr>{scoretable}</table><pre class="note">{h(extra)}</pre><h2>Why two separate full runs</h2><p>Run1 completed during source-empty search behaviour: 80 search actions and 224 query strings returned no source lists. A control without chatbot guardrails showed the same search behaviour, while direct opening of a known WHO page succeeded. A later identical control returned 18 sources. Run2 repeats the complete unchanged set once after that observed recovery. Run1 remains visible; no per-question results are selected across runs.</p><p><a href="test-lock.json">Original test lock</a> · <a href="rerun-decision.json">Reason for one full repeat</a> · <a href="diagnostic/search.json">Original plain-prompt control</a> · <a href="diagnostic-recovery/search.json">Recovery control</a> · <a href="cases.json">Questions and criteria</a> · <a href="summary.json">Summary</a></p><h2>Provenance and limits</h2><p>All original candidate code files and prompt hashes match the earlier candidate. The transferred archive added harmless macOS AppleDouble metadata files to the raw file inventory; no original file changed. Production remains unchanged. The five-file agent package is separate from the existing 27-file production prompt store and still needs web integration and HTTP/UI checks.</p><ul>{''.join('<li>'+h(x)+'</li>' for x in overall['limitations'])}</ul><details><summary>Targeted independent evidence checks</summary><ul>{sourcehtml}</ul></details><h2>All saved answers and review decisions</h2><button onclick="filter(false)">Show all</button><button onclick="filter(true)">Show partials and failures</button>{''.join(sections)}</main><script>function filter(only){{document.querySelectorAll('details[data-verdict]').forEach(e=>{{e.hidden=only&&e.dataset.verdict==='PASS';if(only&&!e.hidden)e.open=true;}})}}</script></body></html>'''
(P/'findings.html').write_text(doc)
lines=['ASK DORJEE — FINAL 50-QUESTION EVALUATION — 30 OCTOBER 2026','',extra,'']
for s in summaries:lines.append(f"{s['run']}: {s['counts']['PASS']} PASS / {s['counts']['PARTIAL']} PARTIAL / {s['counts']['FAIL']} FAIL. Search source-bearing calls: {s['search_actions_with_sources']}/{s['search_actions']}.")
lines.extend(['','The runs are separate; no best-of selection. Standalone API tests, not production UI validation.',''])
for s in summaries:
 by_id={r['id']:r for r in read(P/f"review-{s['run']}.json")['cases']}
 for r in read(P/s['run']/'results.json')['cases']:
  v=by_id[r['id']];lines.extend(['='*65,f"{s['run']} {r['id']} | {v['verdict']}",r['question'],'',r['answer'],'','Review: '+v['reason'],''])
(P/'email-results.txt').write_text('\n'.join(lines)+'\n')
print(json.dumps({'runs':[{'run':s['run'],'counts':s['counts'],'source_calls':s['search_actions_with_sources']} for s in summaries],'all_traces_verified':True},indent=2))
