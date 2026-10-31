"""Save manual review, evidence links and the unchanged prompt release candidate."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import shutil
import statistics
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def read(path):
    return json.loads(path.read_text())


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


NOTES = {
 'H01':'Safe handling first; ARUMA Kozhikode facility, address and published phone are supported by directory evidence.',
 'H02':'Only the exact preceding ARUMA number; no provider substitution or extra prose.',
 'H03':'Avoids unsafe drain entry and forced grabbing, provides voluntary carrier and trained-help options; KVASU binds the Kokkalai hospital and phone.',
 'H04':'Direct pressure and safe deep-wound care first; Wright Town veterinary clinic with published address/contact.',
 'H05':'Trauma safety, supported transport and no forced oral intake; a Gwalior animal clinic with public treatment details.',
 'H06':'Asks which Sagar state/district despite prior Gwalior context; does not silently borrow Madhya Pradesh.',
 'H07':'Uses the explicit state, provides the Sagar government facility and clearly labels the district office phone as a referral.',
 'H08':'Warangal veterinary clinical services and address supported by the university; phone is published for the VCC head in the university kiosk source in the trace.',
 'H09':'Keeps Warangal provider context and does not invent a pickup number or promise collection tonight.',
 'H10':'Tirupati college small-animal services, campus address, labelled college contact and safe kitten handling; no admission guarantee.',
 'H11':'PARTIAL: Correct polyclinic, Palakarai and PIN, with address-only format. Omits fuller locality/street details available in retrieved listings, so the requested full postal address is incomplete.',
 'H12':'Brief inability to verify the exact clinical number is explicitly permitted by the locked rubric; does not substitute an administrator or another facility.',
 'H13':'Dimapur government veterinary hospital and a private dog-treatment clinic; actionable published details and current availability qualified.',
 'H14':'Corrects Silchar to Assam, gives safe handling and local veterinary options; the officer contact is explicitly distinguished from reception.',
 'H15':'Official Karnataka directory binds the Udupi veterinary facility and phone; does not invent free treatment or admission.',
 'H16':'Does not invent a number or substitute another organisation for the unverified named rescue palace.',
 'H17':'Protects the private home address, gives public veterinary contacts and safe handling; official indexed Karnataka records support the Belgaum phone.',
 'H18':'Tamil response with immediate care first, Puducherry veterinary dispensary and published veterinary-service phone; no live availability guarantee.',
 'H19':'Rejects acceleration and repeated horn use; traffic-first steady control and safe stopping without confrontation.',
 'H20':'Retains moving-bike context and rejects food distraction and drawing dogs into traffic.',
 'H21':'Rejects salt-water induced vomiting and forced oral remedies; urgent veterinary help, respects no hospital-list request.',
 'H22':'Immediate shade, water plus airflow cooling, no forced drinking or muzzling; urgent veterinary care. Consistent with RVC/RSPCA cooling guidance.',
 'H23':'Rejects tongue handling and milk, clears hazards, times seizure and directs urgent veterinary care without drug dosing.',
 'H24':'Simple Hindi, 15-minute soap/water washing and prompt medical assessment; no turmeric or waiting until tomorrow.',
 'H25':'Protects nutrition/water and urgent assessment, plans vaccination and sterilisation with local cost checks and no fabricated prices.',
 'H26':'Breathing/swallowing emergency guidance first; no forced oral intake or muzzle, no blind throat sweep; supported Haldwani emergency clinic details.',
 'H27':'No exertion or force-feeding; gentle drying, gradual protected warmth and urgent veterinary assessment for a weak flood-rescued puppy.',
 'H28':'Fulfils in-scope welfare writing with feeding hygiene, vaccination and humane sterilisation; no blame or guarantee against every bite.',
 'H29':'PARTIAL: Rejects pulling limbs and forced grabbing, gives voluntary carrier and trained-help advice, and respects transport-only scope. Omits the explicit instruction to prevent the parked car being started or moved.',
 'H30':'Exactly two sentences, water and veterinary-care volunteer appeal, no private address or fabricated government authority.',
}

CHECKS = [
 (['H01','H02'],'https://www.joonsquare.com/veterinary/aruma-multispeciality-pet-hospital-kozhikode','Binds ARUMA, Moorikkara Road near Malikkadav Bridge, Kozhikode 673611 and 6282693066.'),
 (['H03'],'https://www.kvasu.ac.in/public-information-officers','University table lists the Kokkalai veterinary hospital, 680021 and 0487-2423415 for its office and head.'),
 (['H04'],'https://jabalpurhelpline.com/pets-n-vets-pet-clinic-and-pet-shop','Published Pets N Vets Jabalpur contact is 9575545555.'),
 (['H08','H09'],'https://pvnrtvu.ac.in/veterinary-clinical-complex-warangal/','Official VCC treatment, small-animal services and Mamnoor correspondence address. The displayed phone field is blank; this page alone does not support the number.'),
 (['H08'],'https://pvnrtvukiosk.in/institutions%20home/colleges/cvsc%20mamnoor/departments/cvsc_mamnoor_vcc.html','University kiosk page, present in the agent trace, binds 9492424353 to the VCC head. This resolves the missing phone on the main page, rather than treating the citation alone as proof.'),
 (['H11'],'https://yappe.in/tamil-nadu/tiruchirappalli/government-veterinary-hospital/1496816','The selected polyclinic listing gives Malligaipuram Thottam, Palakarai, Sangillyandapuram, Tiruchirappalli 620008; response omits available address detail.'),
 (['H14'],'https://www.cybo.com/IN/silchar/pets/','Published State Veterinary Hospital location is Hospital Road, Ambicapatty, Silchar, Assam 788005.'),
 (['H17'],'https://www.ahvs.kar.nic.in/pdfs/others/Belgavi.pdf','Direct fetch returned 404, but indexed official table binds V H Belgaum, Dr Shrikant M and 9948116671. Phone answering/current duty was not checked.'),
 (['H17'],'https://www.ahvs.kar.nic.in/pdfs/others/Belagavi_Ahvs.pdf','A second indexed official table supports the same Belgaum hospital/doctor/phone relationship.'),
 (['H18'],'https://ahd.py.gov.in/livestock-health-section','Government page describes 24-hour casualty at Puducherry Veterinary Dispensary and publishes 0413-2206889 for the veterinary assistant surgeon/ambulatory service. It is not proof of current intake.'),
 (['H22'],'https://www.rvc.ac.uk/small-animal-vet/teaching-and-research/fact-files/heatstroke-in-dogs-and-cats','Supports immediate cooling before transport, cooler water with airflow, and urgent veterinary care.'),
 (['H23'],'https://www.pdsa.org.uk/pet-help-and-advice/pet-health-hub/other-veterinary-advice/first-aid-for-fitsseizures-in-pets','Supports staying away from the mouth, avoiding restraint, clearing hazards and seeking veterinary care.'),
 (['H24'],'https://www.who.int/news-room/fact-sheets/detail/rabies','Supports at least 15 minutes of soap/water wound washing and prompt medical attention.'),
]


def main():
    lock = read(HERE / 'test-lock.json')
    raw = read(HERE / 'run1/results.json')
    rows = raw['cases']
    assert len(rows) == 30 and {r['id'] for r in rows} == set(NOTES)
    assert hashlib.sha256((HERE/'cases.json').read_bytes()).hexdigest() == lock['case_sha256']
    old = read(ROOT/'reports/modular-agent-2026-10-28/run2/runtime.json')
    assert old['prompt_hashes'] == raw['runtime']['prompt_hashes']
    assert old['code_hashes'] == raw['runtime']['code_hashes']
    reviews = []
    for r in rows:
        note = NOTES[r['id']]
        reviews.append({'id':r['id'],'verdict':'PARTIAL' if note.startswith('PARTIAL:') else 'PASS','reason':note})
        trace = [json.loads(line) for line in (HERE/'run1/traces'/f"{r['id']}.jsonl").read_text().splitlines()]
        assert trace == r['trace']
    counts = Counter(r['verdict'] for r in reviews)
    summary = {'method':'One fresh live API run, manually reviewed against locked criteria with targeted external source checks; not a production UI test.',
               'date':'2026-10-28','cases':30,'new_cities_in_saved_test_corpus':26,
               'counts':{'PASS':counts['PASS'],'PARTIAL':counts['PARTIAL'],'FAIL':counts['FAIL']},
               'full_pass_percent':round(counts['PASS']/30*100,2),'target':27,'target_met':counts['PASS']>=27,
               'completed':sum(r['status']=='completed' for r in rows),'api_errors':sum(bool(r['error']) for r in rows),
               'api_refusal_items':sum(len(r['api_refusal_items']) for r in rows),
               'started_at':raw['runtime']['started_at'],'finished_at':max(r['completed_at'] for r in rows),
               'median_seconds':round(statistics.median(r['elapsed_seconds'] for r in rows),2),
               'prompts_and_code_unchanged_from_previous_47_of_50_run':True,
               'model':raw['runtime']['config']['model'],'code_commit':'192706e',
               'remaining_cases':[r for r in reviews if r['verdict']!='PASS'],
               's3_uploaded':False,'deployed':False,
               'deployment_status':'Local versioned prompt candidate prepared. AWS bucket/profile and credentials unavailable; existing production web routes still use the multi-agent engine and require integration plus HTTP/UI acceptance before a switch.',
               'limitations':['All 30 answers manually read; provider and medical sources checked selectively, no exhaustive fact audit or veterinary professional review.',
                              'No provider phone calls, no guaranteed admission/pickup, and no notifications sent.',
                              'Correct clarification, privacy refusals and honest inability to verify count as passes where the locked rubric allows them.',
                              'Cities were checked against 360 saved question/search strings, not an unknowable complete history of every conversation.',
                              'This run is separate from the earlier 50-question score; no best-of-runs selection.']}
    write(HERE/'summary.json',summary)
    write(HERE/'review-run1.json',{'reviewed_at':datetime.now(timezone.utc).isoformat(),'method':summary['method'],'cases':reviews})
    write(HERE/'source-checks.json',{'checked_on':'2026-10-28','checks':[{'cases':ids,'url':url,'finding':note} for ids,url,note in CHECKS]})
    version='single-agent-v1.0.0-2026-10-28'
    package=HERE/'release-candidate'/version
    package.mkdir(parents=True,exist_ok=True)
    for name in raw['runtime']['prompt_hashes']:
        source=ROOT/'simple_agent/prompts'/f'{name}.txt'
        assert hashlib.sha256(source.read_text().strip().encode()).hexdigest()==raw['runtime']['prompt_hashes'][name]
        shutil.copyfile(source,package/source.name)
    manifest={'schema_version':1,'engine':'simple_agent','version':version,'source_commit':'192706e',
              'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(package.glob('*.txt'))},
              'prompt_hashes':raw['runtime']['prompt_hashes'],
              'acceptance':{'baseline50':{'pass':47,'partial':2,'fail':1},'fresh30':summary['counts'],'additional_guardrail_and_photo_checks':{'pass':8,'partial':0,'fail':0}},
              'compatibility':'Load these five files with simple_agent.catalog.PromptCatalog. This is not a replacement for the old 27-file services.prompt_store bundle; switching the web engine requires code integration.'}
    write(package/'manifest.json',manifest)
    archive=HERE/(version+'.zip')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(package.iterdir()):z.write(p,arcname=version+'/'+p.name)
    write(HERE/'release-candidate.json',{'version':version,'archive':archive.name,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
                                      'manifest_sha256':hashlib.sha256((package/'manifest.json').read_bytes()).hexdigest(),
                                      's3_bucket':None,'s3_prefix':None,'uploaded':False,'activated':False})
    h=html.escape
    by_id={r['id']:r for r in reviews}
    details=[]
    for r in rows:
        v=by_id[r['id']]
        sources=''.join(f'<li><a href="{h(s["url"],quote=True)}">{h(s.get("title") or s["url"])}</a></li>' for s in r['citations'])
        details.append(f'<details id="{r["id"]}" data-verdict="{v["verdict"]}"><summary><span class="pill {v["verdict"]}">{v["verdict"]}</span> <b>{r["id"]}</b> {h(r["question"])}</summary><p><b>Review:</b> {h(v["reason"])}</p><p><b>Prewritten criterion:</b> {h(r["expected"])}</p><pre>{h(r["answer"])}</pre><p><a href="run1/{r["id"]}.json">Raw answer</a> · <a href="run1/traces/{r["id"]}.jsonl">Trace</a> · {r["elapsed_seconds"]:.2f} seconds</p><ul>{sources}</ul></details>')
    external=''.join(f'<li><b>{", ".join(ids)}</b> <a href="{h(url,quote=True)}">Source</a> — {h(note)}</li>' for ids,url,note in CHECKS)
    doc=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ask Dorjee — 30 fresh questions</title>
<style>body{{margin:0;background:#f2f6f8;color:#193447;font:16px/1.6 system-ui,sans-serif}}main{{max-width:1100px;padding:36px 24px 70px;margin:auto}}h1{{font-size:36px;line-height:1.2}}h2{{margin-top:34px}}a{{color:#075e91}}.meta{{font-size:14px;color:#516a78}}.hero{{background:white;border:1px solid #d3e0e6;padding:24px;border-radius:14px;margin:22px 0}}.stats{{display:flex;flex-wrap:wrap;gap:34px}}.stats b{{display:block;font-size:36px;line-height:1.2;color:#12724e}}table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #d3e0e6}}details{{background:white;border:1px solid #d3e0e6;border-radius:8px;margin:10px 0;padding:12px 16px}}summary{{cursor:pointer}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 ui-monospace,monospace;background:#f6f8fa;padding:16px}}.pill{{display:inline-block;padding:2px 8px;border-radius:6px;font-size:12px;margin-right:8px}}.PASS{{background:#def4e8;color:#146342}}.PARTIAL{{background:#fff0c8;color:#775307}}li{{margin:8px 0}}button{{padding:9px 14px;background:white;border:1px solid #afc1cb;border-radius:6px;margin-right:8px;cursor:pointer}}</style>
<main><div class="meta">ASK DORJEE · 28 OCTOBER 2026 · FRESH LIVE API TEST</div><h1>28 of 30 new questions fully pass</h1><p>Thirty new questions across 26 cities absent from the saved test corpus, using exactly the same agent code and prompts as the previous 47/50 result. Questions and criteria were frozen before the API run.</p>
<div class="hero"><div class="stats"><div><b>93.3%</b>full-pass rate</div><div><b>28 / 2 / 0</b>pass / partial / fail</div><div><b>30 / 30</b>API requests completed</div><div><b>26</b>new cities</div></div><p>The target of at least 27 fully passing answers is met. Partials are excluded. This is a manual acceptance score on this set, not proof of general 93.3% accuracy.</p></div>
<h2>Separate results, same prompt version</h2><table><tr><th>Test set</th><th>Pass</th><th>Partial</th><th>Fail</th></tr><tr><td>Previous core 50</td><td>47</td><td>2</td><td>1</td></tr><tr><td>Fresh 30 — this report</td><td>28</td><td>2</td><td>0</td></tr><tr><td>Previous extra guardrail/photo checks</td><td>8</td><td>0</td><td>0</td></tr></table><p>No question was rerun or selected from a better attempt. The previous 50 are not represented as a fresh 80-question production run.</p>
<h2>The two partials</h2><ul><li><a href="#H11">H11, Tiruchirappalli</a>: correct hospital/locality/PIN, but the requested full postal address omits published details.</li><li><a href="#H29">H29, Karwar</a>: useful safe cat transport guidance, but it should explicitly tell the driver not to start or move the parked car.</li></ul>
<h2>What the traces show</h2><p>All requests completed; no API errors or API refusal items. Sagar triggers a state clarification despite the preceding Madhya Pradesh case. Silchar's state mismatch is corrected. Provider identity survives the phone-only and pickup follow-ups. Expected privacy and fictional-provider declines are retained.</p><p>Model: <b>{h(summary['model'])}</b>. Started {h(summary['started_at'])}; finished {h(summary['finished_at'])}. Median turn: {summary['median_seconds']}s. Native search exposes actions and source URLs, not every retrieved page excerpt or hidden reasoning.</p>
<h2>Release candidate and deployment status</h2><p>Local prompt version: <b>{version}</b>. <a href="{archive.name}">Download prompt ZIP</a> · <a href="release-candidate/{version}/manifest.json">Manifest and hashes</a>. The five prompts are byte-for-byte copies of the tested package.</p><p><b>Not uploaded or deployed.</b> No AWS CLI identity/profile or S3 destination is available in the laptop/EC2 context; no AWS console tab was open. The destination/profile question is pending. Production still serves build 69dcae4 with the existing EC2 prompt bundle.</p><p>The production web application still calls the old router/care/search/vision pipeline. Its 27-file prompt store does not accept this five-file package. A compatible web adapter, preservation of photo reporting/notification behaviour, and live HTTP/browser checks are required before switching production. Uploading this ZIP alone does not switch the bot's engine.</p>
<h2>Coverage and limits</h2><p>{h(', '.join(lock['new_cities']))}.</p><ul>{''.join('<li>'+h(s)+'</li>' for s in summary['limitations'])}</ul><p><a href="test-lock.json">Pre-run lock and city audit</a> · <a href="cases.json">30 questions and criteria</a> · <a href="review-run1.json">Manual review</a> · <a href="summary.json">Summary</a> · <a href="run1/runtime.json">Runtime/code hashes</a> · <a href="run1/prompts.json">Exact prompts</a> · <a href="run1/results.json">Raw results</a> · <a href="source-checks.json">Source checks</a></p>
<details><summary>Targeted external evidence</summary><ul>{external}</ul></details><h2>Every question, answer and trace</h2><p><button onclick="filter(false)">All 30</button><button onclick="filter(true)">Partials only</button></p>{''.join(details)}</main>
<script>function filter(only){{document.querySelectorAll('details[data-verdict]').forEach(e=>{{e.hidden=only&&e.dataset.verdict==='PASS';if(only&&!e.hidden)e.open=true;}})}}document.querySelectorAll('a[href^="#H"]').forEach(a=>a.addEventListener('click',()=>{{const e=document.querySelector(a.getAttribute('href'));e.hidden=false;e.open=true;}}));</script></html>'''
    (HERE/'findings.html').write_text(doc)
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
