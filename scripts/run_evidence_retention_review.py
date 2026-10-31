#!/usr/bin/env python3
"""Replay the recorded 40+10 questions through real HTTP handlers and live models.
Isolated SQLite/RAG copy; synthetic sessions; no outbound notification credentials.
This is API validation, not a browser UI run. Results and per-call traces are saved.
"""
from pathlib import Path
import argparse, json, os, sys, sqlite3, time, logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--env-file',required=True)
p.add_argument('--source-db',required=True)
p.add_argument('--plan',required=True)
p.add_argument('--output',required=True)
a=p.parse_args()
output=Path(a.output).resolve();output.mkdir(parents=True,exist_ok=True)
from dotenv import dotenv_values
os.environ.update({k:str(v) for k,v in dotenv_values(a.env_file).items() if v is not None})
os.environ.update({
    'GAIA_ENV_FILE':os.devnull,'DB_PATH':str(output/'validation.db'),
    'STORAGE_DIR':str(output/'storage'),'RAG_VECTOR_BACKEND':'sqlite','RAG_SQLITE_EMBEDDINGS':'false',
    'PROMPT_PACKAGE_DIR':str(ROOT/'prompts/runtime-v2'),
    'ALERT_WEBHOOK_URL':'','SLACK_WEBHOOK_URL':'','TWILIO_ACCOUNT_SID':'','TWILIO_AUTH_TOKEN':'',
    'ADMIN_PASSWORD':'isolated-test-only','CONVERSATION_COOKIE_SECURE':'true',
    'PUBLIC_REQUESTS_PER_MINUTE':'300','AGENT_TRACE_CONTENT_SESSION_IDS':'',
    'APP_SOURCE_REVISION':'evidence-retention-candidate',
})
if (output/'validation.db').exists():
    raise SystemExit('Output database already exists; choose a new output directory to preserve evidence.')
source=sqlite3.connect('file:'+str(Path(a.source_db).resolve())+'?mode=ro',uri=True)
source.row_factory=sqlite3.Row
chunks=[dict(x) for x in source.execute('SELECT doc_file,title,chunk_index,content,embedding FROM rag_chunks')]
source.close()
import app, database as db
from services import agent_trace
from fastapi.testclient import TestClient
logging.getLogger().setLevel(logging.WARNING)
db.init_db()
for c in chunks: db.insert_rag_chunk(c['doc_file'],c['title'],c['chunk_index'],c['content'],c['embedding'])
groups=json.loads(Path(a.plan).read_text())
with ExitStack() as stack:
    clients={g:stack.enter_context(TestClient(app.app,base_url='https://testserver')) for g in groups}
    sessions={g:client.post('/v1/conversations').json()['conversation_id'] for g,client in clients.items()}
    os.environ['AGENT_TRACE_CONTENT_SESSION_IDS']=','.join(sessions.values())
    runtime={'method':'Full HTTP-handler API replay with live configured models; not browser UI validation',
             'production_changed':False,'notifications_enabled':False,'sessions':sessions,
             'rag_chunks':len(chunks),'prompt_version':json.loads((ROOT/'prompts/runtime-v2/manifest.json').read_text())['version']}
    (output/'runtime.json').write_text(json.dumps(runtime,indent=2)+'\n')
    def run(group):
        results=[];client=clients[group];session=sessions[group]
        for case in sorted(groups[group],key=lambda c:c['execution_order']):
            start=time.monotonic()
            response=client.post('/v1/chat/query',json={'message':case['question'],'session_id':session})
            try: payload=response.json()
            except Exception: payload={'error':response.text[:1000]}
            history=db.get_chat_history(session,limit=1)
            metadata=history[-1].get('metadata',{}) if history else {}
            trace_id=metadata.get('stage_trace',{}).get('id')
            result={**case,'http_status':response.status_code,'response':payload,
                    'metadata':metadata,'elapsed_seconds':round(time.monotonic()-start,2),
                    'trace':agent_trace.read(trace_id) if trace_id else None}
            (output/(case['id']+'.json')).write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
            results.append(result)
            print(json.dumps({'id':case['id'],'status':response.status_code,'satisfied':metadata.get('request_satisfied'),'kind':metadata.get('result_kind'),'seconds':result['elapsed_seconds']},ensure_ascii=False),flush=True)
        return results
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=[case for group_results in pool.map(run,groups) for case in group_results]
    (output/'results.json').write_text(json.dumps({'runtime':runtime,'cases':results},indent=2,ensure_ascii=False)+'\n')
