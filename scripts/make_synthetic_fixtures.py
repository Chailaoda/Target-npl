"""Create wholly synthetic Web-history fixtures; never reads production runs."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT.parent / "state"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    states = ("partial", "completed", "failed", "needs_user_login")
    created = []
    for index, status in enumerate(states, 1):
        run_id = f"{index:032x}"
        folder = STATE / "data/task-runs" / run_id
        output = STATE / "output/task-runs" / run_id
        report = output / f"report-SYNTHETIC-{index}.html"
        export = output / f"results-SYNTHETIC-{index}.jsonl"
        output.mkdir(parents=True, exist_ok=True)
        report.write_text(f"<!doctype html><meta charset=utf-8><title>SYNTHETIC</title><p>SYNTHETIC-{status}</p>", encoding="utf-8")
        export.write_text(json.dumps({"source": {"text": "SYNTHETIC-TEXT", "synthetic": True}, "result": {"status": status}}) + "\n", encoding="utf-8")
        state = {
            "analysis_job_id": "a" * 32,
            "created_at": f"2026-01-0{index}T00:00:00Z",
            "dataset_id": "20260101T000000000000Z",
            "input_sha256": "b" * 64,
            "phase": "finished",
            "pid": 0,
            "result": {"report": str(report), "export": str(export), "status": status, "candidate_contents": 0, "unique_contents": 0},
            "revision": "a" * 32,
            "run_id": run_id,
            "status": status,
            "updated_at": f"2026-01-0{index}T00:01:00Z",
            "collection_incomplete": status == "partial",
            "incomplete_platforms": ["SYNTHETIC-PLATFORM"] if status == "partial" else [],
        }
        write(folder / "status.json", state)
        for name in ("request", "policy", "collection", "task", "launch", "collection-progress"):
            write(folder / f"{name}.json", {"synthetic": True, "value": f"SYNTHETIC-{name.upper()}"})
        (folder / "worker.log").write_text("SYNTHETIC-WORKER-LOG\n", encoding="utf-8")
        (folder / "platform-live").mkdir(exist_ok=True)
        created.append(run_id)
    evidence = STATE / "evidence/data-web-fixtures.json"
    write(evidence, {"status": "PASS", "fixture_ids": created, "values": "SYNTHETIC-* only", "production_run_files_read": False})
    print(json.dumps({"status": "PASS", "created": created}))


if __name__ == "__main__":
    main()
