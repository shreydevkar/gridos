"""Drive SpreadsheetBench Verified-400 against a running GridOS kernel.

Verified-400 file layout (different from the original 912):
    {dataset_dir}/spreadsheet/{id}/1_{id}_init.xlsx     <- the input
    {dataset_dir}/spreadsheet/{id}/1_{id}_golden.xlsx   <- the answer
    {dataset_dir}/spreadsheet/{id}/prompt.txt           <- (also-available instruction)
Only ONE test case per question — index always 1, no 2 or 3.

Pipeline per question:
    1. POST /system/clear          — reset the kernel to an empty workbook.
    2. POST /system/import.xlsx    — load the question's init.xlsx.
    3. POST /agent/chat            — instruction + agent_id="data_analyst"
                                     (forces the data-analyst agent via the
                                     ChatRequest override so the LLM router
                                     never enters the loop).
    4. POST /agent/apply           — commit the preview.
    5. GET  /system/export.xlsx?values_only=true
                                   — write computed cell values into the
                                     output xlsx, skipping formula strings so
                                     the evaluator's data_only=True read sees
                                     a populated cached-value column without
                                     a LibreOffice/Excel round-trip.

The output layout follows the same convention as the original 912's evaluator:
    {dataset_path}/outputs/{setting}_{model}/1_{id}_output.xlsx

Usage:
    # 1) Start a sandboxed GridOS kernel on a non-prod port (different cwd
    #    so it doesn't touch the dev system_state.gridos):
    cd bench/sandbox
    "../../.venv/Scripts/python.exe" -m uvicorn main:app --port 8001 --app-dir ../..

    # 2) From the project root, run the adapter:
    python scripts/run_spreadsheetbench.py --limit 3 --base http://localhost:8001

The default limit is 3 (smoke test). Bump --limit to 25-50 for a pilot, or
--limit 0 for the full 400. --start lets you resume after a crash.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import httpx


DEFAULT_DATASET = (
    Path(__file__).resolve().parent.parent
    / "bench"
    / "spreadsheetbench"
    / "spreadsheetbench_verified_400"
)


def parse_args():
    p = argparse.ArgumentParser(description="Run SpreadsheetBench against GridOS.")
    p.add_argument("--base", default="http://localhost:8001",
                   help="GridOS kernel base URL (default: http://localhost:8001).")
    p.add_argument("--dataset", default=str(DEFAULT_DATASET),
                   help="Path to the spreadsheetbench dataset directory.")
    p.add_argument("--setting", default="single",
                   help="Eval setting tag — gets baked into the output dir name.")
    p.add_argument("--model", default="gridos",
                   help="Model tag — gets baked into the output dir name.")
    p.add_argument("--agent-id", default="data_analyst",
                   help="GridOS agent to pin via ChatRequest.agent_id (skips router).")
    p.add_argument("--limit", type=int, default=3,
                   help="Max questions to run; 0 = the entire dataset.")
    p.add_argument("--start", type=int, default=0,
                   help="Skip the first N questions (resume after a crash).")
    p.add_argument("--per-call-timeout", type=float, default=240.0,
                   help="HTTP timeout in seconds for the agent chat call. "
                        "Bumped 180 -> 240 because the largest Sheet-Level "
                        "questions (700+ rows × 17 cols) consume the full "
                        "180s on free-tier Gemini and time out late.")
    p.add_argument("--rate-limit-delay", type=float, default=0.0,
                   help="Sleep N seconds between questions to ride free-tier RPM caps.")
    p.add_argument("--retry", type=int, default=2,
                   help="Retries on transient HTTP/5xx errors per call.")
    return p.parse_args()


class BenchClient:
    def __init__(self, base: str, agent_id: str, timeout: float, retry: int):
        self.base = base.rstrip("/")
        self.agent_id = agent_id
        self.retry = retry
        self.client = httpx.Client(timeout=timeout)

    def close(self):
        self.client.close()

    def _post_with_retry(self, path: str, **kwargs):
        last = None
        for attempt in range(self.retry + 1):
            try:
                r = self.client.post(self.base + path, **kwargs)
                if r.status_code == 200:
                    return r
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                # 5xx retried; 4xx usually deterministic, no point retrying
                if 400 <= r.status_code < 500:
                    return r
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
            time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"POST {path} failed after {self.retry + 1} attempts: {last}")

    def _get_with_retry(self, path: str, **kwargs):
        last = None
        for attempt in range(self.retry + 1):
            try:
                r = self.client.get(self.base + path, **kwargs)
                if r.status_code == 200:
                    return r
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if 400 <= r.status_code < 500:
                    return r
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
            time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"GET {path} failed after {self.retry + 1} attempts: {last}")

    def reset(self):
        return self._post_with_retry("/system/clear")

    def import_xlsx(self, xlsx_path: Path):
        with open(xlsx_path, "rb") as f:
            files = {"file": (xlsx_path.name, f.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        return self._post_with_retry("/system/import.xlsx", files=files)

    def chat(self, instruction: str) -> dict:
        body = {
            "prompt": instruction,
            "scope": "sheet",
            "selected_cells": [],
            "history": [],
            "agent_id": self.agent_id,
        }
        r = self._post_with_retry("/agent/chat", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"/agent/chat failed: {r.status_code} {r.text[:200]}")
        return r.json()

    def apply(self, preview_token: str, payload: dict) -> dict:
        # /agent/apply requires the preview_token plus the same shape the
        # preview returned. We forward the payload fields the endpoint
        # expects: agent_id, target_cell/values OR intents, shift_direction,
        # chart_spec.
        body = {
            "preview_token": preview_token,
            "agent_id": payload.get("category") or self.agent_id,
            "target_cell": payload.get("target_cell"),
            "values": payload.get("values"),
            "intents": payload.get("intents"),
            "shift_direction": payload.get("shift_direction") or "right",
            "chart_spec": payload.get("chart_spec"),
            "sheet": payload.get("sheet"),
        }
        r = self._post_with_retry("/agent/apply", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"/agent/apply failed: {r.status_code} {r.text[:200]}")
        return r.json()

    def export_values(self, out_path: Path):
        r = self._get_with_retry("/system/export.xlsx", params={"values_only": "true"})
        if r.status_code != 200:
            raise RuntimeError(f"/system/export.xlsx failed: {r.status_code} {r.text[:200]}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(r.content)


def run_one_test_case(client: BenchClient, instruction: str, input_xlsx: Path, output_xlsx: Path) -> tuple[bool, str]:
    try:
        client.reset()
    except Exception as e:
        return False, f"reset failed: {e}"
    try:
        client.import_xlsx(input_xlsx)
    except Exception as e:
        return False, f"import failed: {e}"
    try:
        chat_resp = client.chat(instruction)
    except Exception as e:
        return False, f"chat failed: {e}"

    preview_token = chat_resp.get("preview_token")
    if not preview_token:
        # Some chat responses surface an error text instead of a preview when
        # the agent decided the question was unanswerable. Treat as a soft
        # failure (we still write the input xlsx as the output so the eval
        # can score it as 0 without crashing).
        try:
            output_xlsx.parent.mkdir(parents=True, exist_ok=True)
            output_xlsx.write_bytes(input_xlsx.read_bytes())
        except Exception:
            pass
        return False, f"no preview_token in chat response: {str(chat_resp)[:200]}"

    try:
        client.apply(preview_token, chat_resp)
    except Exception as e:
        return False, f"apply failed: {e}"
    try:
        client.export_values(output_xlsx)
    except Exception as e:
        return False, f"export failed: {e}"
    return True, "ok"


def main() -> int:
    args = parse_args()
    dataset_dir = Path(args.dataset).resolve()
    if not (dataset_dir / "dataset.json").exists():
        print(f"[error] dataset.json not found at {dataset_dir}", file=sys.stderr)
        return 2

    with open(dataset_dir / "dataset.json", "r", encoding="utf-8") as fp:
        dataset = json.load(fp)

    if args.start:
        dataset = dataset[args.start:]
    if args.limit > 0:
        dataset = dataset[: args.limit]

    out_root = dataset_dir / "outputs" / f"{args.setting}_{args.model}"
    out_root.mkdir(parents=True, exist_ok=True)

    client = BenchClient(args.base, args.agent_id, args.per_call_timeout, args.retry)

    summary = {
        "n_questions": len(dataset),
        "n_test_cases": 0,
        "n_pipeline_ok": 0,
        "n_pipeline_fail": 0,
        "agent_id": args.agent_id,
        "base": args.base,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "errors": [],
    }

    try:
        for q_idx, item in enumerate(dataset, start=1):
            # IDs in dataset.json are sometimes pure-numeric ('12307') and load
            # as int from JSON. Coerce to str so Path / qid works.
            qid = str(item["id"])
            instruction = item["instruction"]
            ans_pos = item.get("answer_position", "")
            print(f"[{q_idx}/{len(dataset)}] id={qid}  pos={ans_pos[:40]}")
            tc = 1  # Verified-400 ships only one test case per question
            inp = dataset_dir / "spreadsheet" / qid / f"{tc}_{qid}_init.xlsx"
            out = out_root / f"{tc}_{qid}_output.xlsx"
            if not inp.exists():
                print(f"    SKIP (no init xlsx at {inp})")
                continue
            summary["n_test_cases"] += 1
            ok, msg = run_one_test_case(client, instruction, inp, out)
            if ok:
                summary["n_pipeline_ok"] += 1
                print(f"    ok")
            else:
                summary["n_pipeline_fail"] += 1
                print(f"    FAIL: {msg}")
                summary["errors"].append({"id": qid, "tc": tc, "msg": msg})
            if args.rate_limit_delay > 0:
                time.sleep(args.rate_limit_delay)
    finally:
        client.close()

    summary["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    log_path = out_root.parent / f"run_log_{args.setting}_{args.model}.json"
    with open(log_path, "w", encoding="utf-8") as fp:
        json.dump(summary, fp, indent=2)

    pct = (summary["n_pipeline_ok"] / summary["n_test_cases"] * 100) if summary["n_test_cases"] else 0
    print()
    print(f"=== Pipeline summary ===")
    print(f"questions:     {summary['n_questions']}")
    print(f"test cases:    {summary['n_test_cases']}")
    print(f"pipeline ok:   {summary['n_pipeline_ok']} ({pct:.1f}%)")
    print(f"pipeline fail: {summary['n_pipeline_fail']}")
    print(f"log:           {log_path}")
    print()
    print("Pipeline 'ok' just means the request -> preview -> apply -> export round-tripped.")
    print("To score correctness, run SpreadsheetBench's evaluation.py against")
    print(f"  {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
