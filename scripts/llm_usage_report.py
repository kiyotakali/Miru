#!/usr/bin/env python3
"""LLM token usage report — read data/_admin/llm_usage.jsonl and roll up.

Usage:
    python3 scripts/llm_usage_report.py                # all-time
    python3 scripts/llm_usage_report.py --since 1h     # last hour
    python3 scripts/llm_usage_report.py --since 2026-05-11T14:28
    python3 scripts/llm_usage_report.py --by-user      # group by user_id too
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from collections import defaultdict


def parse_since(s: str) -> float:
    if not s:
        return 0.0
    s = s.strip()
    if s.endswith("h") and s[:-1].replace(".", "").isdigit():
        return time.time() - float(s[:-1]) * 3600
    if s.endswith("m") and s[:-1].replace(".", "").isdigit():
        return time.time() - float(s[:-1]) * 60
    if s.endswith("d") and s[:-1].replace(".", "").isdigit():
        return time.time() - float(s[:-1]) * 86400
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        try:
            return float(s)
        except ValueError:
            raise SystemExit(f"can't parse --since {s!r}; try 2h / 30m / 2026-05-11T14:28")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="time floor: 2h / 30m / 1d / ISO timestamp / unix")
    ap.add_argument("--until", default="", help="time ceiling (default: now)")
    ap.add_argument("--by-user", action="store_true", help="break down by user_id")
    ap.add_argument("--log", default=None, help="path to llm_usage.jsonl (default: $DATA_DIR/_admin/llm_usage.jsonl)")
    args = ap.parse_args()

    base = os.environ.get("DATA_DIR", "data")
    path = args.log or os.path.join(base, "_admin", "llm_usage.jsonl")
    if not os.path.isfile(path):
        print(f"no usage log at {path}")
        return

    since = parse_since(args.since)
    until = parse_since(args.until) if args.until else time.time()

    def empty():
        return {
            "calls": 0, "prompt": 0, "completion": 0, "total": 0,
            "cache_hit": 0, "cache_miss": 0, "reasoning": 0,
        }

    by_tier = defaultdict(empty)
    by_user_tier = defaultdict(lambda: defaultdict(empty))
    by_model = defaultdict(lambda: {"calls": 0, "total": 0})
    grand = empty()

    n_lines = n_in = 0
    first_ts = None
    last_ts = None
    with open(path) as f:
        for line in f:
            n_lines += 1
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts = r.get("ts", 0)
            if ts < since or ts > until:
                continue
            n_in += 1
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
            tier = r.get("tier", "?")
            model = r.get("model", "?")
            uid = r.get("user_id") or "(no-user)"
            pt = r.get("prompt_tokens", 0)
            ct = r.get("completion_tokens", 0)
            tt = r.get("total_tokens", 0) or (pt + ct)
            hit = r.get("prompt_cache_hit_tokens", 0) or 0
            miss = r.get("prompt_cache_miss_tokens", 0) or 0
            reasoning = r.get("reasoning_tokens", 0) or 0

            d = by_tier[tier]
            d["calls"] += 1
            d["prompt"] += pt
            d["completion"] += ct
            d["total"] += tt
            d["cache_hit"] += hit
            d["cache_miss"] += miss
            d["reasoning"] += reasoning

            d2 = by_user_tier[uid][tier]
            d2["calls"] += 1
            d2["prompt"] += pt
            d2["completion"] += ct
            d2["total"] += tt
            d2["cache_hit"] += hit
            d2["cache_miss"] += miss
            d2["reasoning"] += reasoning

            dm = by_model[model]
            dm["calls"] += 1
            dm["total"] += tt

            grand["calls"] += 1
            grand["prompt"] += pt
            grand["completion"] += ct
            grand["total"] += tt
            grand["cache_hit"] += hit
            grand["cache_miss"] += miss
            grand["reasoning"] += reasoning

    def fmt(d):
        s = (f"calls={d['calls']:5d}  prompt={d['prompt']:>8,}  "
             f"completion={d['completion']:>8,}  total={d['total']:>8,}")
        if d.get("cache_hit") or d.get("cache_miss") or d.get("reasoning"):
            s += (f"  cache_hit={d['cache_hit']:>8,}"
                  f"  cache_miss={d['cache_miss']:>8,}"
                  f"  reasoning={d['reasoning']:>8,}")
        return s

    if first_ts:
        dur_min = (last_ts - first_ts) / 60
        print(f"window: {datetime.fromtimestamp(first_ts).isoformat(timespec='seconds')} → "
              f"{datetime.fromtimestamp(last_ts).isoformat(timespec='seconds')}  "
              f"({dur_min:.1f} min, {n_in}/{n_lines} rows)")
    else:
        print(f"no rows in window (total in log: {n_lines})")
        return

    print()
    print("== by tier ==")
    for tier in sorted(by_tier):
        print(f"  {tier:8s}  {fmt(by_tier[tier])}")
    print(f"  {'TOTAL':8s}  {fmt(grand)}")

    print()
    print("== by model ==")
    for model in sorted(by_model, key=lambda m: -by_model[m]["total"]):
        d = by_model[model]
        print(f"  {model:40s}  calls={d['calls']:5d}  total={d['total']:>8,}")

    if args.by_user:
        print()
        print("== by user × tier ==")
        for uid in sorted(by_user_tier):
            print(f"  {uid}")
            for tier in sorted(by_user_tier[uid]):
                print(f"    {tier:8s}  {fmt(by_user_tier[uid][tier])}")


if __name__ == "__main__":
    main()
