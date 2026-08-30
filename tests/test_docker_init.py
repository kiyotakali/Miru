import json
import os
import subprocess
import sys


def _run_init(data_dir, *, ip="127.0.0.1", port="5081"):
    env = {
        **os.environ,
        "DATA_DIR": str(data_dir),
        "LOG_DIR": str(data_dir / "_logs"),
        "SERVER_IP": ip,
        "SERVER_PORT": str(port),
        "PORT": "5001",
        "MIRU_HEADLESS": "1",
    }
    return subprocess.run(
        [sys.executable, "scripts/docker_init.py"],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def _bootstrap(data_dir):
    return json.loads((data_dir / "_admin" / "docker_bootstrap.json").read_text())


def test_docker_init_creates_and_reuses_single_invitation(tmp_path):
    first = _run_init(tmp_path)
    assert "MIRU_INVITATION_CODE=MIRU-" in first.stdout
    b1 = _bootstrap(tmp_path)
    assert b1["server_ip"] == "127.0.0.1"
    assert b1["server_port"] == 5081
    assert b1["local_code"].startswith("MIRU-")
    assert b1["current_full_code"].startswith("MIRU-")

    second = _run_init(tmp_path)
    b2 = _bootstrap(tmp_path)
    assert b2["local_code"] == b1["local_code"]
    assert b2["current_full_code"] == b1["current_full_code"]


def test_docker_init_recomposes_full_code_when_server_address_changes(tmp_path):
    _run_init(tmp_path, ip="127.0.0.1", port="5081")
    b1 = _bootstrap(tmp_path)

    _run_init(tmp_path, ip="192.168.50.23", port="5081")
    b2 = _bootstrap(tmp_path)

    assert b2["local_code"] == b1["local_code"]
    assert b2["current_full_code"] != b1["current_full_code"]
    assert b2["server_ip"] == "192.168.50.23"
    assert b2["server_port"] == 5081


def test_docker_init_rejects_missing_server_ip(tmp_path):
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path),
        "LOG_DIR": str(tmp_path / "_logs"),
        "SERVER_IP": "",
        "SERVER_PORT": "5081",
    }
    proc = subprocess.run(
        [sys.executable, "scripts/docker_init.py"],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 2
    assert "SERVER_IP is required" in proc.stderr
