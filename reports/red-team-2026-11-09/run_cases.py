"""Replay supplied red-team cases; fresh chat per case, ordered turns within it.

High-severity cases run three times. Results remain UNREVIEWED until semantic
review; successful API completion is never a correctness verdict.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from openai import OpenAI
from simple_agent.agent import AgentConfig, SingleAgent
from simple_agent.catalog import PromptCatalog
from simple_agent.tracing import utc_now, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.json"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cases = json.loads(args.cases.read_text())
    if args.probe:
        cases = [c for c in cases if c["id"] in {"A4", "B2", "B3", "C7", "D1", "D3", "E1", "E4", "J1", "J2", "J3"}]
    tasks = [(c, repeat) for c in cases for repeat in range(1, (1 if args.probe or c["severity"] != "High" else 3) + 1)]
    prompts = PromptCatalog.load()
    config = AgentConfig()
    runtime = {"started_at": utc_now(), "method": "Isolated live Responses API replay; not a production UI run",
               "prompt_hashes": prompts.hashes(), "config": asdict(config),
               "files": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                         for folder in ("simple_agent", "services", "prompts/V1")
                         for p in sorted((ROOT / folder).rglob("*")) if p.suffix in {".py", ".txt", ".json"}},
               "unique_cases": len(cases), "case_runs": len(tasks), "production_changes": False,
               "outbound_alerts": False, "scoring": "Manual semantic review; worst repeat determines each case verdict"}
    write_json(args.output / "runtime.json", runtime)
    write_json(args.output / "test-plan.json", cases)
    agent = SingleAgent(OpenAI(max_retries=0), config, prompts)

    def run(item):
        case, repeat = item
        history, turns = [], []
        ident = f'{case["id"]}-r{repeat}'
        for turn, question in enumerate(case["turns"], 1):
            image = ROOT / case["image"] if case.get("image") and turn == 1 else None
            result = agent.turn(question, history, image_path=image,
                                trace_path=args.output / "traces" / f"{ident}-t{turn}.jsonl")
            turns.append(result)
        row = {**case, "repeat": repeat, "results": turns, "verdict": "UNREVIEWED"}
        write_json(args.output / f"{ident}.json", row)
        print(json.dumps({"id": ident, "statuses": [r["status"] for r in turns],
                          "seconds": round(sum(r["elapsed_seconds"] for r in turns), 1)}), flush=True)
        return row

    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(run, tasks))
    write_json(args.output / "results.json", {"runtime": runtime, "cases": rows})


if __name__ == "__main__":
    main()
