import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="host_manager.py is uploaded to and executed on the Linux VPS",
)


ROOT = Path(__file__).resolve().parents[1]
HOST_MANAGER = ROOT / "deploy" / "host_manager"
HOST_MANAGER_PY = ROOT / "scripts" / "host_manager.py"

SCRIPT_NAMES = [
    "common.sh",
    "sync_tools.sh",
    "sync_remote_tools.sh",
    "host_install.sh",
    "create_instance.sh",
    "list_instances.sh",
    "status_instance.sh",
    "backup_instance.sh",
    "restore_instance.sh",
    "reset_instance.sh",
    "update_instance.sh",
    "delete_instance.sh",
    "audit_host.sh",
    "e2e_four_instances.sh",
]


def run_json(*args, **kwargs):
    result = subprocess.run(
        [sys.executable, str(HOST_MANAGER_PY), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        **kwargs,
    )
    return json.loads(result.stdout)


def test_host_manager_scripts_exist_and_are_syntax_valid():
    for name in SCRIPT_NAMES:
        path = HOST_MANAGER / name
        assert path.exists(), name
        if path.suffix == ".sh":
            subprocess.run(["bash", "-n", str(path)], check=True)
    subprocess.run([sys.executable, "-m", "py_compile", str(HOST_MANAGER_PY)], check=True)


def test_host_manager_entrypoints_show_help_without_root_or_docker():
    for name in SCRIPT_NAMES:
        if name == "common.sh":
            continue
        path = HOST_MANAGER / name
        result = subprocess.run(
            ["bash", str(path), "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        assert "Usage:" in result.stdout


def test_host_manager_defaults_are_current_release_and_cloud_neutral():
    common = (HOST_MANAGER / "common.sh").read_text(encoding="utf-8")
    source = HOST_MANAGER_PY.read_text(encoding="utf-8")

    assert 'MIRU_IMAGE_VERSION="${MIRU_IMAGE_VERSION:-0.2.0}"' in common
    assert '"miru/server:0.2.0"' in source
    assert "metadata.tencentyun.com" not in common
    assert "Host manager installer supports" not in common


def test_host_install_initializes_ledgers_without_docker(tmp_path):
    home = tmp_path / "miru-host"
    result = subprocess.run(
        [
            "bash",
            str(HOST_MANAGER / "host_install.sh"),
            "--home",
            str(home),
            "--server-ip",
            "127.0.0.1",
            "--port-start",
            "5601",
            "--port-end",
            "5604",
            "--default-image",
            "miru/server:test",
            "--skip-docker-install",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert (home / "host.json").exists()
    assert (home / "instances.json").exists()
    assert (home / "ports.json").exists()
    assert oct((home / "instances.json").stat().st_mode & 0o777) == "0o600"
    host = json.loads((home / "host.json").read_text(encoding="utf-8"))
    assert host["server_ip"] == "127.0.0.1"
    assert host["port_range"] == {"start": 5601, "end": 5604}
    assert host["default_image"] == "miru/server:test"
    assert (home / "tools" / "current" / "deploy" / "host_manager" / "reset_instance.sh").exists()
    assert (home / "tools" / "current" / "deploy" / "self_host" / "reset.sh").exists()
    assert (home / "tools" / "current" / "scripts" / "host_manager.py").exists()
    assert (home / "deploy" / "host_manager" / "status_instance.sh").exists()
    assert (home / "scripts" / "host_manager.py").exists()


def test_sync_tools_persists_maintainer_bundle_into_host_home(tmp_path):
    home = tmp_path / "miru-host"
    result = subprocess.run(
        [
            "bash",
            str(HOST_MANAGER / "sync_tools.sh"),
            "--home",
            str(home),
            "--source-root",
            str(ROOT),
            "--json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["tools_dir"] == str(home / "tools" / "current")
    assert (home / "deploy" / "host_manager" / "sync_tools.sh").exists()
    assert (home / "deploy" / "self_host" / "common.sh").exists()
    assert (home / "scripts" / "host_manager.py").exists()
    assert not list((home / "tools" / "current").rglob("__pycache__"))
    assert not list((home / "tools" / "current").rglob("*.pyc"))

    copied_help = subprocess.run(
        [
            "bash",
            str(home / "deploy" / "host_manager" / "status_instance.sh"),
            "--help",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert "Usage:" in copied_help.stdout


def test_ledger_allocates_unique_ports_and_reuses_after_remove(tmp_path):
    home = tmp_path / "miru-host"
    run_json(
        "init-host",
        "--home",
        str(home),
        "--server-ip",
        "127.0.0.1",
        "--port-start",
        "5601",
        "--port-end",
        "5602",
        "--default-image",
        "miru/server:test",
    )
    first = run_json(
        "allocate-instance",
        "--home",
        str(home),
        "--instance-id",
        "inst-alpha",
    )["instance"]
    second = run_json(
        "allocate-instance",
        "--home",
        str(home),
        "--instance-id",
        "inst-beta",
    )["instance"]
    assert first["server_port"] == 5601
    assert second["server_port"] == 5602
    assert first["instance_home"] != second["instance_home"]
    assert first["container_name"] == "miru-inst-alpha"
    assert second["compose_project"] == "miru_inst_beta"

    collision = subprocess.run(
        [
            sys.executable,
            str(HOST_MANAGER_PY),
            "allocate-instance",
            "--home",
            str(home),
            "--instance-id",
            "inst-gamma",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert collision.returncode == 2
    assert "no free port" in collision.stderr

    run_json("remove-instance", "--home", str(home), "--instance-id", "inst-alpha")
    third = run_json(
        "allocate-instance",
        "--home",
        str(home),
        "--instance-id",
        "inst-gamma",
    )["instance"]
    assert third["server_port"] == 5601


def _busy_port_with_free_successor():
    for _attempt in range(100):
        busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        busy.bind(("0.0.0.0", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        if port >= 65535:
            busy.close()
            continue
        successor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            successor.bind(("0.0.0.0", port + 1))
        except OSError:
            busy.close()
            successor.close()
            continue
        successor.close()
        return busy, port, port + 1
    raise AssertionError("could not find adjacent ports for host allocation test")


def test_allocator_skips_port_used_outside_its_ledger(tmp_path):
    busy, occupied_port, free_port = _busy_port_with_free_successor()
    try:
        home = tmp_path / "miru-host"
        run_json(
            "init-host",
            "--home",
            str(home),
            "--server-ip",
            "127.0.0.1",
            "--port-start",
            str(occupied_port),
            "--port-end",
            str(free_port),
            "--default-image",
            "miru/server:test",
        )

        instance = run_json(
            "allocate-instance",
            "--home",
            str(home),
            "--instance-id",
            "inst-system-port",
        )["instance"]

        assert instance["server_port"] == free_port
        assert instance["port_selection"]["skipped_system_ports"] == [occupied_port]
    finally:
        busy.close()


def test_allocator_rejects_explicit_port_used_outside_its_ledger(tmp_path):
    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    busy.bind(("0.0.0.0", 0))
    busy.listen(1)
    occupied_port = busy.getsockname()[1]
    try:
        home = tmp_path / "miru-host"
        run_json(
            "init-host",
            "--home",
            str(home),
            "--server-ip",
            "127.0.0.1",
            "--port-start",
            str(occupied_port),
            "--port-end",
            str(occupied_port),
            "--default-image",
            "miru/server:test",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(HOST_MANAGER_PY),
                "allocate-instance",
                "--home",
                str(home),
                "--instance-id",
                "inst-explicit-busy",
                "--port",
                str(occupied_port),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        assert result.returncode == 2
        assert f"port already in use on host: {occupied_port}" in result.stderr
        assert run_json("list-instances", "--home", str(home))["count"] == 0
    finally:
        busy.close()


def test_allocator_fails_cleanly_when_system_uses_entire_range(tmp_path):
    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    busy.bind(("0.0.0.0", 0))
    busy.listen(1)
    occupied_port = busy.getsockname()[1]
    try:
        home = tmp_path / "miru-host"
        run_json(
            "init-host",
            "--home",
            str(home),
            "--server-ip",
            "127.0.0.1",
            "--port-start",
            str(occupied_port),
            "--port-end",
            str(occupied_port),
            "--default-image",
            "miru/server:test",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(HOST_MANAGER_PY),
                "allocate-instance",
                "--home",
                str(home),
                "--instance-id",
                "inst-no-system-port",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        assert result.returncode == 2
        assert "no free port" in result.stderr
        assert run_json("list-instances", "--home", str(home))["count"] == 0
    finally:
        busy.close()


def test_allocate_writes_ports_before_instances_to_avoid_duplicate_on_crash():
    source = HOST_MANAGER_PY.read_text(encoding="utf-8")
    block = source[
        source.index("def allocate_instance"):
        source.index("def get_instance_record")
    ]
    assert block.index("write_json_atomic(ports_path(home), ports_doc)") < block.index(
        "write_json_atomic(instances_path(home), instances_doc)"
    )


def test_ready_marker_and_audit_contract(tmp_path):
    home = tmp_path / "miru-host"
    run_json(
        "init-host",
        "--home",
        str(home),
        "--server-ip",
        "127.0.0.1",
        "--port-start",
        "5601",
        "--port-end",
        "5604",
    )
    run_json("allocate-instance", "--home", str(home), "--instance-id", "inst-alpha")
    ready = run_json(
        "mark-instance",
        "--home",
        str(home),
        "--instance-id",
        "inst-alpha",
        "--status",
        "ready",
        "--invitation-code",
        "MIRU-ABCDEFGHIJ-ABCDEF",
    )["instance"]
    assert ready["status"] == "ready"
    assert ready["invitation_code"] == "MIRU-ABCDEFGHIJ-ABCDEF"
    assert run_json("audit", "--home", str(home)) == {
        "ok": True,
        "home": str(home),
        "issues": [],
    }

    ports_path = home / "ports.json"
    ports_doc = json.loads(ports_path.read_text(encoding="utf-8"))
    ports_doc["ports"]["5601"]["instance_id"] = "missing-instance"
    ports_path.write_text(json.dumps(ports_doc), encoding="utf-8")
    audit = run_json("audit", "--home", str(home))
    assert audit["ok"] is False
    assert any("missing/mismatched ports.json" in issue for issue in audit["issues"])


def test_host_manager_rejects_bad_ids_and_domain_server_ip(tmp_path):
    home = tmp_path / "miru-host"
    bad_ip = subprocess.run(
        [
            sys.executable,
            str(HOST_MANAGER_PY),
            "init-host",
            "--home",
            str(home),
            "--server-ip",
            "example.com",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert bad_ip.returncode == 2
    assert "IPv4" in bad_ip.stderr

    run_json("init-host", "--home", str(home), "--server-ip", "127.0.0.1")
    bad_id = subprocess.run(
        [
            sys.executable,
            str(HOST_MANAGER_PY),
            "allocate-instance",
            "--home",
            str(home),
            "--instance-id",
            "../oops",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert bad_id.returncode == 2
    assert "lowercase" in bad_id.stderr or "only lowercase" in bad_id.stderr


def test_shell_wrappers_delegate_to_self_host_with_instance_scope():
    create_text = (HOST_MANAGER / "create_instance.sh").read_text(encoding="utf-8")
    assert "bash \"$SELF_HOST_DIR/install.sh\"" in create_text
    assert "--env-file FILE" in create_text
    assert "load_ai_env_file \"$ENV_FILE\"" in create_text
    assert "source \"$ENV_FILE\"" not in create_text
    assert "AI_VISION_HOST|AI_VISION_KEY|AI_VISION_MODEL" in create_text
    assert "REQUESTED_PORT" in create_text
    assert "MIRU_CONTAINER_NAME=\"$container_name\"" in create_text
    assert "MIRU_COMPOSE_PROJECT=\"$compose_project\"" in create_text
    assert "--server-port \"$server_port\"" in create_text
    assert "remove_instance_dir \"$instance_home\"" in create_text

    for name in ["status_instance.sh", "reset_instance.sh", "restore_instance.sh", "update_instance.sh"]:
        text = (HOST_MANAGER / name).read_text(encoding="utf-8")
        assert "MIRU_COMPOSE_PROJECT=\"$compose_project\"" in text
        assert "--home \"$instance_home\"" in text
    assert "--json" in (HOST_MANAGER / "status_instance.sh").read_text(encoding="utf-8")

    delete_text = (HOST_MANAGER / "delete_instance.sh").read_text(encoding="utf-8")
    assert "compose_down_instance \"$instance_home\" \"$compose_project\"" in delete_text
    assert "remove-instance --home \"$MIRU_HOST_HOME\" --instance-id \"$INSTANCE_ID\"" in delete_text


def test_host_manager_does_not_use_single_instance_home_as_data_boundary():
    combined = "\n".join(
        (HOST_MANAGER / name).read_text(encoding="utf-8")
        for name in SCRIPT_NAMES
        if (HOST_MANAGER / name).exists()
    )
    assert "/opt/miru-host" in combined
    assert "/opt/miru/data" not in combined
    assert "instances/$instance_id" in combined or "instances\" / instance_id" in HOST_MANAGER_PY.read_text(encoding="utf-8")


def test_e2e_four_instances_script_covers_milestone_e_contract():
    text = (HOST_MANAGER / "e2e_four_instances.sh").read_text(encoding="utf-8")
    for fragment in [
        "--env-file FILE",
        "--clean-existing",
        "--keep",
        "Checking cross-instance auth rejection",
        "reset_instance.sh",
        "backup_instance.sh",
        "restore_instance.sh",
        "update_instance.sh",
        "delete_instance.sh",
        "ScreenObservationVLM",
        "AttentionEngineEvaluate",
        "docker stats --no-stream",
        "Milestone E PASS",
    ]:
        assert fragment in text
    assert "invitation_code_masked" in text
    assert "token" not in text.split("json.dump(payload")[1]


def test_e2e_four_instances_script_avoids_common_false_positive_checks():
    text = (HOST_MANAGER / "e2e_four_instances.sh").read_text(encoding="utf-8")
    assert 'prompt_text="Milestone E ${id} smoke' in text
    assert 'wait_chat_reply "$url" "$token" "$prompt_text"' in text
    assert 'json.loads(sys.argv[2] or "[]")' in text
    assert 'msg.get("type") not in proactive_types' in text
    assert 'code" == "401" || "$code" == "403"' in text
    assert '${#CREATED[@]} == 0' in text
    assert '"${PREFIX}-b" >/dev/null' not in text
