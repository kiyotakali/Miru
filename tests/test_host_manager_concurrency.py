import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="host_manager.py uses the Linux VPS fcntl lock implementation",
)


ROOT = Path(__file__).resolve().parents[1]
HOST_MANAGER_PY = ROOT / "scripts" / "host_manager.py"


def run_json(*args):
    result = subprocess.run(
        [sys.executable, str(HOST_MANAGER_PY), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(result.stdout)


def allocate(home: Path, instance_id: str):
    result = subprocess.run(
        [
            sys.executable,
            str(HOST_MANAGER_PY),
            "allocate-instance",
            "--home",
            str(home),
            "--instance-id",
            instance_id,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return instance_id, result.returncode, result.stdout, result.stderr


def test_parallel_allocation_uses_lock_and_unique_ports(tmp_path):
    home = tmp_path / "miru-host"
    run_json(
        "init-host",
        "--home",
        str(home),
        "--server-ip",
        "127.0.0.1",
        "--port-start",
        "5901",
        "--port-end",
        "5904",
        "--default-image",
        "miru/server:test",
    )

    ids = [f"inst-par-{idx}" for idx in range(8)]
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(allocate, home, instance_id) for instance_id in ids]
        for future in as_completed(futures):
            results.append(future.result())

    successes = []
    failures = []
    for instance_id, code, stdout, stderr in results:
        if code == 0:
            payload = json.loads(stdout)
            successes.append(payload["instance"])
        else:
            failures.append((instance_id, stderr))

    assert len(successes) == 4
    assert len(failures) == 4
    assert sorted(item["server_port"] for item in successes) == [5901, 5902, 5903, 5904]
    assert len({item["instance_id"] for item in successes}) == 4
    assert all("no free port" in stderr for _, stderr in failures)

    audit = run_json("audit", "--home", str(home))
    assert audit["ok"] is True
    instances = run_json("list-instances", "--home", str(home))
    assert instances["count"] == 4
