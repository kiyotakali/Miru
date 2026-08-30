import io
import os
import threading
import time
import types

import pytest

import auth
import client_provisioning as cp


class FakeSuccessRunner:
    last_request = None

    def provision(self, request, job_id, on_log):
        type(self).last_request = request
        on_log(f"connected with password={request.ssh_password}")
        server = auth.encode_server(request.server_ip, request.port_start)
        return {
            "invitation_code": f"MIRU-{server}-ABCDEF",
            "server_url": f"http://{request.server_ip}:{request.port_start}",
            "instance_id": "inst-test",
        }


class FakeFailureRunner:
    def provision(self, request, job_id, on_log):
        on_log("started")
        raise RuntimeError(f"ssh failed with secret {request.ssh_password}")


class FakeSlowRunner:
    started = threading.Event()
    release = threading.Event()
    calls = 0

    @classmethod
    def reset(cls):
        cls.started = threading.Event()
        cls.release = threading.Event()
        cls.calls = 0

    def provision(self, request, job_id, on_log):
        type(self).calls += 1
        on_log("running")
        type(self).started.set()
        type(self).release.wait(timeout=3)
        server = auth.encode_server(request.server_ip, request.port_start)
        return {
            "invitation_code": f"MIRU-{server}-ABCDEF",
            "server_url": f"http://{request.server_ip}:{request.port_start}",
            "instance_id": "inst-test",
        }


class _FakeStdin:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        pass

    def close(self):
        pass


class _FakeChannel:
    def __init__(self, status):
        self._status = status

    def recv_exit_status(self):
        return self._status


class _FakeStream:
    def __init__(self, text="", status=0):
        self._data = text.encode("utf-8")
        self.channel = _FakeChannel(status)

    def read(self):
        return self._data


class FakeSSHClient:
    def __init__(self, *, image_exists=False, host_exists=False, host_config=None,
                 skipped_system_ports=None, selected_port=5201):
        self.image_exists = image_exists
        self.host_exists = host_exists
        self.host_config = host_config or {
            "server_ip": "8.8.8.8",
            "port_range": {"start": 5201, "end": 5208},
            "default_image": "miru/server:0.2.0",
        }
        self.skipped_system_ports = list(skipped_system_ports or [])
        self.selected_port = selected_port
        self.commands = []
        self.connect_kwargs = []
        self.stdin_writes = []
        self.uploads = []
        self.closed = False

    def set_missing_host_key_policy(self, _policy):
        pass

    def connect(self, **kwargs):
        self.connect_kwargs.append(kwargs)

    def open_sftp(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def close(self):
        self.closed = True

    def put(self, local_path, remote_path, callback=None):
        self.uploads.append((local_path, remote_path))
        if callback:
            size = os.path.getsize(local_path)
            callback(size, size)

    def chmod(self, _path, _mode):
        pass

    def exec_command(self, command, timeout=600):
        self.commands.append(command)
        stdin = _FakeStdin()
        self.stdin_writes.append(stdin.writes)
        if "test -f" in command and "host.json" in command:
            return stdin, _FakeStream(status=0 if self.host_exists else 1), _FakeStream()
        if "cat " in command and "host.json" in command:
            return stdin, _FakeStream(__import__("json").dumps(self.host_config)), _FakeStream()
        if command.startswith("docker image inspect"):
            return stdin, _FakeStream(status=0 if self.image_exists else 1), _FakeStream()
        if "docker image inspect" in command:
            return stdin, _FakeStream(status=0 if self.image_exists else 1), _FakeStream()
        if command.startswith("docker pull"):
            return stdin, _FakeStream("pulled"), _FakeStream()
        if "docker pull" in command:
            return stdin, _FakeStream("pulled"), _FakeStream()
        if "bash deploy/host_manager/create_instance.sh" in command:
            server = auth.encode_server("8.8.8.8", self.selected_port)
            selection = __import__("json").dumps({
                "skipped_system_ports": self.skipped_system_ports,
            })
            output = (
                '{"ok": true, "instance": {'
                f'"invitation_code": "MIRU-{server}-ABCDEF", '
                f'"server_url": "http://8.8.8.8:{self.selected_port}", '
                f'"server_port": {self.selected_port}, '
                f'"port_selection": {selection}, '
                '"instance_id": "inst-test"'
                "}}"
            )
            return stdin, _FakeStream(output), _FakeStream()
        return stdin, _FakeStream("ok"), _FakeStream()


class FakeTransientCreateSSHClient(FakeSSHClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.create_attempts = 0

    def exec_command(self, command, timeout=600):
        if "bash deploy/host_manager/create_instance.sh" in command:
            self.commands.append(command)
            stdin = _FakeStdin()
            self.stdin_writes.append(stdin.writes)
            self.create_attempts += 1
            if self.create_attempts == 1:
                return stdin, _FakeStream(status=1), _FakeStream("transient compose failure")
        return super().exec_command(command, timeout=timeout)


class FakeParamikoRunner(cp.ParamikoProvisionRunner):
    def _upload_bundle(self, sftp, remote_repo):
        pass


@pytest.fixture(autouse=True)
def reset_jobs():
    cp.reset_jobs_for_tests()
    cp.set_runner_factory_for_tests(cp.ParamikoProvisionRunner)
    yield
    cp.reset_jobs_for_tests()
    cp.set_runner_factory_for_tests(cp.ParamikoProvisionRunner)


def _payload(**overrides):
    data = {
        "server_ip": "8.8.8.8",
        "ssh_user": "root",
        "ssh_port": 22,
        "ssh_password": "super-secret",
        "host_home": "/opt/miru-host",
        "port_start": 5201,
        "port_end": 5208,
        "image": "miru/server:0.2.0",
    }
    data.update(overrides)
    return data


def test_validate_self_server_payload_rejects_bad_input():
    with pytest.raises(cp.ProvisioningError, match="IPv4"):
        cp.validate_self_server_payload(_payload(server_ip="miru.example.com"))
    with pytest.raises(cp.ProvisioningError, match="公网"):
        cp.validate_self_server_payload(_payload(server_ip="127.0.0.1"))
    with pytest.raises(cp.ProvisioningError, match="公网"):
        cp.validate_self_server_payload(_payload(server_ip="10.0.0.8"))
    with pytest.raises(cp.ProvisioningError, match="密码"):
        cp.validate_self_server_payload(_payload(ssh_password=""))
    with pytest.raises(cp.ProvisioningError, match="起始端口"):
        cp.validate_self_server_payload(_payload(port_start=6000, port_end=5201))
    with pytest.raises(cp.ProvisioningError, match="镜像"):
        cp.validate_self_server_payload(_payload(image="bad image name"))


def test_validate_self_server_payload_parses_form_booleans():
    assert cp.validate_self_server_payload(_payload(allow_no_api="false")).allow_no_api is False
    assert cp.validate_self_server_payload(_payload(allow_no_api="0")).allow_no_api is False
    assert cp.validate_self_server_payload(_payload(allow_no_api="true")).allow_no_api is True
    assert cp.validate_self_server_payload(_payload(allow_no_api="")).allow_no_api is True


def test_validate_self_server_payload_requires_explicit_ssh_user():
    with pytest.raises(cp.ProvisioningError, match="SSH 用户名"):
        cp.validate_self_server_payload({**_payload(), "ssh_user": ""})

    req = cp.validate_self_server_payload(_payload(ssh_user="ubuntu"))
    assert req.ssh_user == "ubuntu"


def test_validate_self_server_payload_accepts_key_auth_sentinel():
    req = cp.validate_self_server_payload(_payload(ssh_password="//"))

    assert req.ssh_password == "//"
    assert req.public_dict()["ssh_auth"] == "key"


def test_validate_self_server_payload_accepts_uploaded_private_key_without_password(tmp_path):
    private_key = tmp_path / "id_ed25519"
    private_key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n", encoding="utf-8")

    req = cp.validate_self_server_payload(
        _payload(
            ssh_password="",
            ssh_private_key_path=str(private_key),
            ssh_private_key_name="id_ed25519",
            ssh_private_key_size=private_key.stat().st_size,
        )
    )

    assert req.ssh_private_key_path == str(private_key)
    assert req.ssh_auth_mode == "uploaded_key"
    assert req.public_dict()["ssh_auth"] == "key"


def test_paramiko_runner_uses_uploaded_private_key_without_agent(monkeypatch, tmp_path):
    private_key = tmp_path / "id_ed25519"
    private_key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n", encoding="utf-8")
    fake = FakeSSHClient(image_exists=True)
    fake_paramiko = types.SimpleNamespace(
        SSHClient=lambda: fake,
        AutoAddPolicy=lambda: object(),
    )
    monkeypatch.setitem(__import__("sys").modules, "paramiko", fake_paramiko)
    request = cp.validate_self_server_payload(
        _payload(
            ssh_password="",
            ssh_private_key_path=str(private_key),
            ssh_private_key_name="id_ed25519",
            ssh_private_key_size=private_key.stat().st_size,
        )
    )

    result = FakeParamikoRunner().provision(
        request,
        "job-uploaded-key",
        lambda *_args, **_kwargs: None,
    )

    assert result["invitation_code"].startswith("MIRU-")
    connect = fake.connect_kwargs[0]
    assert connect["password"] is None
    assert connect["passphrase"] is None
    assert connect["key_filename"] == str(private_key)
    assert connect["look_for_keys"] is False
    assert connect["allow_agent"] is False


def test_self_server_job_success_sanitizes_logs_and_returns_invitation():
    job = cp.create_self_server_job(
        _payload(),
        runner_factory=lambda: FakeSuccessRunner(),
        run_inline=True,
    )
    public = job.public_dict()

    assert public["status"] == "succeeded"
    assert public["phase_key"] == "done"
    assert public["progress"] == 100
    assert public["invitation_code"].startswith("MIRU-")
    assert public["server_url"] == "http://8.8.8.8:5201"
    assert public["instance_id"] == "inst-test"
    assert "super-secret" not in str(public)
    assert "******" in "\n".join(public["logs"])
    assert "ssh_password" not in public["request"]
    assert job.request.ssh_password == ""


def test_paramiko_runner_uses_sudo_for_non_root_user(tmp_path):
    archive = tmp_path / "miru-server.tar.gz"
    archive.write_bytes(b"fake")
    fake = FakeSSHClient(image_exists=True)

    class Runner(FakeParamikoRunner):
        def _upload_image_tar(self, sftp, remote_root, request, on_log):
            return f"{remote_root}/miru-server.tar.gz"

    import sys
    import types
    fake_paramiko = types.SimpleNamespace(
        SSHClient=lambda: fake,
        AutoAddPolicy=lambda: object(),
    )
    old = sys.modules.get("paramiko")
    sys.modules["paramiko"] = fake_paramiko
    try:
        result = Runner().provision(
            cp.SelfServerProvisionRequest(
                server_ip="8.8.8.8",
                ssh_user="ubuntu",
                ssh_password="super-secret",
                port_start=5201,
                port_end=5208,
                image_tar_path=str(archive),
                image_tar_name=archive.name,
                image_tar_size=archive.stat().st_size,
            ),
            "job-sudo",
            lambda *_args, **_kwargs: None,
        )
    finally:
        if old is None:
            sys.modules.pop("paramiko", None)
        else:
            sys.modules["paramiko"] = old

    assert result["invitation_code"].startswith("MIRU-")
    sudo_commands = [cmd for cmd in fake.commands if cmd.startswith("sudo -S -p '' bash -lc")]
    assert any("host_install.sh" in cmd for cmd in sudo_commands)
    assert any("create_instance.sh" in cmd for cmd in sudo_commands)
    assert "super-secret" not in "\n".join(fake.commands)


def test_paramiko_runner_uses_agent_key_and_passwordless_sudo_for_key_auth(monkeypatch):
    fake = FakeSSHClient(image_exists=True)
    fake_paramiko = types.SimpleNamespace(
        SSHClient=lambda: fake,
        AutoAddPolicy=lambda: object(),
    )
    monkeypatch.setitem(__import__("sys").modules, "paramiko", fake_paramiko)
    request = cp.validate_self_server_payload(
        _payload(
            ssh_user="ubuntu",
            ssh_password="//",
            instance_id="inst-key",
        )
    )

    result = FakeParamikoRunner().provision(
        request,
        "job-key",
        lambda *_args, **_kwargs: None,
    )

    assert result["invitation_code"].startswith("MIRU-")
    assert fake.connect_kwargs
    connect = fake.connect_kwargs[0]
    assert connect["password"] is None
    assert connect["look_for_keys"] is True
    assert connect["allow_agent"] is True
    sudo_commands = [cmd for cmd in fake.commands if cmd.startswith("sudo -n bash -lc")]
    assert any("host_install.sh" in cmd for cmd in sudo_commands)
    assert any("create_instance.sh" in cmd for cmd in sudo_commands)
    assert all(not writes for writes in fake.stdin_writes)


def test_paramiko_runner_reuses_existing_host_manager_without_overwriting(monkeypatch):
    fake = FakeSSHClient(image_exists=True, host_exists=True)
    fake_paramiko = types.SimpleNamespace(
        SSHClient=lambda: fake,
        AutoAddPolicy=lambda: object(),
    )
    monkeypatch.setitem(__import__("sys").modules, "paramiko", fake_paramiko)
    request = cp.validate_self_server_payload(_payload(instance_id="inst-existing-host"))
    logs = []

    result = FakeParamikoRunner().provision(
        request,
        "job-existing-host",
        lambda message, **meta: logs.append((message, meta)),
    )

    assert result["invitation_code"].startswith("MIRU-")
    assert not any("host_install.sh" in cmd for cmd in fake.commands)
    assert any("create_instance.sh" in cmd for cmd in fake.commands)
    assert any(meta.get("phase_key") == "host_reuse" for _message, meta in logs)


def test_paramiko_runner_rejects_existing_host_manager_with_different_port_range(monkeypatch):
    fake = FakeSSHClient(
        image_exists=True,
        host_exists=True,
        host_config={
            "server_ip": "8.8.8.8",
            "port_range": {"start": 5001, "end": 5010},
            "default_image": "miru/server:0.2.0",
        },
    )
    fake_paramiko = types.SimpleNamespace(
        SSHClient=lambda: fake,
        AutoAddPolicy=lambda: object(),
    )
    monkeypatch.setitem(__import__("sys").modules, "paramiko", fake_paramiko)
    request = cp.validate_self_server_payload(
        _payload(instance_id="inst-range-mismatch", port_start=5002, port_end=5002)
    )

    with pytest.raises(cp.ProvisioningError, match="端口范围为 5001-5010"):
        FakeParamikoRunner().provision(
            request,
            "job-range-mismatch",
            lambda *_args, **_kwargs: None,
        )

    assert not any("create_instance.sh" in cmd for cmd in fake.commands)


def test_paramiko_runner_reports_system_port_fallback(monkeypatch):
    fake = FakeSSHClient(
        image_exists=True,
        skipped_system_ports=[5001],
        selected_port=5002,
    )
    fake_paramiko = types.SimpleNamespace(
        SSHClient=lambda: fake,
        AutoAddPolicy=lambda: object(),
    )
    monkeypatch.setitem(__import__("sys").modules, "paramiko", fake_paramiko)
    request = cp.validate_self_server_payload(
        _payload(instance_id="inst-port-fallback", port_start=5001, port_end=5002)
    )
    logs = []

    result = FakeParamikoRunner().provision(
        request,
        "job-port-fallback",
        lambda message, **meta: logs.append((message, meta)),
    )

    assert result["server_url"] == "http://8.8.8.8:5002"
    assert any(
        message == "检测到端口 5001 已被占用，已改用 5002"
        and meta.get("phase_key") == "create_instance"
        for message, meta in logs
    )


def test_paramiko_runner_retries_one_failed_instance_start(monkeypatch):
    fake = FakeTransientCreateSSHClient(image_exists=True)
    fake_paramiko = types.SimpleNamespace(
        SSHClient=lambda: fake,
        AutoAddPolicy=lambda: object(),
    )
    monkeypatch.setitem(__import__("sys").modules, "paramiko", fake_paramiko)
    request = cp.validate_self_server_payload(_payload(instance_id="inst-retry"))
    logs = []

    result = FakeParamikoRunner().provision(
        request,
        "job-retry",
        lambda message, **meta: logs.append((message, meta)),
    )

    assert result["instance_id"] == "inst-test"
    assert fake.create_attempts == 2
    assert any(message == "Miru 首次启动未完成，正在自动重试一次" for message, _ in logs)


def test_self_server_job_failure_sanitizes_error():
    job = cp.create_self_server_job(
        _payload(),
        runner_factory=lambda: FakeFailureRunner(),
        run_inline=True,
    )
    public = job.public_dict()

    assert public["status"] == "failed"
    assert public["phase_key"] == "failed"
    assert "super-secret" not in public["error"]
    assert "super-secret" not in str(public)
    assert "******" in public["error"]
    assert job.request.ssh_password == ""


def test_duplicate_active_self_server_job_reuses_existing_job():
    FakeSlowRunner.reset()
    cp.set_runner_factory_for_tests(lambda: FakeSlowRunner())

    first = cp.create_self_server_job(_payload())
    assert FakeSlowRunner.started.wait(timeout=1)

    second = cp.create_self_server_job(_payload(ssh_password="different-secret"))
    try:
        assert second is first
        assert FakeSlowRunner.calls == 1
        public = second.public_dict()
        assert public["job_id"] == first.job_id
        assert public["status"] == "running"
        assert "different-secret" not in str(public)
        assert "进行中的任务" in "\n".join(public["logs"])
    finally:
        FakeSlowRunner.release.set()


def test_client_provisioning_routes_are_client_mode_only(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_is_client_mode", False)
    client = app_mod.app.test_client()

    resp = client.get("/api/client/provision/self-server/defaults")
    assert resp.status_code == 404


def test_client_provisioning_routes_reject_nonlocal_client_mode(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    client = app_mod.app.test_client()

    resp = client.get(
        "/api/client/provision/self-server/defaults",
        environ_base={"REMOTE_ADDR": "8.8.8.8"},
    )
    assert resp.status_code == 403

    resp = client.post(
        "/api/client/provision/self-server/create",
        json=_payload(),
        environ_base={"REMOTE_ADDR": "8.8.8.8"},
    )
    assert resp.status_code == 403
    assert "super-secret" not in resp.get_data(as_text=True)


def test_client_provisioning_defaults_do_not_assume_cloud_user(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    client = app_mod.app.test_client()

    resp = client.get("/api/client/provision/self-server/defaults")

    assert resp.status_code == 200
    defaults = resp.get_json()["defaults"]
    assert defaults["ssh_user"] == ""
    assert defaults["image"] == "miru/server:0.2.0"


def test_client_provisioning_route_starts_job_without_leaking_password(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    cp.set_runner_factory_for_tests(lambda: FakeSuccessRunner())
    client = app_mod.app.test_client()

    resp = client.post("/api/client/provision/self-server/create", json=_payload())
    assert resp.status_code == 202
    body = resp.get_json()
    job_id = body["job"]["job_id"]

    deadline = time.time() + 3
    status_body = None
    while time.time() < deadline:
        status_resp = client.get(f"/api/client/provision/self-server/status/{job_id}")
        assert status_resp.status_code == 200
        status_body = status_resp.get_json()
        if status_body["job"]["status"] == "succeeded":
            break
        time.sleep(0.05)

    assert status_body["job"]["status"] == "succeeded"
    assert status_body["job"]["invitation_code"].startswith("MIRU-")
    assert "super-secret" not in str(status_body)


def test_client_provisioning_public_health_route_reports_blocked(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    job = cp.create_self_server_job(
        _payload(),
        runner_factory=lambda: FakeSuccessRunner(),
        run_inline=True,
    )

    def fake_health(_server_url):
        raise cp.ProvisioningError(
            "Miru 已在服务器上启动，但这台电脑访问不到 http://8.8.8.8:5201/api/health。"
            "请在云服务器安全组或防火墙放行 TCP 5201，或清理旧实例后重新部署到 5001。"
        )

    monkeypatch.setattr(cp, "check_self_server_public_health", fake_health)
    client = app_mod.app.test_client()

    resp = client.post(f"/api/client/provision/self-server/public-health/{job.job_id}")

    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False
    assert "TCP 5201" in body["error"]
    assert "重新部署到 5001" in body["error"]
    assert "super-secret" not in str(body)


def test_client_provisioning_public_health_route_accepts_reachable_job(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    job = cp.create_self_server_job(
        _payload(),
        runner_factory=lambda: FakeSuccessRunner(),
        run_inline=True,
    )

    monkeypatch.setattr(
        cp,
        "check_self_server_public_health",
        lambda server_url: {
            "ok": True,
            "server_url": server_url,
            "health_url": server_url.rstrip("/") + "/api/health",
        },
    )
    client = app_mod.app.test_client()

    resp = client.post(f"/api/client/provision/self-server/public-health/{job.job_id}")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["health_url"] == "http://8.8.8.8:5201/api/health"


def test_client_provisioning_unknown_job_is_explicitly_not_found(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    client = app_mod.app.test_client()

    resp = client.get("/api/client/provision/self-server/status/job-missing")

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "job not found"


def test_client_provisioning_route_accepts_image_tar_upload(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    FakeSuccessRunner.last_request = None
    cp.set_runner_factory_for_tests(lambda: FakeSuccessRunner())
    client = app_mod.app.test_client()

    form = _payload()
    form["image_tar"] = (io.BytesIO(b"fake docker image tar"), "miru-server-v0.2.0-linux-amd64.tar.gz")
    resp = client.post(
        "/api/client/provision/self-server/create",
        data=form,
        content_type="multipart/form-data",
    )

    assert resp.status_code == 202
    body = resp.get_json()
    assert body["job"]["request"]["image_source"] == "archive"
    assert body["job"]["request"]["image_tar_name"] == "miru-server-v0.2.0-linux-amd64.tar.gz"
    assert "image_tar_path" not in str(body)
    deadline = time.time() + 3
    while time.time() < deadline:
        status_resp = client.get(f"/api/client/provision/self-server/status/{body['job']['job_id']}")
        status_body = status_resp.get_json()
        if status_body["job"]["status"] == "succeeded":
            break
        time.sleep(0.05)

    assert status_body["job"]["status"] == "succeeded"
    assert FakeSuccessRunner.last_request is not None
    assert FakeSuccessRunner.last_request.image_tar_name == "miru-server-v0.2.0-linux-amd64.tar.gz"
    assert not os.path.exists(FakeSuccessRunner.last_request.image_tar_path)


def test_client_provisioning_route_accepts_and_cleans_ssh_private_key(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    FakeSuccessRunner.last_request = None
    cp.set_runner_factory_for_tests(lambda: FakeSuccessRunner())
    client = app_mod.app.test_client()

    form = _payload(ssh_password="")
    form["ssh_private_key"] = (
        io.BytesIO(b"-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n"),
        "id_ed25519",
    )
    resp = client.post(
        "/api/client/provision/self-server/create",
        data=form,
        content_type="multipart/form-data",
    )

    assert resp.status_code == 202
    body = resp.get_json()
    assert body["job"]["request"]["ssh_auth"] == "key"
    assert "ssh_private_key_path" not in str(body)
    deadline = time.time() + 3
    while time.time() < deadline:
        status_resp = client.get(f"/api/client/provision/self-server/status/{body['job']['job_id']}")
        status_body = status_resp.get_json()
        if status_body["job"]["status"] == "succeeded":
            break
        time.sleep(0.05)

    assert status_body["job"]["status"] == "succeeded"
    assert FakeSuccessRunner.last_request is not None
    assert not os.path.exists(FakeSuccessRunner.last_request.ssh_private_key_path)
    assert status_body["job"]["request"]["ssh_auth"] == "key"


def test_client_provisioning_route_accepts_non_ascii_archive_filename(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    FakeSuccessRunner.last_request = None
    cp.set_runner_factory_for_tests(lambda: FakeSuccessRunner())
    client = app_mod.app.test_client()

    form = _payload()
    form["image_tar"] = (io.BytesIO(b"fake docker image tar"), "镜像包.tar.gz")
    resp = client.post(
        "/api/client/provision/self-server/create",
        data=form,
        content_type="multipart/form-data",
    )

    assert resp.status_code == 202
    body = resp.get_json()
    assert body["job"]["request"]["image_tar_name"] == "miru-server-image.tar.gz"
    assert "镜像包" not in str(body)
    deadline = time.time() + 3
    while time.time() < deadline:
        status_resp = client.get(f"/api/client/provision/self-server/status/{body['job']['job_id']}")
        status_body = status_resp.get_json()
        if status_body["job"]["status"] == "succeeded":
            break
        time.sleep(0.05)

    assert status_body["job"]["status"] == "succeeded"
    assert FakeSuccessRunner.last_request is not None
    assert FakeSuccessRunner.last_request.image_tar_name == "miru-server-image.tar.gz"


def test_client_provisioning_route_reuses_active_job_and_cleans_duplicate_upload(monkeypatch, tmp_path):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    FakeSlowRunner.reset()
    cp.set_runner_factory_for_tests(lambda: FakeSlowRunner())
    temp_root = tmp_path / "uploads"
    temp_root.mkdir()
    created_dirs = []

    def fake_mkdtemp(prefix=""):
        path = temp_root / f"{prefix}{len(created_dirs)}"
        path.mkdir()
        created_dirs.append(path)
        return str(path)

    monkeypatch.setattr(app_mod.tempfile, "mkdtemp", fake_mkdtemp)
    client = app_mod.app.test_client()

    form = _payload()
    form["image_tar"] = (io.BytesIO(b"first image tar"), "miru-server-v0.2.0-linux-amd64.tar.gz")
    first_resp = client.post(
        "/api/client/provision/self-server/create",
        data=form,
        content_type="multipart/form-data",
    )
    assert first_resp.status_code == 202
    first_job_id = first_resp.get_json()["job"]["job_id"]
    assert FakeSlowRunner.started.wait(timeout=1)

    form2 = _payload(ssh_password="second-secret")
    form2["image_tar"] = (io.BytesIO(b"second image tar"), "miru-server-v0.2.0-linux-amd64.tar.gz")
    second_resp = client.post(
        "/api/client/provision/self-server/create",
        data=form2,
        content_type="multipart/form-data",
    )
    try:
        assert second_resp.status_code == 202
        second_body = second_resp.get_json()
        assert second_body["job"]["job_id"] == first_job_id
        assert "second-secret" not in str(second_body)
        assert len(created_dirs) == 2
        assert created_dirs[0].exists()
        assert not created_dirs[1].exists()
    finally:
        FakeSlowRunner.release.set()


def test_client_provisioning_route_rejects_oversized_multipart_before_job(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_is_client_mode", True)
    def fail_if_job_created(*_args, **_kwargs):
        raise AssertionError("oversized multipart should be rejected before job creation")

    monkeypatch.setattr(cp, "create_self_server_job", fail_if_job_created)
    client = app_mod.app.test_client()

    form = _payload()
    form["image_tar"] = (io.BytesIO(b"x"), "miru-server-v0.2.0-linux-amd64.tar.gz")
    resp = client.post(
        "/api/client/provision/self-server/create",
        data=form,
        content_type="multipart/form-data",
        environ_overrides={"CONTENT_LENGTH": str(cp.MAX_IMAGE_TAR_BYTES + 5 * 1024 * 1024)},
    )

    assert resp.status_code == 400
    body = resp.get_json()
    assert "过大" in body["error"]


def test_image_tar_stream_copy_has_hard_size_limit(tmp_path):
    import app as app_mod

    ok_path = tmp_path / "ok.tar.gz"
    app_mod._copy_stream_limited(io.BytesIO(b"abc"), str(ok_path), 3, "too big", cp.ProvisioningError)
    assert ok_path.read_bytes() == b"abc"

    too_big_path = tmp_path / "too-big.tar.gz"
    with pytest.raises(cp.ProvisioningError, match="too big"):
        app_mod._copy_stream_limited(
            io.BytesIO(b"abcdef"),
            str(too_big_path),
            3,
            "too big",
            cp.ProvisioningError,
        )


def test_create_instance_parts_skip_pull_only_when_remote_image_exists():
    request = cp.validate_self_server_payload(_payload(instance_id="inst-a"))

    with_image = cp._build_create_instance_parts(request, image_ready=True)
    without_image = cp._build_create_instance_parts(request, image_ready=False)

    assert "--skip-pull" in with_image
    assert "--skip-pull" not in without_image
    assert "--allow-no-api" in with_image
    assert "--instance-id inst-a" in with_image


def test_create_instance_parts_can_pass_remote_image_tar():
    request = cp.validate_self_server_payload(_payload(instance_id="inst-a"))

    parts = cp._build_create_instance_parts(
        request,
        image_ready=False,
        image_tar_remote="/tmp/miru-provision/image.tar.gz",
    )

    joined = " ".join(parts)
    assert "--image-tar /tmp/miru-provision/image.tar.gz" in joined
    assert "--skip-pull" not in joined


def test_paramiko_runner_pulls_missing_registry_image_then_skips_second_pull(monkeypatch):
    fake_client = FakeSSHClient(image_exists=False)
    fake_paramiko = types.SimpleNamespace(
        SSHClient=lambda: fake_client,
        AutoAddPolicy=lambda: object(),
    )
    monkeypatch.setitem(__import__("sys").modules, "paramiko", fake_paramiko)
    request = cp.validate_self_server_payload(_payload(instance_id="inst-a"))
    logs = []

    result = FakeParamikoRunner().provision(
        request,
        "job-test",
        lambda message, **meta: logs.append((message, meta)),
    )

    assert result["invitation_code"].startswith("MIRU-")
    assert any(cmd == f"docker pull {request.image}" for cmd in fake_client.commands)
    create_cmd = next(cmd for cmd in fake_client.commands if "create_instance.sh" in cmd)
    assert "--skip-pull" in create_cmd
    assert ("正在下载 Miru 服务", {"phase_key": "image_pull", "progress": 52}) in logs


def test_clear_job_secret_removes_image_tar_path_without_password():
    request = cp.SelfServerProvisionRequest(
        server_ip="8.8.8.8",
        ssh_password="",
        image_tar_path="/tmp/miru-image-test/image.tar.gz",
        image_tar_name="image.tar.gz",
        image_tar_size=123,
    )
    job = cp.ProvisionJob(job_id="job-test", request=request)

    cp._clear_job_secret(job)

    public = job.public_dict()
    assert public["request"]["image_tar_name"] == "image.tar.gz"
    assert "image_tar_path" not in str(public)
    assert job.request.image_tar_path == ""


def test_clear_job_secret_removes_private_key_and_preserves_auth_mode():
    request = cp.SelfServerProvisionRequest(
        server_ip="8.8.8.8",
        ssh_private_key_path="/tmp/miru-ssh-key-test/ssh-private-key",
        ssh_private_key_name="SSH 私钥",
        ssh_private_key_size=123,
        ssh_auth_mode="uploaded_key",
    )
    job = cp.ProvisionJob(job_id="job-test", request=request)

    cp._clear_job_secret(job)

    assert job.request.ssh_private_key_path == ""
    assert job.request.ssh_private_key_name == ""
    assert job.request.ssh_auth_mode == "uploaded_key"
    assert job.public_dict()["request"]["ssh_auth"] == "key"


def test_sanitize_message_replaces_image_path_before_password():
    request = cp.SelfServerProvisionRequest(
        server_ip="8.8.8.8",
        ssh_password="abc",
        image_tar_path="/tmp/miru-image-abc/image.tar.gz",
        image_tar_name="image.tar.gz",
    )

    clean = cp._sanitize_message("uploading /tmp/miru-image-abc/image.tar.gz with abc", request)

    assert clean == "uploading image.tar.gz with ******"
    assert "/tmp/miru-image" not in clean
    assert "abc" not in clean


def test_sanitize_message_does_not_treat_key_auth_sentinel_as_secret():
    request = cp.SelfServerProvisionRequest(
        server_ip="8.8.8.8",
        ssh_password="//",
    )

    clean = cp._sanitize_message("server_url=http://8.8.8.8:5201", request)

    assert clean == "server_url=http://8.8.8.8:5201"


def test_sanitize_message_hides_uploaded_key_path_and_passphrase():
    request = cp.SelfServerProvisionRequest(
        server_ip="8.8.8.8",
        ssh_password="key-passphrase",
        ssh_private_key_path="/tmp/miru-ssh-key-test/ssh-private-key",
        ssh_auth_mode="uploaded_key",
    )

    clean = cp._sanitize_message(
        "loading /tmp/miru-ssh-key-test/ssh-private-key with key-passphrase",
        request,
    )

    assert clean == "loading SSH 私钥 with ******"


def test_public_health_error_names_the_allocated_port(monkeypatch):
    class BrokenOpener:
        def open(self, *_args, **_kwargs):
            raise OSError("blocked")

    monkeypatch.setattr(cp.urllib.request, "build_opener", lambda *_args, **_kwargs: BrokenOpener())

    with pytest.raises(cp.ProvisioningError) as exc:
        cp.check_self_server_public_health(
            "http://8.8.8.8:5202",
            timeout=0.01,
            attempts=1,
        )

    message = str(exc.value)
    assert "这台电脑访问不到" in message
    assert "这台 Mac" not in message
    assert "http://8.8.8.8:5202/api/health" in message
    assert "TCP 5202" in message
    assert "重新部署到 5001" in message


def test_public_health_retries_until_new_server_is_ready(monkeypatch):
    calls = []

    class Response:
        status = 200

        def getcode(self):
            return self.status

        def read(self, _limit):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class StartingOpener:
        def open(self, *_args, **_kwargs):
            calls.append(True)
            if len(calls) < 3:
                raise OSError("still starting")
            return Response()

    sleeps = []
    monkeypatch.setattr(cp.urllib.request, "build_opener", lambda *_args, **_kwargs: StartingOpener())
    monkeypatch.setattr(cp.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = cp.check_self_server_public_health(
        "http://8.8.8.8:5202",
        timeout=0.01,
        attempts=3,
        retry_delay=0.25,
    )

    assert result["ok"] is True
    assert len(calls) == 3
    assert sleeps == [0.25, 0.25]


def test_paramiko_runner_uploads_image_tar_and_does_not_pull(monkeypatch, tmp_path):
    image_tar = tmp_path / "miru-server-v0.2.0-linux-amd64.tar.gz"
    image_tar.write_bytes(b"fake docker image tar")
    fake_client = FakeSSHClient(image_exists=False)
    fake_paramiko = types.SimpleNamespace(
        SSHClient=lambda: fake_client,
        AutoAddPolicy=lambda: object(),
    )
    monkeypatch.setitem(__import__("sys").modules, "paramiko", fake_paramiko)
    request = cp.validate_self_server_payload(
        _payload(
            instance_id="inst-a",
            image_tar_path=str(image_tar),
            image_tar_name=image_tar.name,
            image_tar_size=image_tar.stat().st_size,
        )
    )
    logs = []

    result = FakeParamikoRunner().provision(
        request,
        "job-test",
        lambda message, **meta: logs.append((message, meta)),
    )

    assert result["invitation_code"].startswith("MIRU-")
    assert not any(cmd.startswith("docker pull") for cmd in fake_client.commands)
    assert fake_client.uploads
    assert fake_client.uploads[-1][1].endswith("/miru-server-image.tar.gz")
    create_cmd = next(cmd for cmd in fake_client.commands if "create_instance.sh" in cmd)
    assert "--image-tar /tmp/miru-provision-job-test/miru-server-image.tar.gz" in create_cmd
    assert any(meta.get("phase_key") == "image_upload" for _message, meta in logs)
    assert any(meta.get("phase_key") == "image_load" for _message, meta in logs)


def test_progress_log_updates_phase_and_progress():
    request = cp.validate_self_server_payload(_payload())
    job = cp.ProvisionJob(job_id="job-test", request=request)

    job.log("正在下载 Miru 服务", phase_key="image_pull", progress=52)

    public = job.public_dict()
    assert public["phase"] == "正在下载 Miru 服务"
    assert public["phase_key"] == "image_pull"
    assert public["progress"] == 52


def test_extract_last_json_object_accepts_pretty_json_tail():
    raw = """
noise before
{
  "ok": true,
  "instance": {
    "invitation_code": "MIRU-AAAAABBBBB-ABCDEF"
  }
}
"""
    parsed = cp._extract_last_json_object(raw)
    assert parsed["ok"] is True
    assert parsed["instance"]["invitation_code"].startswith("MIRU-")
