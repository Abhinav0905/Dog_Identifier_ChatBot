import json,re
from pathlib import Path
from openai import OpenAI
from simple_agent.agent import SingleAgent
client=OpenAI(max_retries=0)
original=client.responses.create
def request(**kwargs):
    try:return original(**kwargs)
    except Exception as exc:
        body=getattr(exc,'body',{})
        message=body.get('message','') if isinstance(body,dict) else ''
        message=re.sub(r'[A-Za-z0-9_+/=-]{80,}','[redacted-long-value]',message)
        print(json.dumps({'error_type':type(exc).__name__,'status':getattr(exc,'status_code',None),'code':getattr(exc,'code',None),'param':getattr(exc,'param',None),'message':message[:800]}),flush=True)
        raise
client.responses.create=request
agent=SingleAgent(client)
cases=json.loads(Path('simple_agent/evaluation/cases.json').read_text())['photo_checks'][:2]
history=[]
out=Path('reports/modular-agent-2026-10-28/photo-probe');out.mkdir()
for case in cases:
    row=agent.turn(case['question'],history,image_path=case['image'],trace_path=out/(case['id']+'.jsonl'))
    (out/(case['id']+'.json')).write_text(json.dumps(row,indent=2))
    print(json.dumps({'id':case['id'],'status':row['status'],'error':row['error']}),flush=True)
