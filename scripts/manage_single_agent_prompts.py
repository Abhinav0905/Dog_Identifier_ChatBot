#!/usr/bin/env python3
"""Validate or atomically activate V1. Re-activate a prior release to roll back."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from simple_agent.release import load_bundle
from scripts.manage_prompts import activate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "activate"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--current", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "activate":
            if args.current is None:
                parser.error("activate requires --current")
            result = activate(args.directory, args.current, loader=load_bundle)
        else:
            bundle = load_bundle(args.directory)
            result = {"version": bundle.version, "sha256": bundle.digest}
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Prompt package rejected: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
