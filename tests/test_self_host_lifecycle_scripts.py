import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SELF_HOST = ROOT / "deploy" / "self_host"


SCRIPT_NAMES = [
    "common.sh",
    "install.sh",
    "status.sh",
    "backup.sh",
    "restore.sh",
    "reset.sh",
    "update.sh",
]


@pytest.mark.skipif(os.name == "nt", reason="Linux shell syntax is validated on macOS/Linux")
def test_self_host_scripts_exist_and_are_syntax_valid():
    for name in SCRIPT_NAMES:
        path = SELF_HOST / name
        assert path.exists(), name
        subprocess.run(["bash", "-n", str(path)], check=True)


@pytest.mark.skipif(os.name == "nt", reason="Linux lifecycle entrypoints require bash")
def test_lifecycle_entrypoints_show_help_without_root_or_docker():
    for name in ["install.sh", "status.sh", "backup.sh", "restore.sh", "reset.sh", "update.sh"]:
        path = SELF_HOST / name
        result = subprocess.run(
            ["bash", str(path), "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        assert "Usage:" in result.stdout


def test_compose_template_matches_server_image_contract():
    text = (SELF_HOST / "compose.yaml.template").read_text(encoding="utf-8")
    assert 'version: "3.7"' in text
    assert "image: ${MIRU_IMAGE}" in text
    assert "./data:/opt/miru/data" in text
    assert "./logs:/opt/miru/logs" in text
    assert "container_name: ${MIRU_CONTAINER_NAME}" in text
    assert '"${SERVER_PORT}:5001"' in text
    assert ":-" not in text
    assert "TZ: ${TZ}" in text
    assert "TIMEZONE: ${TIMEZONE}" in text
    assert "MIRU_HEADLESS" in text
    common = (SELF_HOST / "common.sh").read_text(encoding="utf-8")
    assert "-f compose.yaml" in common
    assert "http://127.0.0.1:${host_port}/api/health" in common
    assert "host_port=\"$(server_port_from_env)\"" in common


def test_env_example_contains_required_provisioning_keys():
    text = (SELF_HOST / "env.example").read_text(encoding="utf-8")
    for key in [
        "MIRU_IMAGE",
        "SERVER_IP",
        "SERVER_PORT",
        "TZ",
        "TIMEZONE",
        "AI_VISION_HOST",
        "AI_VISION_KEY",
        "AI_VISION_MODEL",
        "AI_CHAT_HOST",
        "AI_CHAT_KEY",
        "AI_CHAT_MODEL",
        "AI_MEMORY_HOST",
        "AI_MEMORY_KEY",
        "AI_MEMORY_MODEL",
    ]:
        assert f"{key}=" in text
    assert "AI_VISION_HOST=openrouter.ai/api" in text
    assert "AI_VISION_MODEL=qwen/qwen3.5-9b" in text
    assert "AI_CHAT_HOST=api.deepseek.com" in text
    assert "AI_CHAT_MODEL=deepseek-v4-pro" in text
    assert "AI_MEMORY_HOST=api.deepseek.com" in text
    assert "AI_MEMORY_MODEL=deepseek-v4-flash" in text
    assert "MIRU_IMAGE=miru/server:0.2.0" in text
    assert "miru/server:0.1.0" not in text
    assert "TZ=Asia/Shanghai" in text
    assert "TIMEZONE=Asia/Shanghai" in text


def test_docker_runtime_defaults_timezone_for_private_servers():
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (ROOT / "scripts" / "docker_entrypoint.sh").read_text(encoding="utf-8")

    assert "TZ=Asia/Shanghai" in dockerfile
    assert "TIMEZONE=Asia/Shanghai" in dockerfile
    assert ': "${TZ:=Asia/Shanghai}"' in entrypoint
    assert ': "${TIMEZONE:=$TZ}"' in entrypoint
    assert "export DATA_DIR LOG_DIR PORT SERVER_PORT MIRU_HEADLESS FLASK_DEBUG TZ TIMEZONE" in entrypoint


def test_reset_default_preserves_ai_config_and_auth_token():
    text = (SELF_HOST / "reset.sh").read_text(encoding="utf-8")
    assert 'MODE="user-data"' in text
    assert "ai_config.json" in text
    assert "auth.json" in text
    assert "--factory" in text


def test_backup_default_includes_env_and_marks_sensitive():
    text = (SELF_HOST / "backup.sh").read_text(encoding="utf-8")
    assert "INCLUDE_ENV=1" in text
    assert "chmod 600" in text
    assert "Backup includes .env and API keys" in text


def test_install_supports_provisioning_noninteractive_contract():
    text = (SELF_HOST / "install.sh").read_text(encoding="utf-8")
    assert "--non-interactive" in text
    assert "SERVER_IP" in text
    assert "MIRU_IMAGE" in text
    assert "AI_VISION_KEY" in text
    assert "MIRU_INVITATION_CODE" in text
    assert "--skip-pull" in text
    assert "--image-tar" in text
    assert "docker load -i" in text
    assert "docker image inspect \"$MIRU_IMAGE\"" in text
    assert "docker tag \"$loaded_ref\" \"$MIRU_IMAGE\"" in text
    assert "runtime_tz=\"${TZ:-Asia/Shanghai}\"" in text
    assert "TIMEZONE=${runtime_timezone}" in text
    assert "Loaded image:" in text
    assert "printf '%s %s\\n'" in text
    assert "docker.io docker-compose" in text
    assert "rm -f /etc/apt/sources.list.d/docker.list" in text
    assert "$MIRU_HOME/data/_admin/ai_config.json" in text
    assert "qwen/qwen3.5-9b" in text
    assert "deepseek-v4-pro" in text
    assert "deepseek-v4-flash" in text
    assert "os.environ.get(\"AI_VISION_KEY\"" in text
    assert "metadata.tencentyun.com" not in text
    assert "Milestone C installer" not in text
    assert "Ubuntu/Debian" in text


def test_update_supports_preloaded_image_validation_path():
    text = (SELF_HOST / "update.sh").read_text(encoding="utf-8")
    assert "--skip-pull" in text
    assert "--image-tar" in text
    assert "MIRU_SKIP_PULL" in text
    assert "MIRU_IMAGE_TAR" in text
    assert "Skipping docker pull for preloaded image" in text


def test_status_reports_local_and_public_health_separately():
    text = (SELF_HOST / "status.sh").read_text(encoding="utf-8")
    assert "local_health" in text
    assert "public_health" in text
    assert "http://127.0.0.1:${host_port}/api/health" in text
    assert "host_port=\"$(server_port_from_env)\"" in text
    assert '127.0.0.1:${CONTAINER_PORT}/api/health' not in text
    assert "docker-compose -f compose.yaml" in text


def test_health_checks_never_fall_back_to_an_unrelated_host_port():
    common = (SELF_HOST / "common.sh").read_text(encoding="utf-8")

    assert 'local_url="http://127.0.0.1:${host_port}/api/health"' in common
    assert '127.0.0.1:${CONTAINER_PORT}/api/health' not in common


def test_lifecycle_scripts_keep_compose_v1_and_v2_compatibility():
    common = (SELF_HOST / "common.sh").read_text(encoding="utf-8")
    assert "docker compose -f compose.yaml" in common
    assert "docker-compose -f compose.yaml" in common

    forbidden = []
    for path in SELF_HOST.glob("*.sh"):
        text = path.read_text(encoding="utf-8")
        if path.name == "common.sh":
            continue
        if "docker compose -f compose.yaml" in text and "docker-compose -f compose.yaml" not in text:
            forbidden.append(path.name)
    assert not forbidden


def test_self_host_defaults_are_current_release_and_cloud_neutral():
    common = (SELF_HOST / "common.sh").read_text(encoding="utf-8")
    install = (SELF_HOST / "install.sh").read_text(encoding="utf-8")
    readme = (SELF_HOST / "README.md").read_text(encoding="utf-8")

    assert 'MIRU_IMAGE_VERSION="${MIRU_IMAGE_VERSION:-0.2.0}"' in common
    assert "miru-server-v0.2.0-linux-amd64.tar.gz" in readme
    assert "metadata.tencentyun.com" not in common
    assert "metadata.tencentyun.com" not in install
