#!/usr/bin/env python3
"""Run prompt stress in per-case subprocesses with timeout protection."""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "tests" / "prompt_stress_cases.json"
SINGLE_RUNNER = ROOT / "scripts" / "prompt_stress.py"


def _load_cases(case_filter: set[str] | None = None) -> list[dict]:
    all_cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if not case_filter:
        return all_cases
    return [c for c in all_cases if c.get("id") in case_filter]


def _run_one(case_id: str, timeout_sec: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="contextlife_stress_case_") as td:
        report_path = Path(td) / "report.json"
        cmd = [
            sys.executable,
            str(SINGLE_RUNNER),
            "--cases",
            case_id,
            "--rounds",
            "1",
            "--report",
            str(report_path),
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "case_id": case_id,
                "ok": False,
                "detail": f"timeout>{timeout_sec}s",
                "returncode": None,
            }

        detail = ""
        ok = False
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                ok = report.get("failed", 1) == 0
                if ok:
                    detail = "pass"
                else:
                    failed_items = [r for r in report.get("results", []) if not r.get("ok")]
                    detail = failed_items[0]["detail"] if failed_items else "failed"
            except Exception as exc:
                detail = f"bad report: {exc}"
                ok = False
        else:
            detail = "missing report"
            ok = False

        if proc.returncode not in (0, 2):
            detail = f"runner rc={proc.returncode}; stderr={proc.stderr.strip()[:200]}"
            ok = False

        return {
            "case_id": case_id,
            "ok": ok,
            "detail": detail,
            "returncode": proc.returncode,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Matrix stress runner with timeout per case")
    parser.add_argument("--cases", default="", help="Comma-separated case ids")
    parser.add_argument("--rounds", type=int, default=1, help="Repeat all selected cases this many rounds")
    parser.add_argument("--shuffle", action="store_true", help="Shuffle case order each round")
    parser.add_argument("--timeout", type=int, default=90, help="Timeout seconds for each single-case run")
    parser.add_argument("--report", default="", help="Write matrix report json")
    args = parser.parse_args()

    case_filter = {x.strip() for x in args.cases.split(",") if x.strip()} if args.cases else None
    cases = _load_cases(case_filter)
    if not cases:
        print("No cases selected.")
        return 1

    print(f"[matrix] selected={len(cases)}, rounds={args.rounds}, timeout={args.timeout}s")
    all_results = []

    for rnd in range(args.rounds):
        ordered = list(cases)
        if args.shuffle:
            random.shuffle(ordered)
        print(f"\n=== Round {rnd + 1}/{args.rounds} ===")
        for case in ordered:
            case_id = case["id"]
            result = _run_one(case_id, timeout_sec=args.timeout)
            all_results.append(result)
            marker = "PASS" if result["ok"] else "FAIL"
            print(f"[{marker}] {case_id:28s} {result['detail']}")

    total = len(all_results)
    failed = [r for r in all_results if not r["ok"]]
    passed = total - len(failed)
    rate = (passed / total * 100.0) if total else 0.0

    print("\n=== Matrix Summary ===")
    print(f"Total: {total}")
    print(f"Pass : {passed}")
    print(f"Fail : {len(failed)}")
    print(f"Rate : {rate:.1f}%")

    report_obj = {
        "total": total,
        "passed": passed,
        "failed": len(failed),
        "rate": round(rate, 2),
        "results": all_results,
    }
    if args.report:
        out = Path(args.report).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report_obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Report written: {out}")

    if failed:
        print("\nFailed cases:")
        for item in failed:
            print(f"- {item['case_id']}: {item['detail']}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
