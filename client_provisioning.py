"""Client-side provisioning helpers for the desktop first-run wizard.

This module runs only in desktop client mode. It lets the local Miru launcher
use SSH to ask a user's own Linux server to create a new isolated Miru instance
via the existing Host Instance Manager scripts. The resulting long invitation
code is still the only login credential.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import shlex
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


DEFAULT_HOST_HOME = "/opt/miru-host"
DEFAULT_IMAGE_VERSION = os.environ.get("MIRU_IMAGE_VERSION", "0.2.0")
DEFAULT_IMAGE = os.environ.get("MIRU_SELF_SERVER_IMAGE", f"miru/server:{DEFAULT_IMAGE_VERSION}")
DEFAULT_PORT_START = int(os.environ.get("MIRU_SELF_SERVER_PORT_START", "5001"))
DEFAULT_PORT_END = int(os.environ.get("MIRU_SELF_SERVER_PORT_END", "5010"))
MAX_IMAGE_TAR_BYTES = int(os.environ.get("MIRU_SELF_SERVER_IMAGE_TAR_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
MAX_SSH_PRIVATE_KEY_BYTES = int(
    os.environ.get("MIRU_SELF_SERVER_SSH_KEY_MAX_BYTES", str(256 * 1024))
)
PUBLIC_HEALTH_TIMEOUT_SECONDS = float(os.environ.get("MIRU_SELF_SERVER_PUBLIC_HEALTH_TIMEOUT", "5"))
PUBLIC_HEALTH_ATTEMPTS = int(os.environ.get("MIRU_SELF_SERVER_PUBLIC_HEALTH_ATTEMPTS", "3"))
PUBLIC_HEALTH_RETRY_DELAY_SECONDS = float(
    os.environ.get("MIRU_SELF_SERVER_PUBLIC_HEALTH_RETRY_DELAY", "1")
)
ALLOWED_IMAGE_TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz")
SSH_KEY_AUTH_SENTINEL = "//"

_SSH_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,180}$")
_INSTANCE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,46}[a-z0-9])?$")
_ACTIVE_JOB_STATUSES = {"queued", "running"}


class ProvisioningError(RuntimeError):
    pass


@dataclass(frozen=True)
class SelfServerProvisionRequest:
    server_ip: str
    ssh_user: str = ""
    ssh_port: int = 22
    ssh_password: str = ""
    ssh_private_key_path: str = ""
    ssh_private_key_name: str = ""
    ssh_private_key_size: int = 0
    ssh_auth_mode: str = ""
    host_home: str = DEFAULT_HOST_HOME
    port_start: int = DEFAULT_PORT_START
    port_end: int = DEFAULT_PORT_END
    image: str = DEFAULT_IMAGE
    image_tar_path: str = ""
    image_tar_name: str = ""
    image_tar_size: int = 0
    instance_id: str = ""
    allow_no_api: bool = True

    def public_dict(self) -> dict[str, Any]:
        return {
            "server_ip": self.server_ip,
            "ssh_user": self.ssh_user,
            "ssh_port": self.ssh_port,
            "ssh_auth": "key" if _uses_ssh_key_auth(self) else "password",
            "host_home": self.host_home,
            "port_start": self.port_start,
            "port_end": self.port_end,
            "image": self.image,
            "image_source": "archive" if self.image_tar_path else "registry",
            "image_tar_name": self.image_tar_name,
            "image_tar_size": self.image_tar_size,
            "instance_id": self.instance_id,
            "allow_no_api": self.allow_no_api,
        }


@dataclass
class ProvisionJob:
    job_id: str
    request: SelfServerProvisionRequest
    status: str = "queued"
    phase: str = "等待开始"
    phase_key: str = "queued"
    progress: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    invitation_code: str = ""
    server_url: str = ""
    instance_id: str = ""
    error: str = ""
    logs: list[str] = field(default_factory=list)

    def log(
        self,
        message: str,
        *,
        phase_key: str | None = None,
        progress: int | None = None,
    ) -> None:
        clean = _sanitize_message(message, self.request)
        if clean:
            self.logs.append(clean)
            if len(self.logs) > 120:
                self.logs = self.logs[-120:]
            if phase_key:
                self.phase_key = phase_key
                self.phase = clean
            elif clean.startswith("正在"):
                self.phase = clean
            if progress is not None:
                self.progress = max(0, min(100, int(progress)))
        self.updated_at = time.time()

    def public_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "phase": self.phase,
            "phase_key": self.phase_key,
            "progress": self.progress,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "invitation_code": self.invitation_code,
            "server_url": self.server_url,
            "instance_id": self.instance_id,
            "error": self.error,
            "logs": list(self.logs),
            "request": self.request.public_dict(),
        }


class ParamikoProvisionRunner:
    """Real SSH runner used by installed desktop clients.

    Password-based SSH is intentionally implemented through Paramiko instead
    of shelling out to `ssh`, because the system SSH client cannot accept a
    password non-interactively without extra tools.
    """

    def provision(
        self,
        request: SelfServerProvisionRequest,
        job_id: str,
        on_log: Callable[..., None],
    ) -> dict[str, Any]:
        try:
            import paramiko  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on release env
            raise ProvisioningError(
                "当前 Miru 缺少 SSH 密码连接组件，请重新安装包含自有服务器向导的桌面版。"
            ) from exc

        remote_root = f"/tmp/miru-provision-{job_id}"
        remote_repo = f"{remote_root}/repo"
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            _emit_progress(on_log, "正在连接服务器", "connect", 12)
            use_key_auth = _uses_ssh_key_auth(request)
            use_uploaded_key = bool(request.ssh_private_key_path)
            try:
                client.connect(
                    hostname=request.server_ip,
                    port=request.ssh_port,
                    username=request.ssh_user,
                    password=None if use_key_auth else request.ssh_password,
                    passphrase=(request.ssh_password or None) if use_uploaded_key else None,
                    key_filename=request.ssh_private_key_path or None,
                    timeout=20,
                    banner_timeout=20,
                    auth_timeout=20,
                    look_for_keys=use_key_auth and not use_uploaded_key,
                    allow_agent=use_key_auth and not use_uploaded_key,
                )
            except Exception as exc:
                if use_uploaded_key:
                    raise ProvisioningError(
                        "SSH 私钥登录失败。请确认选择的是这个服务器对应的私钥；"
                        "如果私钥有口令，请在 SSH 密码栏填写私钥口令。"
                    ) from exc
                if use_key_auth:
                    raise ProvisioningError(
                        "SSH 密钥登录失败。请确认这台电脑的 SSH agent 或默认 SSH 密钥可以登录这个用户；"
                        "也可以改填 SSH 登录密码。"
                    ) from exc
                raise
            use_sudo = request.ssh_user != "root"
            sudo_password = (
                request.ssh_password
                if (
                    use_sudo
                    and request.ssh_password
                    and request.ssh_password.strip() != SSH_KEY_AUTH_SENTINEL
                )
                else ""
            )
            if use_sudo:
                try:
                    self._run(client, "true", timeout=30, sudo=True, sudo_password=sudo_password)
                except ProvisioningError as exc:
                    if use_key_auth and not sudo_password:
                        raise ProvisioningError(
                            "SSH 密钥登录成功，但这个用户不能免密 sudo。请换用有免密 sudo 权限的管理员用户，"
                            "或者在 SSH 密码栏填写登录密码让 Miru 通过 sudo 完成安装。"
                        ) from exc
                    else:
                        raise ProvisioningError(
                            "SSH 登录成功，但这个用户没有可用的 sudo 权限。请填写云服务器登录页显示的用户名，"
                            "并确认它可以使用 sudo。"
                        ) from exc
            self._run(client, f"rm -rf {shlex.quote(remote_root)} && mkdir -p {shlex.quote(remote_repo)}")
            _emit_progress(on_log, "正在准备 Miru 需要的文件", "upload", 24)
            with client.open_sftp() as sftp:
                self._upload_bundle(sftp, remote_repo)
                remote_image_tar = ""
                if request.image_tar_path:
                    remote_image_tar = self._upload_image_tar(
                        sftp,
                        remote_root,
                        request,
                        on_log,
                    )
            self._run(
                client,
                "chmod +x "
                f"{shlex.quote(remote_repo)}/deploy/host_manager/*.sh "
                f"{shlex.quote(remote_repo)}/deploy/self_host/*.sh "
                f"{shlex.quote(remote_repo)}/scripts/host_manager.py",
            )

            _emit_progress(on_log, "正在准备运行环境", "host_prepare", 38)
            host_install = (
                "bash deploy/host_manager/host_install.sh"
                f" --home {shlex.quote(request.host_home)}"
                f" --server-ip {shlex.quote(request.server_ip)}"
                f" --port-start {request.port_start}"
                f" --port-end {request.port_end}"
                f" --default-image {shlex.quote(request.image)}"
            )
            host_json = f"{request.host_home.rstrip('/')}/host.json"
            if self._remote_file_exists(client, host_json, sudo=use_sudo, sudo_password=sudo_password):
                host_config = self._read_remote_json_file(
                    client,
                    host_json,
                    sudo=use_sudo,
                    sudo_password=sudo_password,
                )
                existing_range = host_config.get("port_range") or {}
                try:
                    existing_start = int(existing_range.get("start"))
                    existing_end = int(existing_range.get("end"))
                except Exception as exc:
                    raise ProvisioningError(
                        "服务器上的 Miru 管理配置缺少有效端口范围，请换一个新的服务器保存位置。"
                    ) from exc
                requested_range = (request.port_start, request.port_end)
                if (existing_start, existing_end) != requested_range:
                    raise ProvisioningError(
                        "服务器保存位置已有 Miru 配置，端口范围为 "
                        f"{existing_start}-{existing_end}；你本次填写的是 "
                        f"{request.port_start}-{request.port_end}。"
                        "请改回原范围，或换一个新的服务器保存位置。"
                    )
                _emit_progress(
                    on_log,
                    "服务器上已有 Miru 管理目录，将复用现有端口和实例管理设置",
                    "host_reuse",
                    42,
                )
            else:
                host_cmd = f"cd {shlex.quote(remote_repo)} && {host_install}"
                self._run(client, host_cmd, timeout=900, sudo=use_sudo, sudo_password=sudo_password)

            image_ready = False
            if remote_image_tar:
                _emit_progress(on_log, "正在准备 Miru 服务包", "image_load", 68)
            else:
                _emit_progress(on_log, "正在检查在线 Miru 镜像", "image_check", 46)
                image_exists = self._remote_image_exists(
                    client, request.image, sudo=use_sudo, sudo_password=sudo_password
                )
                if image_exists:
                    image_ready = True
                    _emit_progress(on_log, "服务器已有 Miru 镜像，跳过下载", "image_ready", 62)
                else:
                    _emit_progress(on_log, "正在下载 Miru 服务", "image_pull", 52)
                    pull_cmd = f"docker pull {shlex.quote(request.image)}"
                    self._run(client, pull_cmd, timeout=1800, sudo=use_sudo, sudo_password=sudo_password)
                    image_ready = True
                    _emit_progress(on_log, "Miru 服务下载完成", "image_ready", 68)
                _emit_progress(on_log, "正在唤醒 Miru", "create_instance", 76)
            create_parts = _build_create_instance_parts(
                request,
                image_ready=image_ready,
                image_tar_remote=remote_image_tar,
            )
            create_cmd = f"cd {shlex.quote(remote_repo)} && {' '.join(create_parts)}"
            try:
                output = self._run(
                    client,
                    create_cmd,
                    timeout=1500,
                    sudo=use_sudo,
                    sudo_password=sudo_password,
                )
            except ProvisioningError:
                _emit_progress(
                    on_log,
                    "Miru 首次启动未完成，正在自动重试一次",
                    "create_instance",
                    76,
                )
                output = self._run(
                    client,
                    create_cmd,
                    timeout=1500,
                    sudo=use_sudo,
                    sudo_password=sudo_password,
                )
            result = _extract_last_json_object(output)
            instance = result.get("instance") or {}
            invitation_code = str(instance.get("invitation_code") or "")
            if not invitation_code.startswith("MIRU-"):
                raise ProvisioningError("服务器已启动，但没有返回有效邀请码")
            skipped_ports = (
                (instance.get("port_selection") or {}).get("skipped_system_ports") or []
            )
            if skipped_ports:
                skipped_text = "、".join(str(port) for port in skipped_ports)
                selected_port = instance.get("server_port")
                _emit_progress(
                    on_log,
                    f"检测到端口 {skipped_text} 已被占用，已改用 {selected_port}",
                    "create_instance",
                    92,
                )
            _emit_progress(on_log, "Miru 已经准备好了", "ready", 94)
            self._run(
                client,
                f"rm -rf {shlex.quote(remote_root)}",
                timeout=60,
                check=False,
                sudo=use_sudo,
                sudo_password=sudo_password,
            )
            return {
                "invitation_code": invitation_code,
                "server_url": str(instance.get("server_url") or ""),
                "instance_id": str(instance.get("instance_id") or ""),
                "raw": result,
            }
        finally:
            client.close()

    def _run(
        self,
        client: Any,
        command: str,
        timeout: int = 600,
        check: bool = True,
        sudo: bool = False,
        sudo_password: str = "",
    ) -> str:
        actual_command = command
        if sudo:
            if sudo_password:
                actual_command = f"sudo -S -p '' bash -lc {shlex.quote(command)}"
            else:
                actual_command = f"sudo -n bash -lc {shlex.quote(command)}"
        stdin, stdout, stderr = client.exec_command(actual_command, timeout=timeout)
        try:
            if sudo and sudo_password:
                stdin.write(sudo_password + "\n")
                try:
                    stdin.flush()
                except Exception:
                    pass
            stdin.close()
        except Exception:
            pass
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
        combined = "\n".join(part for part in (out.strip(), err.strip()) if part)
        if check and status != 0:
            raise ProvisioningError(_trim_error(combined) or f"远端命令失败，退出码 {status}")
        return combined

    def _remote_image_exists(
        self,
        client: Any,
        image: str,
        sudo: bool = False,
        sudo_password: str = "",
    ) -> bool:
        command = f"docker image inspect {shlex.quote(image)} >/dev/null 2>&1"
        if sudo:
            if sudo_password:
                command = f"sudo -S -p '' bash -lc {shlex.quote(command)}"
            else:
                command = f"sudo -n bash -lc {shlex.quote(command)}"
        stdin, stdout, stderr = client.exec_command(command, timeout=30)
        try:
            if sudo and sudo_password:
                stdin.write(sudo_password + "\n")
                try:
                    stdin.flush()
                except Exception:
                    pass
            stdin.close()
        except Exception:
            pass
        stdout.read()
        stderr.read()
        return stdout.channel.recv_exit_status() == 0

    def _remote_file_exists(
        self,
        client: Any,
        path: str,
        sudo: bool = False,
        sudo_password: str = "",
    ) -> bool:
        command = f"test -f {shlex.quote(path)}"
        if sudo:
            if sudo_password:
                command = f"sudo -S -p '' bash -lc {shlex.quote(command)}"
            else:
                command = f"sudo -n bash -lc {shlex.quote(command)}"
        stdin, stdout, stderr = client.exec_command(command, timeout=30)
        try:
            if sudo and sudo_password:
                stdin.write(sudo_password + "\n")
                try:
                    stdin.flush()
                except Exception:
                    pass
            stdin.close()
        except Exception:
            pass
        stdout.read()
        stderr.read()
        return stdout.channel.recv_exit_status() == 0

    def _read_remote_json_file(
        self,
        client: Any,
        path: str,
        sudo: bool = False,
        sudo_password: str = "",
    ) -> dict[str, Any]:
        output = self._run(
            client,
            f"cat {shlex.quote(path)}",
            timeout=30,
            sudo=sudo,
            sudo_password=sudo_password,
        )
        try:
            data = json.loads(output)
        except Exception as exc:
            raise ProvisioningError("服务器上的 Miru 管理配置无法读取，请换一个新的服务器保存位置。") from exc
        if not isinstance(data, dict):
            raise ProvisioningError("服务器上的 Miru 管理配置格式不正确，请换一个新的服务器保存位置。")
        return data

    def _upload_image_tar(
        self,
        sftp: Any,
        remote_root: str,
        request: SelfServerProvisionRequest,
        on_log: Callable[..., None],
    ) -> str:
        local_path = Path(request.image_tar_path)
        suffix = _image_tar_suffix(request.image_tar_name or local_path.name) or ".tar"
        remote_path = f"{remote_root}/miru-server-image{suffix}"
        last_bucket = -1

        def progress(sent: int, total: int) -> None:
            nonlocal last_bucket
            if total <= 0:
                return
            pct = max(0, min(100, int(sent * 100 / total)))
            bucket = pct // 10
            if bucket == last_bucket and pct < 100:
                return
            last_bucket = bucket
            scaled = 28 + int(pct * 0.18)
            _emit_progress(
                on_log,
                f"正在上传 Miru 服务包（{pct}%）",
                "image_upload",
                scaled,
            )

        _emit_progress(on_log, "正在上传 Miru 服务包", "image_upload", 28)
        sftp.put(str(local_path), remote_path, callback=progress)
        try:
            sftp.chmod(remote_path, 0o600)
        except Exception:
            pass
        _emit_progress(on_log, "Miru 服务包上传完成", "image_uploaded", 46)
        return remote_path

    def _upload_bundle(self, sftp: Any, remote_repo: str) -> None:
        root = _bundle_root()
        upload_items = [
            (root / "deploy" / "host_manager", f"{remote_repo}/deploy/host_manager"),
            (root / "deploy" / "self_host", f"{remote_repo}/deploy/self_host"),
            (root / "scripts" / "host_manager.py", f"{remote_repo}/scripts/host_manager.py"),
        ]
        for local_path, remote_path in upload_items:
            if not local_path.exists():
                raise ProvisioningError(f"本地部署资源缺失：{local_path}")
            if local_path.is_dir():
                self._mkdir_p(sftp, remote_path)
                for child in local_path.rglob("*"):
                    rel = child.relative_to(local_path)
                    if "__pycache__" in rel.parts:
                        continue
                    target = f"{remote_path}/{rel.as_posix()}"
                    if child.is_dir():
                        self._mkdir_p(sftp, target)
                    else:
                        self._mkdir_p(sftp, str(Path(target).parent).replace("\\", "/"))
                        sftp.put(str(child), target)
                        sftp.chmod(target, 0o755 if child.suffix == ".sh" else 0o644)
            else:
                self._mkdir_p(sftp, str(Path(remote_path).parent).replace("\\", "/"))
                sftp.put(str(local_path), remote_path)
                sftp.chmod(remote_path, 0o755)

    def _mkdir_p(self, sftp: Any, remote_path: str) -> None:
        parts = [p for p in remote_path.split("/") if p]
        cur = ""
        for part in parts:
            cur += "/" + part
            try:
                sftp.stat(cur)
            except Exception:
                sftp.mkdir(cur)


_jobs: dict[str, ProvisionJob] = {}
_jobs_lock = threading.Lock()
_runner_factory: Callable[[], Any] = ParamikoProvisionRunner


def set_runner_factory_for_tests(factory: Callable[[], Any]) -> None:
    global _runner_factory
    _runner_factory = factory


def reset_jobs_for_tests() -> None:
    with _jobs_lock:
        _jobs.clear()


def _build_create_instance_parts(
    request: SelfServerProvisionRequest,
    *,
    image_ready: bool,
    image_tar_remote: str = "",
) -> list[str]:
    create_parts = [
        "bash deploy/host_manager/create_instance.sh",
        f"--home {shlex.quote(request.host_home)}",
        f"--server-ip {shlex.quote(request.server_ip)}",
        f"--image {shlex.quote(request.image)}",
        "--json",
    ]
    if request.instance_id:
        create_parts.append(f"--instance-id {shlex.quote(request.instance_id)}")
    if request.allow_no_api:
        create_parts.append("--allow-no-api")
    if image_tar_remote:
        create_parts.append(f"--image-tar {shlex.quote(image_tar_remote)}")
    if image_ready:
        create_parts.append("--skip-pull")
    return create_parts


def validate_self_server_payload(payload: dict[str, Any]) -> SelfServerProvisionRequest:
    server_ip = str(payload.get("server_ip") or payload.get("ip") or "").strip()
    try:
        ip = ipaddress.IPv4Address(server_ip)
        server_ip = str(ip)
    except Exception as exc:
        raise ProvisioningError("请输入有效的服务器公网 IPv4") from exc
    if not ip.is_global:
        raise ProvisioningError("请输入可公网访问的服务器 IPv4，不要使用内网、回环或保留地址")

    ssh_user = str(payload.get("ssh_user") or "").strip()
    if not ssh_user:
        raise ProvisioningError("请输入 SSH 用户名，和云服务器登录窗口里的用户名保持一致")
    if not _SSH_USER_RE.match(ssh_user):
        raise ProvisioningError("SSH 用户名只能包含字母、数字、点、下划线和横线")

    ssh_port = _validate_port(payload.get("ssh_port") or 22, "SSH 端口")
    ssh_password = str(payload.get("ssh_password") or "")
    ssh_private_key_path = str(payload.get("ssh_private_key_path") or "").strip()
    ssh_private_key_name = str(payload.get("ssh_private_key_name") or "").strip()
    ssh_private_key_size = int(payload.get("ssh_private_key_size") or 0)
    if ssh_private_key_path:
        _validate_ssh_private_key_file(
            ssh_private_key_path,
            ssh_private_key_size,
        )
    if not ssh_password and not ssh_private_key_path:
        raise ProvisioningError("请输入 SSH 登录密码；如果要使用本机 SSH 密钥，请输入 //")
    if len(ssh_password) > 512:
        raise ProvisioningError("SSH 密码过长")

    host_home = str(payload.get("host_home") or DEFAULT_HOST_HOME).strip()
    if not host_home.startswith("/") or "\x00" in host_home or len(host_home) > 180:
        raise ProvisioningError("服务器保存位置必须是 Linux 绝对路径")

    port_start = _validate_port(payload.get("port_start") or DEFAULT_PORT_START, "起始端口")
    port_end = _validate_port(payload.get("port_end") or DEFAULT_PORT_END, "结束端口")
    if port_start > port_end:
        raise ProvisioningError("起始端口不能大于结束端口")

    image = str(payload.get("image") or DEFAULT_IMAGE).strip()
    if not _IMAGE_RE.match(image):
        raise ProvisioningError("Docker 镜像名格式不正确")

    image_tar_path = str(payload.get("image_tar_path") or "").strip()
    image_tar_name = str(payload.get("image_tar_name") or "").strip()
    image_tar_size = int(payload.get("image_tar_size") or 0)
    if image_tar_path:
        _validate_image_tar_file(image_tar_path, image_tar_name, image_tar_size)

    instance_id = str(payload.get("instance_id") or "").strip()
    if instance_id and not _INSTANCE_ID_RE.match(instance_id):
        raise ProvisioningError("实例 ID 只能包含小写字母、数字和横线，且不能以横线开头或结尾")

    return SelfServerProvisionRequest(
        server_ip=server_ip,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        ssh_password=ssh_password,
        ssh_private_key_path=ssh_private_key_path,
        ssh_private_key_name=ssh_private_key_name,
        ssh_private_key_size=ssh_private_key_size,
        ssh_auth_mode=(
            "uploaded_key"
            if ssh_private_key_path
            else ("agent_key" if ssh_password.strip() == SSH_KEY_AUTH_SENTINEL else "password")
        ),
        host_home=host_home,
        port_start=port_start,
        port_end=port_end,
        image=image,
        image_tar_path=image_tar_path,
        image_tar_name=image_tar_name,
        image_tar_size=image_tar_size,
        instance_id=instance_id,
        allow_no_api=_parse_bool(payload.get("allow_no_api", True), default=True),
    )


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "y", "on"):
            return True
        if v in ("0", "false", "no", "n", "off"):
            return False
        if v == "":
            return default
    return default


def create_self_server_job(
    payload: dict[str, Any],
    *,
    runner_factory: Callable[[], Any] | None = None,
    run_inline: bool = False,
) -> ProvisionJob:
    request = validate_self_server_payload(payload)
    job = ProvisionJob(job_id=uuid.uuid4().hex[:12], request=request)
    with _jobs_lock:
        existing = _find_active_job_for_request_locked(request)
        if existing:
            existing.log(
                "检测到同一台服务器正在创建 Miru，已回到进行中的任务",
                phase_key=existing.phase_key,
                progress=existing.progress,
            )
            return existing
        _jobs[job.job_id] = job

    def target() -> None:
        _run_job(job, runner_factory or _runner_factory)

    if run_inline:
        target()
    else:
        thread = threading.Thread(target=target, daemon=True, name=f"miru-provision-{job.job_id}")
        thread.start()
    return job


def get_job(job_id: str) -> ProvisionJob | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def check_self_server_public_health(
    server_url: str,
    *,
    timeout: float | None = None,
    attempts: int | None = None,
    retry_delay: float | None = None,
) -> dict[str, Any]:
    """Confirm the newly created server is reachable from this desktop."""
    base = str(server_url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProvisioningError("服务器已启动，但没有返回可检查的访问地址")
    health_url = f"{base}/api/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    effective_timeout = PUBLIC_HEALTH_TIMEOUT_SECONDS if timeout is None else timeout
    effective_attempts = max(1, PUBLIC_HEALTH_ATTEMPTS if attempts is None else int(attempts))
    effective_delay = (
        PUBLIC_HEALTH_RETRY_DELAY_SECONDS if retry_delay is None else max(0.0, retry_delay)
    )
    last_error: Exception | None = None
    for attempt in range(effective_attempts):
        request = urllib.request.Request(health_url, headers={"Accept": "application/json"})
        try:
            with opener.open(request, timeout=effective_timeout) as response:
                status = getattr(response, "status", response.getcode())
                body = response.read(4096).decode("utf-8", errors="replace")
            if status < 200 or status >= 300:
                raise ValueError(f"unexpected HTTP status {status}")
            data = json.loads(body or "{}")
            if data.get("ok") is not True:
                raise ValueError("health response is not ready")
            return {"ok": True, "server_url": base, "health_url": health_url}
        except Exception as exc:
            last_error = exc
            if attempt + 1 < effective_attempts and effective_delay > 0:
                time.sleep(effective_delay)
    raise ProvisioningError(_public_health_error_message(base, parsed)) from last_error


def _find_active_job_for_request_locked(request: SelfServerProvisionRequest) -> ProvisionJob | None:
    for job in _jobs.values():
        if job.status not in _ACTIVE_JOB_STATUSES:
            continue
        if (
            job.request.server_ip == request.server_ip
            and job.request.ssh_port == request.ssh_port
            and job.request.host_home == request.host_home
        ):
            return job
    return None


def _run_job(job: ProvisionJob, runner_factory: Callable[[], Any]) -> None:
    job.status = "running"
    job.phase = "正在准备"
    job.phase_key = "prepare"
    job.progress = 5
    job.updated_at = time.time()
    try:
        runner = runner_factory()
        result = runner.provision(job.request, job.job_id, job.log)
        job.invitation_code = str(result.get("invitation_code") or "")
        job.server_url = str(result.get("server_url") or "")
        job.instance_id = str(result.get("instance_id") or "")
        _cleanup_local_image_tar(job.request.image_tar_path)
        _cleanup_local_ssh_private_key(job.request.ssh_private_key_path)
        job.phase = "完成"
        job.phase_key = "done"
        job.progress = 100
        job.status = "succeeded"
        job.updated_at = time.time()
    except Exception as exc:
        job.status = "failed"
        job.phase = "失败"
        job.phase_key = "failed"
        job.error = _sanitize_message(_trim_error(str(exc)), job.request)
        job.log(job.error)
        job.updated_at = time.time()
    finally:
        _cleanup_local_image_tar(job.request.image_tar_path)
        _cleanup_local_ssh_private_key(job.request.ssh_private_key_path)
        _clear_job_secret(job)


def _validate_port(value: Any, label: str) -> int:
    try:
        port = int(value)
    except Exception as exc:
        raise ProvisioningError(f"{label}必须是数字") from exc
    if port < 1 or port > 65535:
        raise ProvisioningError(f"{label}必须在 1 到 65535 之间")
    return port


def _public_health_error_message(server_url: str, parsed: urllib.parse.ParseResult) -> str:
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return (
        f"Miru 已在服务器上启动，但这台电脑访问不到 {server_url}/api/health。"
        f"请在云服务器安全组或防火墙放行 TCP {port}，"
        "或清理旧实例后重新部署到 5001。"
    )


def _bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()


def _extract_last_json_object(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    decoder = json.JSONDecoder()
    fallback = None
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            data, _end = decoder.raw_decode(output[index:])
        except Exception:
            continue
        if isinstance(data, dict):
            if "ok" in data:
                return data
            fallback = data
    if isinstance(fallback, dict):
        return fallback
    raise ProvisioningError("服务器没有返回可解析的创建结果")


def _trim_error(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _sanitize_message(message: str, request: SelfServerProvisionRequest) -> str:
    message = str(message or "")
    if request.image_tar_path:
        message = message.replace(request.image_tar_path, request.image_tar_name or "Miru 服务包")
    if request.ssh_private_key_path:
        message = message.replace(request.ssh_private_key_path, "SSH 私钥")
    if request.ssh_password and request.ssh_password.strip() != SSH_KEY_AUTH_SENTINEL:
        message = message.replace(request.ssh_password, "******")
    return message.strip()


def _uses_ssh_key_auth(request: SelfServerProvisionRequest) -> bool:
    return bool(request.ssh_private_key_path) or (
        str(request.ssh_password or "").strip() == SSH_KEY_AUTH_SENTINEL
    ) or request.ssh_auth_mode in {"agent_key", "uploaded_key"}


def _emit_progress(
    on_log: Callable[..., None],
    message: str,
    phase_key: str,
    progress: int,
) -> None:
    try:
        on_log(message, phase_key=phase_key, progress=progress)
    except TypeError:
        on_log(message)


def _clear_job_secret(job: ProvisionJob) -> None:
    if (
        not job.request.ssh_password
        and not job.request.image_tar_path
        and not job.request.ssh_private_key_path
    ):
        return
    job.request = SelfServerProvisionRequest(
        server_ip=job.request.server_ip,
        ssh_user=job.request.ssh_user,
        ssh_port=job.request.ssh_port,
        ssh_password="",
        ssh_private_key_path="",
        ssh_private_key_name="",
        ssh_private_key_size=0,
        ssh_auth_mode=job.request.ssh_auth_mode,
        host_home=job.request.host_home,
        port_start=job.request.port_start,
        port_end=job.request.port_end,
        image=job.request.image,
        image_tar_path="",
        image_tar_name=job.request.image_tar_name,
        image_tar_size=job.request.image_tar_size,
        instance_id=job.request.instance_id,
        allow_no_api=job.request.allow_no_api,
    )


def is_allowed_image_tar_name(filename: str) -> bool:
    return bool(_image_tar_suffix(filename))


def image_tar_suffix(filename: str) -> str:
    return _image_tar_suffix(filename)


def _image_tar_suffix(filename: str) -> str:
    lowered = str(filename or "").lower()
    for suffix in ALLOWED_IMAGE_TAR_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    return ""


def _validate_image_tar_file(path: str, name: str, declared_size: int) -> None:
    filename = name or Path(path).name
    if not is_allowed_image_tar_name(filename):
        raise ProvisioningError("Miru 服务包必须是 .tar、.tar.gz 或 .tgz 文件")
    file_path = Path(path)
    if not file_path.is_file():
        raise ProvisioningError("Miru 服务包不存在，请重新选择文件")
    size = file_path.stat().st_size
    if size <= 0:
        raise ProvisioningError("Miru 服务包是空文件，请重新下载")
    if size > MAX_IMAGE_TAR_BYTES:
        raise ProvisioningError("Miru 服务包过大，请确认下载的是正确的 Miru 服务包")
    if declared_size and abs(int(declared_size) - size) > 1024:
        raise ProvisioningError("Miru 服务包大小异常，请重新选择文件")


def _validate_ssh_private_key_file(path: str, declared_size: int) -> None:
    file_path = Path(path)
    if not file_path.is_file():
        raise ProvisioningError("SSH 私钥不存在，请重新选择文件")
    size = file_path.stat().st_size
    if size <= 0:
        raise ProvisioningError("SSH 私钥是空文件，请重新选择")
    if size > MAX_SSH_PRIVATE_KEY_BYTES:
        raise ProvisioningError("SSH 私钥文件过大，请确认选择的是正确的私钥")
    if declared_size and abs(int(declared_size) - size) > 1024:
        raise ProvisioningError("SSH 私钥文件大小异常，请重新选择")


def _cleanup_local_image_tar(path: str) -> None:
    if not path:
        return
    try:
        file_path = Path(path)
        if file_path.exists():
            file_path.unlink()
        parent = file_path.parent
        if parent.name.startswith("miru-image-"):
            shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass


def _cleanup_local_ssh_private_key(path: str) -> None:
    if not path:
        return
    try:
        file_path = Path(path)
        if file_path.exists():
            file_path.unlink()
        parent = file_path.parent
        if parent.name.startswith("miru-ssh-key-"):
            shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass
