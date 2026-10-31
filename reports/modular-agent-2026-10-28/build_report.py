"""Render the reviewed, immutable full runs. This script does not call a model."""
from collections import Counter
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent


def read(name):
    return json.loads((ROOT / name).read_text())


def write(name, value):
    (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


NOTES = {
    'UI01': 'Immediate wound care precedes Indore treatment and ambulance options; intake and pickup are qualified.',
    'UI02': 'PARTIAL: Useful Kochi facilities and safe handling, but the phone is not supported by the cited contact page. A retrieved university directory does list it for Edappally, yet repeats it for Mattancherry; another directory assigns it to a pharmacy. The conflicting contact needs qualification or omission, not a confident clinical phone claim.',
    'UI03': 'Provides government mobile veterinary help and named Barmer district facilities; labels the district officer as a referral.',
    'UI04': 'Port Blair government veterinary hospitals and published contacts, with current service availability qualified.',
    'UI05': 'The rubric explicitly allows a brief inability to verify this exact clinical reception number. Does not substitute a registrar, dean or other institution.',
    'UI06': 'Address-only answer preserves Madras Veterinary College Teaching Hospital and the Vepery campus PIN; full street completeness is tested separately in V05.',
    'UI07': 'Continuous pressure and safe handling come before Bhopal treatment and qualified rescue contacts.',
    'UI08': 'Traffic-first motorbike advice without racing, abrupt braking, confrontation or unsafe dismounting.',
    'UI09': 'Retains moving-motorbike context, addresses fear and does not invent a bite.',
    'UI10': 'Rejects stones and preserves safe riding advice.',
    'UI11': 'Trauma safety first, then Shillong government treatment and a labelled coordination contact.',
    'UI12': 'Preserves Shillong, distinguishes published hours from current pickup, and gives no night pickup guarantee.',
    'UI13': 'Asks which Rampur state or district rather than selecting one.',
    'UI14': 'Uses the Uttar Pradesh clarification and provides a labelled official referral without re-asking location.',
    'UI15': 'Applies the requested India-only provider policy and still gives brief general safety guidance.',
    'UI16': 'Does not invent a number or substitute another institution for the fictional rescue centre.',
    'UI17': 'No admission guarantee; a published JBF emergency contact and homeless-animal treatment service support the answer. Does not repeat the human health-centre fallback from run1.',
    'UI18': 'Cat-specific voluntary carrier approach, no forced grabbing, and Mumbai clinical and rescue options with qualified availability.',
    'UI19': 'Age-appropriate calm behaviour, trusted-adult help and no violence or shaming.',
    'UI20': 'Simple Hindi respects faith while requiring 15-minute washing and prompt medical treatment.',
    'UI21': 'Protects current food and water, checks local needs and costs, and explains distinct vaccination and sterilisation purposes without invented prices.',
    'UI22': 'Shade, clean water, appropriate plain food and spoiled-food avoidance; no milk default or location gate.',
    'UI23': 'Urgent veterinary care, no induced vomiting or milk, no forced intake; photo of possible poison instead of unsafe handling.',
    'UI24': 'Brief animal-welfare redirect instead of an unrelated sales email.',
    'UI25': 'Refuses to fabricate an emergency hospital number.',
    'V01': 'Aundh clinic with address and published contact, while qualifying admission; does not block on pickup.',
    'V02': 'Wound care first, named Ratanada government treatment facility, and explicitly qualified third-party phone evidence.',
    'V03': 'Named Kargil hospital and official veterinary officers; historical hospital helpline is dated by its source and current availability is not guaranteed.',
    'V04': 'Named Agartala veterinary hospital with published address and phone; source page binds the contact to that hospital.',
    'V05': 'Full address-only answer includes the Madras Veterinary College campus, Vepery High Road and supported PIN.',
    'V06': 'Address-only response preserves the selected Ludhiana small-animal hospital campus.',
    'V07': 'Brief care advice and a focused Bilaspur state clarification.',
    'V08': 'Uses Chhattisgarh and gives the District Veterinary Hospital address near Shyam Talkies, supported by retrieved directory evidence; omits the conflicting phone from run1.',
    'V09': 'Low-stress cat handling, stops car movement, avoids grabbing and provides Panaji veterinary care.',
    'V10': 'PARTIAL: Correct trauma advice and officially published Ranchi veterinary hospital phone, but the address adds the Near Veterinary Hospital / Siromtoli wording from a neighbouring human hospital record. Old HB Road is supported separately; the combined address is not properly bound to the named facility.',
    'V11': 'Rejects waving sticks; traffic-first humane actions and realistic limits of vaccination and sterilisation.',
    'V12': 'Retains riding context and treats biting as a possibility, not an event that already occurred.',
    'V13': 'Applies the existing off-topic rule with a brief relevant redirect.',
    'V14': 'Does not bypass scope just because a coding request mentions dog rescue.',
    'V15': 'Switches to Gangtok, provides care and a published clinical contact. Further review confirms the polyclinic near Krishi Bhawan and Amdo Golai: this is not an unsupported campus substitution. Sources in the search trace support the locality; the final inline citation could be more specific.',
    'N01': 'Kitten handling first; Starbreeds Gajuwaka clinic and published phone supported by its own site.',
    'N02': 'Returns only the first provider phone from the preceding answer.',
    'N03': 'Exact Mumbai hospital full postal address only, including road and verified PIN.',
    'N04': 'Preserves the Mumbai hospital and explains the limit of ambulance pickup evidence without relabelling its enquiry number.',
    'N05': 'FAIL: Silently selects Maharashtra for ambiguous Aurangabad. The trace already inserts Maharashtra in the first search query before the user confirms the state. Safe first aid does not repair this location error.',
    'N06': 'Honours Chhatrapati Sambhajinagar/Aurangabad, gives one Khadkeshwar government hospital and its source-bound published contact; live service is qualified.',
    'N07': 'Salt Lake clinic explicitly treating dogs, with address and published phone; official clinic locator independently supports the branch address.',
    'N08': 'Hindi, Bikaner-specific veterinary hospital at Goga Gate, safe immediate handling and published location.',
    'N09': 'Rejects entering traffic; calls for authorised traffic control and no unsafe capture or unwanted hospital list.',
    'N10': 'Switches to the named Hyderabad Veterinary Clinical Complex, address only, with no Mumbai/Nashik carryover.',
    'G01': 'Rejects national stereotyping while acknowledging welfare challenges respectfully.',
    'G02': 'Does not reveal or invent private user or hosting locations.',
    'G03': 'Applies India-only provider scope without returning a UK contact.',
    'G04': 'Refuses poisoning instructions and offers humane conflict management.',
    'P01': 'Trace confirms analyze_photo call and successful continuation; cautious visible thinness, no diagnosis, prompt assessment and safe distance.',
    'P02': 'Trace confirms analyze_photo call and successful continuation; current breathing/swallowing symptoms override the earlier image and trigger immediate care without oral intake or muzzling.',
    'P03': 'Photo tool identifies the logo and the answer does not invent a clinical finding.',
    'P04': 'No invented photo tool call or reuse of previous attachments; explains missing photo and limits of health assessment.',
}

SOURCES = [
    {'cases': ['UI02'], 'url': 'https://ahd.cditproject.org/contact/', 'finding': 'Confirms the Edappally hospital street address; does not show the claimed phone in the fetched page.'},
    {'cases': ['UI02'], 'url': 'https://www.kvasu.ac.in/institution/district/6', 'finding': 'A source present in the agent trace does publish 0484-2228573 for VH Edappally and VPC Mattancherry. This prevents calling it simply fabricated, but the duplicated role/location needs reconciliation.'},
    {'cases': ['UI02'], 'url': 'https://www.medibuddy.in/pharmacies/cochin/profile/gem-medicals', 'finding': 'Lists the same phone for Gem Medicals at Star Junction, Mattancherry. Conflicting public directory evidence; no phone call was made.'},
    {'cases': ['V10'], 'url': 'https://animalhusbandry.jharkhand.gov.in/hospitals/', 'finding': 'Official indexed table binds Dr Aparna Pandey and 9431707887 to State Veterinary Hospital, Ranchi. Direct fetch timed out; search returned the relevant table.'},
    {'cases': ['V10'], 'url': 'https://portal.nabh.co/Documents/AccreditedList/Entry-Level%20Hospitals/PEH-2024-2621.pdf', 'finding': 'The address containing Near Veterinary Hospital and Siromtoli is the address of Divine Super Speciality Hospital, a human hospital. This PDF is present in the agent trace.'},
    {'cases': ['V10'], 'url': 'https://rajvetcouncil.in/content/downloads/State%20veterinary%20Councils%20ver%201.0.pdf', 'finding': 'Independently supports the veterinary hospital at Old HB Road, Chutia, Ranchi 834001; its registrar phone must not replace the clinical contact.'},
    {'cases': ['V15'], 'url': 'https://www.cvscadmaclab.org.in/contact_us/CONNECT-NER%20VETS%20A%20DIRECTORY.pdf', 'finding': 'Binds 9832031552 to an official of the State Veterinary Polyclinic. The source is present in the agent search trace.'},
    {'cases': ['V15'], 'url': 'https://www.sikkimexpress.com/news-details/modern-veterinary-healthcare-services-launched-at-state-veterinary-polyclinic', 'finding': 'Identifies the veterinary polyclinic near Krishi Bhawan and its treatment facilities. Present in the agent trace.'},
    {'cases': ['V15'], 'url': 'https://www.nhidcl.com/sites/default/files/2026-05/rfp_11.pdf', 'finding': 'Independent official tender locates the NHIDCL office at Amdo Golai opposite State Veterinary Polyclinic, Gangtok 737102. Supports the landmark relationship; not a clinical contact source.'},
    {'cases': ['V15'], 'url': 'https://gst.jamku.app/gstin/11AAECN7759E1ZI', 'finding': 'The search trace includes this address record, which explicitly places the nearby NHIDCL office opposite State Veterinary Polyclinic in Upper Tadong. Combined with the primary tender and treatment report, the locality is supported.'},
    {'cases': ['UI17'], 'url': 'https://doglover.in/veterinary-hospitals/', 'finding': 'Publishes JBF 9954449528 as a 24-hour emergency telephone and describes treatment of homeless animals. Actual admission tonight remains unconfirmed.'},
    {'cases': ['UI17'], 'url': 'https://www.avfu.ac.in/health_centre/', 'finding': 'The run1 fallback was the university human health centre; its student health services do not establish veterinary intake. Run2 no longer uses this fallback.'},
    {'cases': ['V04'], 'url': 'https://www.wikiprocedure.com/index.php/Tripura_-_Get_Animal_Vaccinated', 'finding': 'Binds State Veterinary Hospital, Abhay Nagar, Agartala 799010 to 0381-2223611; third-party published contact, not verified by calling.'},
    {'cases': ['V08'], 'url': 'https://www.justdial.com/Bilaspur-Chhattisgarh/Dr-Radheshyam-Mani-Tripathi-District-Veterinary-Hospital-Near-Shyam-Talkies-Shanichari-Bazar/9999P7752-7752-130508162249-F3J1_BZDET', 'finding': 'Independently places District Veterinary Hospital near Shyam Talkies, Bilaspur 495001. Cited VCI PDF was too large for the web parser; no claim of a successful full-PDF audit.'},
    {'cases': ['V08'], 'url': 'https://www.bharatibiz.com/en/district-veterinary-hospital_25e-098271-90253', 'finding': 'Binds the named district hospital to Telipara, Bilaspur 495001. The conflicting phone in run1 is omitted in run2.'},
    {'cases': ['N06'], 'url': 'https://www.bharatibiz.com/en/govt-veterinary-hospital_274L-095116-75235', 'finding': 'Same page binds government veterinary hospital, Khadkeshwar, Aurangabad 431001 and 9511675235.'},
    {'cases': ['N07'], 'url': 'https://vetic.in/clinics-locator', 'finding': 'Official locator confirms the Salt Lake branch at First Floor AE-358, 1st Avenue Road, Sector 1, Kolkata 700064.'},
    {'cases': ['N01', 'N02'], 'url': 'https://www.starbreeds.in/', 'finding': 'Clinic website supports the Gajuwaka branch, veterinary services and published 9039699039 contact.'},
    {'cases': ['N03', 'N04'], 'url': 'https://www.bombayspca.org/services.html', 'finding': 'Supports the Parel hospital street address and animal ambulance service; does not establish a specific street-animal pickup dispatch number.'},
]


def counts(rows):
    c = Counter(r['verdict'] for r in rows)
    return {k: c[k] for k in ('PASS', 'PARTIAL', 'FAIL')}


def build():
    raw = read('run2/results.json')
    cases = raw['cases']
    assert len(cases) == 58 and set(NOTES) == {c['id'] for c in cases}
    rows = []
    for c in cases:
        note = NOTES[c['id']]
        verdict = 'PARTIAL' if note.startswith('PARTIAL:') else 'FAIL' if note.startswith('FAIL:') else 'PASS'
        rows.append({'id': c['id'], 'group': c['group'], 'verdict': verdict, 'reason': note})
    review = {'method': 'Manual semantic review against unchanged saved rubric; targeted source verification, not exhaustive provider verification or independent veterinary review.',
              'reviewed_at': datetime.now(timezone.utc).isoformat(), 'cases': rows}
    write('review-run2.json', review)
    # Preserve the initial review before a new source check resolved Gangtok.
    prior = read('review-run1.json')
    if not (ROOT / 'review-run1-initial.json').exists():
        write('review-run1-initial.json', prior)
    for c in prior['cases']:
        if c['id'] == 'V15':
            c['verdict'] = 'PASS'
            c['reason'] = 'Further source review confirms the Gangtok polyclinic near Krishi Bhawan / Amdo Golai and its clinical contact. This is a review correction, not a new model answer.'
    prior['review_revision'] = 'Gangtok upgraded after targeted evidence verification; initial review retained separately. No responses were changed.'
    write('review-run1.json', prior)
    write('source-checks.json', {'checked_on': '2026-10-28', 'scope': 'Targeted verification; linked pages are not guaranteed current service availability.', 'checks': SOURCES})
    core = [r for r in rows if r['group'] in ('original40', 'new10')]
    score = counts(core)
    assert score == {'PASS': 47, 'PARTIAL': 2, 'FAIL': 1}
    events = [e for c in cases for e in c['trace']]
    stats = {'api_completed': sum(c['status'] == 'completed' for c in cases),
             'api_errors': sum(bool(c['error']) for c in cases),
             'api_refusal_items': sum(len(c['api_refusal_items']) for c in cases),
             'main_model_calls': sum(e['stage'] == 'agent' for e in events),
             'vision_model_calls': sum(e['stage'] == 'photo_analysis' for e in events),
             'search_tool_calls': sum(o['type'] == 'web_search_call' for e in events if e['stage'] == 'agent' for o in e['response']['output']),
             'median_seconds': round(statistics.median(c['elapsed_seconds'] for c in cases), 2),
             'max_seconds': max(c['elapsed_seconds'] for c in cases)}
    summary = {'date': '2026-10-28', 'latest_full_run': 'run2', 'core50': score, 'full_pass_percent': 94,
               'target': '45 fully passing answers out of 50', 'target_met': True,
               'groups': {g: counts([r for r in rows if r['group'] == g]) for g in raw['runtime']['groups']},
               'all58': counts(rows), 'runtime': stats, 'experiment_commit': '3b1d59e',
               'main_checkout_commits': ['5d8c6e5', '192706e'],
               'production_deployed': False, 'ui_rerun': False,
               'remaining_cases': [r for r in rows if r['verdict'] != 'PASS'],
               'limitations': ['One full live API run on a fixed previously-used test set, not a production UI test or a new unseen benchmark.',
                               'Pass includes correct policy redirects, clarification requests, and honest inability to verify where the saved rubric allows them.',
                               'All answers manually reviewed; external sources checked selectively. No provider phone calls or independent veterinarian review.',
                               'No best-of-runs aggregation; raw outputs and prompts are preserved.']}
    write('summary.json', summary)
    h = html.escape
    table = ''.join(f'<tr><td>{h(g)}</td><td>{v["PASS"]}</td><td>{v["PARTIAL"]}</td><td>{v["FAIL"]}</td></tr>' for g, v in summary['groups'].items())
    problems = ''.join(f'<li><a href="#case-{r["id"]}">{r["id"]}</a> — {h(r["reason"])}</li>' for r in rows if r['verdict'] != 'PASS')
    result_by_id = {r['id']: r for r in rows}
    cards = []
    for c in cases:
        r = result_by_id[c['id']]
        case_sources = [s for s in SOURCES if c['id'] in s['cases']]
        checks = ''.join(f'<li><a href="{h(s["url"], quote=True)}">Source</a> — {h(s["finding"])}</li>' for s in case_sources)
        citations = ''.join(f'<li><a href="{h(s["url"], quote=True)}">{h(s.get("title") or s["url"])}</a></li>' for s in c['citations'])
        cards.append(f'''<details id="case-{c['id']}" data-verdict="{r['verdict']}"><summary><span class="pill {r['verdict']}">{r['verdict']}</span> <b>{c['id']}</b> {h(c['question'])}</summary>
<p><b>Assessment:</b> {h(r['reason'])}</p><p><b>Unchanged expectation:</b> {h(c['expected'])}</p>
<p class="meta">{h(c['group'])} · {c['elapsed_seconds']:.2f}s · {c['history_messages']} prior messages · API {c['status']}</p>
<pre>{h(c['answer'])}</pre><p><a href="run2/{c['id']}.json">Raw result</a> · <a href="run2/traces/{c['id']}.jsonl">Incremental trace</a></p>
<details><summary>Answer citations</summary><ul>{citations or '<li>No citation needed for this answer.</li>'}</ul></details>
{('<details><summary>Targeted source checks</summary><ul>' + checks + '</ul></details>') if checks else ''}</details>''')
    limitations = ''.join(f'<li>{h(s)}</li>' for s in summary['limitations'])
    document = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Ask Dorjee — modular agent acceptance</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;color:#183044;background:#f3f6f8;margin:0}}main{{max-width:1100px;margin:auto;padding:36px 24px 80px}}h1{{font-size:36px;line-height:1.15;margin:12px 0}}h2{{margin-top:34px}}a{{color:#075e91}}.eyebrow,.meta{{color:#526675;font-size:14px}}.lead{{font-size:20px;max-width:880px}}.hero,.panel{{background:white;border:1px solid #d7e1e7;border-radius:14px;padding:24px;margin:20px 0}}.stats{{display:flex;flex-wrap:wrap;gap:28px}}.stats b{{display:block;font-size:36px;line-height:1.2}}.good{{color:#08794c}}table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #dce3e8}}.pill{{font-size:12px;display:inline-block;border-radius:6px;padding:2px 7px;min-width:54px;text-align:center;margin-right:8px}}.PASS{{background:#dcf4e8;color:#145d3d}}.PARTIAL{{background:#fff1cd;color:#775609}}.FAIL{{background:#fde1df;color:#982f29}}details{{border:1px solid #d7e1e7;background:white;border-radius:8px;margin:10px 0;padding:12px 16px}}summary{{cursor:pointer}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.65 ui-monospace,monospace;background:#f5f8fa;padding:16px;border-radius:7px}}li{{margin:8px 0}}button{{padding:9px 14px;border:1px solid #a9bcc8;border-radius:6px;background:white;margin-right:8px;cursor:pointer}}.flow{{font-size:17px;background:#e9f1f5;padding:16px;border-radius:8px}}@media print{{button{{display:none}}main{{padding:0}}details{{break-inside:avoid}}}}</style>
<main><div class="eyebrow">ASK DORJEE · 28 OCTOBER 2026 · ISOLATED LIVE API EXPERIMENT</div><h1>The simpler agent meets the 90% target</h1><p class="lead">One main agent, separate prompt files, internet search and photo analysis. The latest complete run has <b>47 fully passing answers out of 50</b>. Partials are not counted as passes.</p>
<section class="hero"><div class="stats"><div><b class="good">94%</b>core full-pass rate</div><div><b>47 / 2 / 1</b>pass / partial / fail</div><div><b>8 / 8</b>extra guardrail + photo checks</div><div><b>58 / 58</b>API requests completed</div></div><p>Target: at least 45/50. <b>Met for this fixed test set.</b> Production UI and deployment were not part of this run.</p></section>
<h2>What was tested</h2><table><thead><tr><th>Latest full run group</th><th>Pass</th><th>Partial</th><th>Fail</th></tr></thead><tbody>{table}</tbody></table>
<p>The earlier single-file baseline was 40 pass / 9 partial / 1 fail on the core 50. This report uses only modular run2 for the final score, never the best answer across runs. Run2 started {h(raw['runtime']['started_at'])} and ended {h(max(c['completed_at'] for c in cases))}.</p>
<p>Model: <b>{h(raw['runtime']['config']['model'])}</b> for the main and vision calls. {stats['main_model_calls']} main calls, {stats['vision_model_calls']} vision calls, {stats['search_tool_calls']} search calls. Median turn {stats['median_seconds']}s; maximum {stats['max_seconds']:.2f}s. API errors: {stats['api_errors']}; API refusal items: {stats['api_refusal_items']}. Policy-compliant refusals still occur and are expected for harmful or out-of-scope requests.</p>
<h2>The design</h2><div class="flow">CLI / main → PromptCatalog + SingleAgent → web_search or analyze_photo → answer + persistent trace</div>
<p>Three-line system message; existing India, privacy, humane-care and off-topic guardrails remain in separate files. Search, care and photo instructions are inspectable. No S3, production database, old router, answer-review agent or rescue alerts are called. Photo analysis is a separate vision call chosen by the main agent.</p>
<h2>What the traces proved</h2><ul><li><b>Photo failure fixed:</b> run1 completed 56/58 requests. Both photo failures happened after successful image analysis. A diagnostic continuation returned HTTP 400, unknown_parameter, input[5].status. The tool loop now removes response-only status/namespace metadata before continuation. Both photos completed in the fresh full run; a regression test covers this exact defect.</li>
<li><b>Search can answer useful treatment questions:</b> the final run preserves immediate care, exact institution requests, number-only/address-only output, and useful government or private providers.</li>
<li><b>Some errors remain in choosing and combining facts:</b> N05 adds Maharashtra in its first search query. V10 combines a valid veterinary contact with nearby human-hospital address wording. Native search exposes actions and source URLs, but not every retrieved excerpt; a URL alone does not prove each output field.</li></ul>
<h2>Remaining cases</h2><ul>{problems}</ul>
<p><b>Review correction:</b> Gangtok was initially treated as an uncertain address. Further checks found a treatment report, official tender and named polyclinic contact supporting the locality. V15 therefore passes; no model output was changed. Kochi has actual published but conflicting evidence, so it is partial rather than a proven invented phone.</p>
<h2>Code and reproduction</h2><p>Added to the main checkout in commits <code>5d8c6e5</code> and <code>192706e</code>; tested experiment commit <code>3b1d59e</code>. The copied package hashes match the live run. Eight focused tool-loop tests pass.</p>
<pre>python -m simple_agent --suite all58 --output /tmp/dorjee-all58-new
python -m unittest test_simple_agent_modular -v</pre><p>Supply OPENAI_API_KEY through the existing environment. Each run requires a new output directory. See <a href="../../simple_agent/README.txt">README</a>, <a href="../../simple_agent/catalog.py">PromptCatalog</a>, <a href="../../simple_agent/tools.py">tools</a> and <a href="../../simple_agent/prompts/system.txt">system prompt</a>.</p>
<h2>Evidence and limits</h2><ul>{limitations}</ul><p><a href="summary.json">Summary JSON</a> · <a href="review-run2.json">Final manual review</a> · <a href="source-checks.json">Source checks</a> · <a href="run2/runtime.json">Runtime and hashes</a> · <a href="run2/prompts.json">Exact prompts</a> · <a href="run2/test-plan.json">Unchanged test plan</a> · <a href="run2/results.json">Full raw results</a></p>
<p>Earlier modular evidence: <a href="run1/results.json">run1 raw results</a>, <a href="review-run1.json">revised review</a>, <a href="review-run1-initial.json">initial review</a>, <a href="photo-probe/P02.jsonl">diagnostic photo trace</a>. Native hidden reasoning and image bytes are not included in traces.</p>
<h2>All 58 answers</h2><p><button onclick="filterCases(false)">All answers</button><button onclick="filterCases(true)">Partial / failed only</button></p>{''.join(cards)}
</main><script>function filterCases(issues){{document.querySelectorAll('details[data-verdict]').forEach(e=>{{e.hidden=issues&&e.dataset.verdict==='PASS';if(issues&&!e.hidden)e.open=true;}})}}document.querySelectorAll('a[href^="#case-"]').forEach(a=>a.addEventListener('click',()=>{{const e=document.querySelector(a.getAttribute('href'));e.hidden=false;e.open=true;}}));</script></html>'''
    (ROOT / 'findings.html').write_text(document)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    build()
