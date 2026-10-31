"""Run the standalone package with only allowlisted API/model configuration."""
import os,subprocess,sys
from pathlib import Path
pid=subprocess.check_output(['systemctl','show','gaia-chatbot.service','-p','MainPID','--value'],text=True).strip()
active=Path('/proc')/pid
live=dict(item.split('=',1) for item in (active/'environ').read_bytes().decode().split('\0') if '=' in item)
python=(active/'cmdline').read_bytes().decode().split('\0')[0]
root=Path(__file__).resolve().parents[2]
allow={'OPENAI_API_KEY','OPENAI_WEB_SEARCH_MODEL','OPENAI_VISION_MODEL','OPENAI_ORG_ID','OPENAI_PROJECT_ID'}
env={key:value for key,value in os.environ.items() if key in {'PATH','HOME','LANG','LC_ALL','SSL_CERT_FILE','SSL_CERT_DIR'}}
env.update({key:value for key,value in live.items() if key in allow})
if not env.get('OPENAI_API_KEY'):raise SystemExit('No API credential; no test was started.')
run=sys.argv[1] if len(sys.argv)>1 else 'run1'
if run not in {'run1','run2','run3'}:raise SystemExit('Invalid run label')
subprocess.run([python,'-m','simple_agent','--suite','all58','--image-root',str(root),'--output',str(root/'reports/modular-agent-2026-10-28'/run)],env=env,cwd=root,check=True)
