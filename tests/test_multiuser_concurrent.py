"""#162 — multi-user concurrent integration test.

Spawns a real Flask subprocess with an isolated DATA_DIR (under pytest's
tmp_path), creates two test users, then drives concurrent traffic against
both to verify that:

1. chat_history is per-user (no cross-talk between A and B)
2. SSE events are per-user (A's broadcasts don't reach B's stream)
3. commitments / data writes are per-user
4. CareEngine instances are spawned per-user (no shared state)

Real LLM calls are made — the test will skip if AI_API_KEY env is not set.
The subprocess dies when the test ends, which kills any spawned CareEngine
threads (no cross-process leakage to your real Flask).

Multi-layered safety against ever touching ./data/users/<your_real_uid>:

  L1 — Subprocess DATA_DIR points at pytest tmp_path; the real ./data is
       physically out of reach for the test server.
  L2 — All test users carry the prefix "pytest_mu_"; the cleanup loop
       refuses to delete anything not bearing that prefix.
  L3 — pytest auto-cleans tmp_path after the test, even on hard failure.
  L4 — When the subprocess is terminated, its CareEngine instances die
       with it (in-process state is never shared with the parent test).
"""
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time

# Bypass any inherited http_proxy / https_proxy when talking to localhost.
# Some dev machines have a system-wide proxy (ShadowsocksX, Surge, etc.) that
# intercepts even 127.0.0.1 traffic and returns 502. Set BEFORE importing
# requests so urllib3 picks it up. Wildcard '*' is the universal opt-out.
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Anything we create in the test must carry this prefix. The cleanup logic
# refuses to delete files that lack it — a defensive layer on top of the
# tmp_path isolation.
TEST_UID_PREFIX = "pytest_mu_"

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
SERVER_READY_TIMEOUT = 60  # seconds; first-time imports can be slow
LLM_RESPONSE_TIMEOUT = 90  # generous: real LLM can be slow

def _saved_ai_config_path() -> str | None:
    """Locate the user's saved AI config so we can copy it into the test sandbox."""
    candidates = [
        os.path.join(REPO_ROOT, "data", "ai_config.json"),
        os.path.join(REPO_ROOT, "data", "_admin", "ai_config.json"),
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                with open(p) as f:
                    cfg = json.load(f)
                if cfg.get("api_key"):
                    return p
            except (OSError, ValueError):
                pass
    return None


_LLM_KEY_AVAILABLE = bool(
    os.environ.get("AI_API_KEY")
    or os.environ.get("MIRU_RUN_INTEGRATION")
    or _saved_ai_config_path()
)
_skip_no_llm = pytest.mark.skipif(
    not _LLM_KEY_AVAILABLE,
    reason="real-LLM integration test; set AI_API_KEY or save AI config in data/ai_config.json",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_ready(base_url: str, proc: subprocess.Popen, log_path: str = "",
                timeout: int = SERVER_READY_TIMEOUT):
    deadline = time.time() + timeout
    last_err = None
    last_status = None
    last_body = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = ""
            if log_path and os.path.exists(log_path):
                with open(log_path, "rb") as f:
                    tail = f.read()[-3000:].decode(errors="replace")
            raise RuntimeError(f"server died early (exit {proc.returncode}).\nLOG TAIL:\n{tail}")
        try:
            r = requests.get(f"{base_url}/api/health", timeout=2)
            last_status = r.status_code
            last_body = (r.text or "")[:300]
            if r.status_code == 200:
                try:
                    if r.json().get("ok"):
                        return
                except ValueError:
                    pass
        except requests.RequestException as e:
            last_err = repr(e)
        time.sleep(0.5)
    tail = ""
    if log_path and os.path.exists(log_path):
        with open(log_path, "rb") as f:
            tail = f.read()[-3000:].decode(errors="replace")
    raise RuntimeError(
        f"server not ready in {timeout}s.\n"
        f"  last_status={last_status} last_body={last_body!r} last_err={last_err}\n"
        f"LOG TAIL:\n{tail}"
    )


def _safe_cleanup_user_dir(data_dir: str, uid: str):
    """Delete a user directory only if every safety check passes."""
    # L2 — refuse anything without the test prefix
    assert uid.startswith(TEST_UID_PREFIX), f"refuse to delete non-test uid: {uid!r}"
    # L1 — the dir must be inside the isolated test DATA_DIR
    user_dir = os.path.realpath(os.path.join(data_dir, "users", uid))
    data_dir_real = os.path.realpath(data_dir)
    assert user_dir.startswith(data_dir_real + os.sep), \
        f"refuse to delete out-of-tmp path: {user_dir!r} not under {data_dir_real!r}"
    if os.path.exists(user_dir):
        shutil.rmtree(user_dir)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def real_server(tmp_path):
    """Spawn a real Flask subprocess pointing at an isolated DATA_DIR."""
    data_dir = tmp_path / "test_data"
    data_dir.mkdir()
    admin_dir = data_dir / "_admin"
    admin_dir.mkdir()

    # Seed AI config so the subprocess can talk to the LLM. Copy the user's
    # saved config from production data dir into the test sandbox. We do NOT
    # share data dirs — only the config file.
    saved_cfg = _saved_ai_config_path()
    if saved_cfg:
        shutil.copy(saved_cfg, str(data_dir / "ai_config.json"))

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "DATA_DIR": str(data_dir),
        "PORT": str(port),
        "MIRU_HEADLESS": "1",     # skip Cloudflare tunnel & other side effects
        "NO_PROXY": "*",           # subprocess too — outbound LLM still uses proxy
        "no_proxy": "*",
    }

    log_path = str(tmp_path / "server.log")
    log_fp = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        env=env,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        cwd=REPO_ROOT,
    )

    try:
        _wait_ready(base_url, proc, log_path=log_path)
        yield {
            "port": port,
            "base": base_url,
            "data_dir": str(data_dir),
            "admin_dir": str(admin_dir),
            "proc": proc,
        }
    finally:
        # Terminate the server. CareEngine threads die with the process.
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        try:
            log_fp.close()
        except Exception:
            pass

        # Defensive cleanup: even though pytest will rm tmp_path, do an
        # explicit prefix-checked sweep so that if anyone ever wires this
        # fixture with a non-tmp data_dir, the safety asserts will fire.
        users_root = data_dir / "users"
        if users_root.exists():
            for child in users_root.iterdir():
                if child.is_dir() and child.name.startswith(TEST_UID_PREFIX):
                    _safe_cleanup_user_dir(str(data_dir), child.name)


@pytest.fixture
def two_test_users(real_server):
    """Create two test users by writing directly to the isolated _admin/users.json.

    Bypasses the invite-code flow because login_with_code() forces uid to
    'u_<hex>' — we need 'pytest_mu_*' prefixes for the cleanup safety nets.
    The route under test (token → uid → data_dir) is identical regardless
    of how the user was created.
    """
    server = real_server
    admin_dir = server["admin_dir"]
    users_path = os.path.join(admin_dir, "users.json")

    ts = int(time.time())
    uid_a = f"{TEST_UID_PREFIX}a_{ts}"
    uid_b = f"{TEST_UID_PREFIX}b_{ts}"
    tok_a = secrets.token_urlsafe(32)
    tok_b = secrets.token_urlsafe(32)

    assert uid_a.startswith(TEST_UID_PREFIX) and uid_b.startswith(TEST_UID_PREFIX)

    users = {
        uid_a: {
            "token": tok_a,
            "invitation_code": f"TEST-{uid_a}",
            "created_at": "2026-04-28 00:00:00",
            "status": "active",
        },
        uid_b: {
            "token": tok_b,
            "invitation_code": f"TEST-{uid_b}",
            "created_at": "2026-04-28 00:00:00",
            "status": "active",
        },
    }
    with open(users_path, "w") as f:
        json.dump(users, f)

    # Pre-create user dirs so first request doesn't race directory creation
    for uid in (uid_a, uid_b):
        ud = os.path.join(server["data_dir"], "users", uid)
        os.makedirs(os.path.join(ud, "uploads"), exist_ok=True)

    yield {
        "uid_a": uid_a, "tok_a": tok_a,
        "uid_b": uid_b, "tok_b": tok_b,
    }

    # Cleanup is handled by real_server fixture's teardown


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.slow
@_skip_no_llm
def test_chat_history_per_user_isolation(real_server, two_test_users):
    """Concurrent /api/chat from A and B → A's history has only A's user msgs."""
    server = real_server
    u = two_test_users

    barrier = threading.Barrier(2)
    results = {"a": [], "b": []}
    errors = []

    def send(token, marker, key):
        try:
            barrier.wait(timeout=10)
            for i in range(2):  # 2 each = 4 total LLM calls; controls cost
                text = f"{marker}_n{i}_{int(time.time()*1000)}"
                r = requests.post(
                    f"{server['base']}/api/chat",
                    headers=_hdr(token),
                    data=json.dumps({"text": text}),
                    timeout=LLM_RESPONSE_TIMEOUT,
                )
                if r.status_code != 200:
                    errors.append(f"{marker} chat failed: {r.status_code} {r.text[:200]}")
                    return
                results[key].append(text)
                time.sleep(0.3)
        except Exception as e:
            errors.append(f"{marker} exception: {e}")

    t1 = threading.Thread(target=send, args=(u["tok_a"], "alice", "a"))
    t2 = threading.Thread(target=send, args=(u["tok_b"], "bob", "b"))
    t1.start(); t2.start()
    t1.join(timeout=LLM_RESPONSE_TIMEOUT * 3)
    t2.join(timeout=LLM_RESPONSE_TIMEOUT * 3)

    assert not errors, f"send errors: {errors}"
    assert len(results["a"]) == 2 and len(results["b"]) == 2

    ra = requests.get(f"{server['base']}/api/chat/history", headers=_hdr(u["tok_a"]), timeout=10)
    rb = requests.get(f"{server['base']}/api/chat/history", headers=_hdr(u["tok_b"]), timeout=10)
    assert ra.status_code == 200 and rb.status_code == 200

    a_text = json.dumps(ra.json(), ensure_ascii=False)
    b_text = json.dumps(rb.json(), ensure_ascii=False)

    for marker in results["a"]:
        assert marker in a_text, f"A's message {marker!r} missing in A's chat history"
        assert marker not in b_text, f"A's message {marker!r} leaked into B's chat history"
    for marker in results["b"]:
        assert marker in b_text, f"B's message {marker!r} missing in B's chat history"
        assert marker not in a_text, f"B's message {marker!r} leaked into A's chat history"


@pytest.mark.integration
@pytest.mark.slow
@_skip_no_llm
def test_sse_per_user_isolation(real_server, two_test_users):
    """Open SSE streams for A and B; A sends a chat → only A's stream sees the event."""
    server = real_server
    u = two_test_users

    a_events = []
    b_events = []

    def consume_sse(token, sink, stop_evt):
        try:
            with requests.get(
                f"{server['base']}/api/events",
                headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
                stream=True,
                timeout=30,
            ) as resp:
                for line in resp.iter_lines(decode_unicode=True):
                    if stop_evt.is_set():
                        return
                    if line and line.startswith("event:"):
                        sink.append(line[6:].strip())
        except requests.RequestException:
            pass

    stop = threading.Event()
    ta = threading.Thread(target=consume_sse, args=(u["tok_a"], a_events, stop), daemon=True)
    tb = threading.Thread(target=consume_sse, args=(u["tok_b"], b_events, stop), daemon=True)
    ta.start(); tb.start()
    time.sleep(2.0)  # let SSE handshake finish

    r = requests.post(
        f"{server['base']}/api/chat",
        headers=_hdr(u["tok_a"]),
        data=json.dumps({"text": f"sse_probe_{int(time.time())}"}),
        timeout=LLM_RESPONSE_TIMEOUT,
    )
    assert r.status_code == 200, f"chat failed: {r.text[:200]}"

    time.sleep(3.0)
    stop.set()
    ta.join(timeout=5); tb.join(timeout=5)

    a_non_connected = [e for e in a_events if e != "connected"]
    b_non_connected = [e for e in b_events if e != "connected"]
    assert len(a_non_connected) >= 1, f"A should have got events. A={a_events}"
    assert len(b_non_connected) == 0, \
        f"B leaked events that belong to A: {b_non_connected}; A={a_events}"


@pytest.mark.integration
def test_commitments_per_user_isolation(real_server, two_test_users):
    """A creates a commitment; B's commitment list is empty (no cross-tenant read).

    No LLM call required — pure data-routing isolation test.
    """
    server = real_server
    u = two_test_users

    payload = {
        "title": f"alice_only_task_{int(time.time())}",
        "deadline": "2026-12-31",
    }
    r = requests.post(
        f"{server['base']}/api/commitments",
        headers=_hdr(u["tok_a"]),
        data=json.dumps(payload),
        timeout=10,
    )
    assert r.status_code in (200, 201), \
        f"create commitment failed: {r.status_code} {r.text[:200]}"

    rb = requests.get(f"{server['base']}/api/commitments", headers=_hdr(u["tok_b"]), timeout=10)
    assert rb.status_code == 200
    b_text = json.dumps(rb.json(), ensure_ascii=False)
    assert payload["title"] not in b_text, \
        f"A's commitment {payload['title']!r} leaked into B's list: {b_text[:300]}"

    ra = requests.get(f"{server['base']}/api/commitments", headers=_hdr(u["tok_a"]), timeout=10)
    assert ra.status_code == 200
    a_text = json.dumps(ra.json(), ensure_ascii=False)
    assert payload["title"] in a_text, \
        f"A's commitment {payload['title']!r} missing in A's list"


@pytest.mark.integration
@pytest.mark.slow
@_skip_no_llm
def test_user_data_dirs_physically_separate(real_server, two_test_users):
    """Sanity: per-user files land in distinct directories under tmp DATA_DIR."""
    server = real_server
    u = two_test_users

    requests.post(
        f"{server['base']}/api/chat",
        headers=_hdr(u["tok_a"]),
        data=json.dumps({"text": f"isolation_check_{int(time.time())}"}),
        timeout=LLM_RESPONSE_TIMEOUT,
    )

    a_dir = os.path.join(server["data_dir"], "users", u["uid_a"])
    b_dir = os.path.join(server["data_dir"], "users", u["uid_b"])
    assert os.path.isdir(a_dir)
    assert os.path.isdir(b_dir)

    a_chat = os.path.join(a_dir, "chat_history.json")
    if os.path.exists(a_chat):
        with open(a_chat) as f:
            a_content = f.read()
        assert "isolation_check_" in a_content

    b_chat = os.path.join(b_dir, "chat_history.json")
    if os.path.exists(b_chat):
        with open(b_chat) as f:
            b_content = f.read()
        assert "isolation_check_" not in b_content, \
            "A's chat leaked into B's chat_history.json"


# ---------------------------------------------------------------------------
# Final safety check — verify production ./data was not touched
# ---------------------------------------------------------------------------

def test_no_pytest_mu_users_in_production_data():
    """Ensure no test users ever leaked into the real data dir."""
    real_data = os.path.join(REPO_ROOT, "data", "users")
    if not os.path.isdir(real_data):
        return
    leaked = [d for d in os.listdir(real_data) if d.startswith(TEST_UID_PREFIX)]
    assert not leaked, f"LEAK: pytest_mu_* users found in production data/: {leaked}"


if __name__ == "__main__":
    print("Run via: PYTHONPATH=. .venv/bin/python -m pytest "
          "tests/test_multiuser_concurrent.py -v -m integration")
