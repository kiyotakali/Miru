import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_memory_filesystem_basic(monkeypatch, tmp_path):
    """Test new filesystem memory works under custom DATA_DIR."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    import memory as mem_module
    importlib.reload(mem_module)

    mem_module.ensure_dirs()
    assert mem_module.read_index() is not None
    mem_module.write_file("people/test.md", "# Test")
    assert mem_module.read_file("people/test.md") == "# Test"


def test_memory_search(monkeypatch, tmp_path):
    """Test memory search functionality."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import memory as mem_module
    importlib.reload(mem_module)

    mem_module.ensure_dirs()
    mem_module.write_file("people/alice.md", "# Alice\nAlice is a software engineer.")
    mem_module.write_file("journal/2026-03-22.md", "Met Alice for coffee today.")

    results = mem_module.search("Alice")
    assert len(results) >= 2
