from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "docker_smoke_test.sh"


def test_exported_api_config_is_forwarded_to_smoke_container_without_argv_values():
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'for key in "${API_CONFIG_KEYS[@]}"; do' in source
    assert 'env_args+=(-e "$key")' in source
    assert 'env_args+=(-e "$key=${!key}")' not in source
