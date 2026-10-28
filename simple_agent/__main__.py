"""Run one question or the fixed acceptance suite with a new trace directory."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

from .agent import AgentConfig, SingleAgent
from .catalog import PromptCatalog
from .tracing import utc_now, write_json


def code_hashes():
    root = Path(__file__).parent
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.suffix in {".py", ".txt", ".json"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--suite", choices=["core50", "all58"])
    selection.add_argument("--question")
    parser.add_argument("--image")
    parser.add_argument("--image-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--model", default=os.getenv("OPENAI_WEB_SEARCH_MODEL", "gpt-5.6-terra"))
    parser.add_argument("--vision-model", default=os.getenv("OPENAI_VISION_MODEL", "gpt-5.6-terra"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if args.image and not args.question:
        parser.error("--image is for --question; the suite has its own fixtures")
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required; no simulated results will be reported")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    if args.suite:
        groups = json.loads((Path(__file__).parent / "evaluation/cases.json").read_text())
        if args.suite == "core50":
            groups = {k: groups[k] for k in ("original40", "new10")}
    else:
        groups = {"question": [{"id": "Q01", "question": args.question, "image": args.image,
                                 "expected": "Answer the supplied question directly and accurately."}]}
    prompts = PromptCatalog.load()
    config = AgentConfig(model=args.model, vision_model=args.vision_model)
    from openai import OpenAI
    agent = SingleAgent(OpenAI(max_retries=0), config, prompts)
    runtime = {"started_at": utc_now(), "method": "Isolated live Responses API; not a production UI test",
               "config": asdict(config), "tools": ["web_search", "analyze_photo"],
               "system_lines": len(prompts.system.splitlines()), "prompt_hashes": prompts.hashes(),
               "code_hashes": code_hashes(), "groups": {k: len(v) for k, v in groups.items()},
               "s3_reads": 0, "production_database_access": False, "production_changes": False,
               "outbound_alerts": False, "acceptance": "At least 45 fully passing cases out of core50; partial does not count as pass"}
    write_json(output / "runtime.json", runtime)
    write_json(output / "prompts.json", prompts.snapshot())
    write_json(output / "test-plan.json", groups)

    def run_group(item):
        group, cases = item
        history, rows = [], []
        for case in cases:
            image = Path(args.image_root) / case["image"] if case.get("image") else None
            row = agent.turn(case["question"], history, image_path=image,
                             trace_path=output / "traces" / (case["id"] + ".jsonl"))
            row = {**case, **row, "group": group}
            write_json(output / (case["id"] + ".json"), row)
            rows.append(row)
            print(json.dumps({"id": case["id"], "status": row["status"], "seconds": row["elapsed_seconds"]}), flush=True)
        return rows

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
        rows = [row for batch in pool.map(run_group, groups.items()) for row in batch]
    write_json(output / "results.json", {"runtime": runtime, "cases": rows})
    print(json.dumps({"finished_at": utc_now(), "cases": len(rows),
                      "completed": sum(r["status"] == "completed" for r in rows), "judged": False}), flush=True)


if __name__ == "__main__":
    main()
