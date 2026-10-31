"""Export one authorized synthetic conversation's recorded traces, read-only.

Run in the deployed application's configured environment. No model/UI calls.
"""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import database as db
from services import agent_trace


def export(session_id, since):
    with db.get_db() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT id,role,content,metadata_json AS metadata,created_at FROM chat_history WHERE session_id=? AND created_at>=? ORDER BY id",
            (session_id, since))]
    turns = []
    pending = None
    for row in rows:
        if row["role"] == "user":
            pending = row
        elif row["role"] == "assistant" and pending:
            metadata = json.loads(row["metadata"] or "{}")
            trace_id = metadata.get("stage_trace", {}).get("id")
            turns.append({"question": pending["content"], "user_row_id": pending["id"],
                "assistant_row_id": row["id"], "response": row["content"], "created_at": row["created_at"],
                "metadata": metadata, "trace": agent_trace.read(trace_id) if trace_id else None})
            pending = None
    return {"method": "Read-only saved backend traces; browser answers are captured separately.",
            "session_id": session_id, "since": since, "turns": turns}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(export(args.session, args.since), ensure_ascii=False, indent=2) + "\n")
