"""New locked acceptance cases; model never sees the expected-answer rubric."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from simple_agent.agent import AgentConfig, SingleAgent
from simple_agent.catalog import PromptCatalog
from simple_agent.__main__ import code_hashes
from simple_agent.tracing import utc_now, write_json
from openai import OpenAI

REPORT = Path(__file__).resolve().parent
run = sys.argv[1] if len(sys.argv) > 1 else 'run1'
if run not in {'run1', 'run2', 'run3'}:
    raise SystemExit('Invalid run label')
output = REPORT / run
output.mkdir(exist_ok=False)
groups = json.loads((REPORT / 'cases.json').read_text())
prompts = PromptCatalog.load()
config = AgentConfig(model=os.getenv('OPENAI_WEB_SEARCH_MODEL', 'gpt-5.6-terra'),
                     vision_model=os.getenv('OPENAI_VISION_MODEL', 'gpt-5.6-terra'))
agent = SingleAgent(OpenAI(max_retries=0), config, prompts)
runtime = {'started_at': utc_now(), 'method': 'Fresh 50-question live API acceptance set; not a production UI run',
           'config': asdict(config), 'code_hashes': code_hashes(), 'prompt_hashes': prompts.hashes(),
           'case_file_sha256': hashlib.sha256((REPORT / 'cases.json').read_bytes()).hexdigest(),
           'groups': {k: len(v) for k, v in groups.items()}, 'acceptance': 'At least 45 fully passing of 50; partials do not count as pass',
           's3_reads': 0, 'production_changes': False, 'outbound_alerts': False}
write_json(output / 'runtime.json', runtime)
write_json(output / 'test-plan.json', groups)
write_json(output / 'prompts.json', prompts.snapshot())


def run_group(item):
    name, cases = item
    history, rows = [], []
    for case in cases:
        image_path = ROOT / case['image'] if case.get('image') else None
        result = agent.turn(case['question'], history, image_path=image_path, trace_path=output / 'traces' / (case['id'] + '.jsonl'))
        row = {**case, **result, 'group': name}
        write_json(output / (case['id'] + '.json'), row)
        rows.append(row)
        print(json.dumps({'id': case['id'], 'status': row['status'], 'seconds': row['elapsed_seconds']}), flush=True)
    return rows


with ThreadPoolExecutor(max_workers=3) as pool:
    rows = [row for batch in pool.map(run_group, groups.items()) for row in batch]
write_json(output / 'results.json', {'runtime': runtime, 'cases': rows})
print(json.dumps({'finished_at': utc_now(), 'cases': len(rows), 'completed': sum(r['status'] == 'completed' for r in rows), 'judged': False}), flush=True)
