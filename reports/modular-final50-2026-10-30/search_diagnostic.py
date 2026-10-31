"""Two search-health controls with no chatbot guardrails; outside acceptance score."""
import json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from openai import OpenAI
from simple_agent.tools import SearchTool
from simple_agent.tracing import public_response,safe_error,utc_now,write_json
P=Path(__file__).resolve().parent/'diagnostic'
P.mkdir(exist_ok=False)
client=OpenAI(max_retries=0)
questions=[('search','Search the internet for the official World Health Organization rabies fact sheet. Return its title and official URL with a source citation.'),('direct_open','Open https://www.who.int/news-room/fact-sheets/detail/rabies and say how long it recommends washing a bite wound. Cite this page.')]
for name,q in questions:
 start=time.monotonic(); data={'started_at':utc_now(),'control':name,'question':q,'method':'Same API model and search tool; only a helpful-assistant system prompt; not an acceptance case.'}
 try:
  r=client.responses.create(model='gpt-5.6-terra',input=[{'role':'system','content':'You are a helpful assistant.'},{'role':'user','content':q}],tools=[SearchTool.definition()],tool_choice='required',store=False,include=['web_search_call.action.sources'],reasoning={'effort':'low'},max_output_tokens=1500,timeout=35)
  data.update(response=public_response(r.model_dump(mode='json')),answer=r.output_text,status=r.status)
 except Exception as e:data.update(error=safe_error(e))
 data['seconds']=round(time.monotonic()-start,3);write_json(P/f'{name}.json',data); print(json.dumps({'control':name,'status':data.get('status'),'error':data.get('error')}),flush=True)
