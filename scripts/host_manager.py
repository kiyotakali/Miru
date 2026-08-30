#!/usr/bin/env python3
"""Host-level instance ledger for Miru private server deployments.

This module intentionally contains no Docker calls. Shell scripts in
deploy/host_manager use it as the single source of truth for instance ids,
host ports, directories, container names, and invite metadata.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import ipaddress
import json
import os
import secrets
import socket
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_HOME = Path("/opt/miru-host")
DEFAULT_IMAGE = os.environ.get(
    "MIRU_HOST_DEFAULT_IMAGE",
    os.environ.get(
        "MIRU_SELF_SERVER_IMAGE",
        "miru/server:0.2.0",
    ),
)
DEFAULT_PORT_START = 5001
DEFAULT_PORT_END = 5010
HOST_VERSION = 1
INSTANCE_ID_RE = "abcdefghijklmnopqrstuvwxyz0123456789-"


class HostManagerError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def as_home(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else DEFAULT_HOME


def validate_ipv4(value: str) -> str:
    try:
        ip = ipaddress.IPv4Address(value)
    except Exception as exc:  # pragma: no cover - exact exception varies
        raise HostManagerError(f"server_ip must be IPv4, got {value}") from exc
    return str(ip)


def validate_port(value: int | str) -> int:
    try:
        port = int(value)
    except Exception as exc:  # pragma: no cover
        raise HostManagerError(f"port must be an integer, got {value}") from exc
    if port < 1 or port > 65535:
        raise HostManagerError(f"port must be 1..65535, got {value}")
    return port


def validate_port_range(start: int | str, end: int | str) -> tuple[int, int]:
    start_i = validate_port(start)
    end_i = validate_port(end)
    if start_i > end_i:
        raise HostManagerError("port_start must be <= port_end")
    return start_i, end_i


def host_port_is_available(value: int | str) -> bool:
    """Return whether Docker can bind this host TCP port.

    The instance ledger only coordinates Miru instances managed from the same
    host home. A production service or a second host home may already own a
    port without appearing in that ledger, so allocation must also consult the
    operating system.
    """
    port = validate_port(value)
    bound_sockets: list[socket.socket] = []
    candidates = ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::"))
    try:
        for family, address in candidates:
            try:
                probe = socket.socket(family, socket.SOCK_STREAM)
            except OSError as exc:
                if family == socket.AF_INET6 and exc.errno in {
                    errno.EAFNOSUPPORT,
                    errno.EPROTONOSUPPORT,
                }:
                    continue
                raise HostManagerError(f"failed to inspect host port {port}: {exc}") from exc
            try:
                if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                probe.bind((address, port))
                bound_sockets.append(probe)
            except OSError as exc:
                probe.close()
                if family == socket.AF_INET6 and exc.errno in {
                    errno.EAFNOSUPPORT,
                    errno.EADDRNOTAVAIL,
                    errno.EPROTONOSUPPORT,
                }:
                    continue
                if exc.errno in {errno.EADDRINUSE, errno.EACCES}:
                    return False
                raise HostManagerError(f"failed to inspect host port {port}: {exc}") from exc
        return True
    finally:
        for probe in bound_sockets:
            probe.close()


def validate_instance_id(value: str) -> str:
    if not (3 <= len(value) <= 48):
        raise HostManagerError("instance_id must be 3..48 characters")
    if value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789":
        raise HostManagerError("instance_id must start with lowercase letter or digit")
    if value[-1] == "-":
        raise HostManagerError("instance_id must not end with '-'")
    if "--" in value:
        raise HostManagerError("instance_id must not contain consecutive '-'")
    if any(ch not in INSTANCE_ID_RE for ch in value):
        raise HostManagerError("instance_id may contain only lowercase letters, digits, and '-'")
    return value


def generate_instance_id() -> str:
    return f"inst-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"


def safe_compose_project(instance_id: str) -> str:
    return f"miru_{instance_id.replace('-', '_')}"


def container_name(instance_id: str) -> str:
    return f"miru-{instance_id}"


def host_path(home: Path) -> Path:
    return home / "host.json"


def instances_path(home: Path) -> Path:
    return home / "instances.json"


def ports_path(home: Path) -> Path:
    return home / "ports.json"


def instance_home(home: Path, instance_id: str) -> Path:
    return home / "instances" / instance_id


def ensure_base_dirs(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    for name in ("instances", "backups", "logs"):
        path = home / name
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise HostManagerError(f"{path} must contain a JSON object")
    return data


def write_json_atomic(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


@contextmanager
def locked(home: Path):
    ensure_base_dirs(home)
    lock_file = home / ".host-manager.lock"
    with lock_file.open("a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def default_host_config(server_ip: str, port_start: int, port_end: int, image: str) -> dict[str, Any]:
    ts = now_iso()
    return {
        "version": HOST_VERSION,
        "server_ip": server_ip,
        "port_range": {"start": port_start, "end": port_end},
        "default_image": image,
        "created_at": ts,
        "updated_at": ts,
    }


def default_instances() -> dict[str, Any]:
    return {"version": HOST_VERSION, "instances": {}}


def default_ports() -> dict[str, Any]:
    return {"version": HOST_VERSION, "ports": {}}


def read_host_config(home: Path) -> dict[str, Any]:
    path = host_path(home)
    if not path.exists():
        raise HostManagerError(f"{path} does not exist; run host_install.sh first")
    data = load_json(path, {})
    if "server_ip" not in data or "port_range" not in data:
        raise HostManagerError(f"{path} is missing required host configuration")
    return data


def init_host(args: argparse.Namespace) -> dict[str, Any]:
    home = as_home(args.home)
    server_ip = validate_ipv4(args.server_ip)
    port_start, port_end = validate_port_range(args.port_start, args.port_end)
    image = args.default_image or DEFAULT_IMAGE
    with locked(home):
        host_file = host_path(home)
        if host_file.exists() and not args.force:
            existing = read_host_config(home)
            expected = {
                "server_ip": server_ip,
                "port_range": {"start": port_start, "end": port_end},
                "default_image": image,
            }
            actual = {
                "server_ip": existing.get("server_ip"),
                "port_range": existing.get("port_range"),
                "default_image": existing.get("default_image"),
            }
            if actual != expected:
                raise HostManagerError(
                    "host.json already exists with different values; use --force to replace"
                )
            changed = False
            host = existing
        else:
            host = default_host_config(server_ip, port_start, port_end, image)
            write_json_atomic(host_file, host)
            changed = True
        if not instances_path(home).exists():
            write_json_atomic(instances_path(home), default_instances())
        if not ports_path(home).exists():
            write_json_atomic(ports_path(home), default_ports())
    return {"ok": True, "changed": changed, "home": str(home), "host": host}


def allocate_instance(args: argparse.Namespace) -> dict[str, Any]:
    home = as_home(args.home)
    instance_id = validate_instance_id(args.instance_id or generate_instance_id())
    with locked(home):
        host = read_host_config(home)
        server_ip = validate_ipv4(args.server_ip or host["server_ip"])
        start, end = validate_port_range(
            host["port_range"]["start"], host["port_range"]["end"]
        )
        image = args.image or host.get("default_image") or DEFAULT_IMAGE
        instances_doc = load_json(instances_path(home), default_instances())
        ports_doc = load_json(ports_path(home), default_ports())
        instances = instances_doc.setdefault("instances", {})
        ports = ports_doc.setdefault("ports", {})

        if instance_id in instances:
            raise HostManagerError(f"instance already exists: {instance_id}")

        skipped_system_ports: list[int] = []
        if args.port:
            port = validate_port(args.port)
            if not (start <= port <= end):
                raise HostManagerError(f"port {port} is outside host range {start}-{end}")
            if str(port) in ports:
                raise HostManagerError(f"port already allocated: {port}")
            if not host_port_is_available(port):
                raise HostManagerError(f"port already in use on host: {port}")
        else:
            port = 0
            for candidate in range(start, end + 1):
                if str(candidate) in ports:
                    continue
                if not host_port_is_available(candidate):
                    skipped_system_ports.append(candidate)
                    continue
                port = candidate
                break
            if port == 0:
                raise HostManagerError(f"no free port in range {start}-{end}")

        ts = now_iso()
        record = {
            "instance_id": instance_id,
            "status": "creating",
            "server_ip": server_ip,
            "server_port": port,
            "server_url": f"http://{server_ip}:{port}",
            "image": image,
            "instance_home": str(instance_home(home, instance_id)),
            "container_name": container_name(instance_id),
            "compose_project": safe_compose_project(instance_id),
            "port_selection": {
                "skipped_system_ports": skipped_system_ports,
            },
            "created_at": ts,
            "updated_at": ts,
        }
        instances[instance_id] = record
        ports[str(port)] = {
            "instance_id": instance_id,
            "status": "allocated",
            "created_at": ts,
            "updated_at": ts,
        }
        write_json_atomic(ports_path(home), ports_doc)
        write_json_atomic(instances_path(home), instances_doc)
    return {"ok": True, "instance": record}


def get_instance_record(home: Path, instance_id: str) -> dict[str, Any]:
    validate_instance_id(instance_id)
    instances_doc = load_json(instances_path(home), default_instances())
    record = instances_doc.get("instances", {}).get(instance_id)
    if not record:
        raise HostManagerError(f"unknown instance: {instance_id}")
    return record


def mark_instance(args: argparse.Namespace) -> dict[str, Any]:
    home = as_home(args.home)
    instance_id = validate_instance_id(args.instance_id)
    with locked(home):
        instances_doc = load_json(instances_path(home), default_instances())
        instances = instances_doc.setdefault("instances", {})
        record = instances.get(instance_id)
        if not record:
            raise HostManagerError(f"unknown instance: {instance_id}")
        ts = now_iso()
        record["status"] = args.status
        record["updated_at"] = ts
        if args.status == "ready":
            if not args.invitation_code:
                raise HostManagerError("--invitation-code is required when status=ready")
            if not args.invitation_code.startswith("MIRU-"):
                raise HostManagerError("invitation_code must start with MIRU-")
            record["invitation_code"] = args.invitation_code
            record["ready_at"] = ts
            record.pop("last_error", None)
        if args.error:
            record["last_error"] = args.error[:2000]
        instances[instance_id] = record
        write_json_atomic(instances_path(home), instances_doc)
    return {"ok": True, "instance": record}


def remove_instance(args: argparse.Namespace) -> dict[str, Any]:
    home = as_home(args.home)
    instance_id = validate_instance_id(args.instance_id)
    with locked(home):
        instances_doc = load_json(instances_path(home), default_instances())
        ports_doc = load_json(ports_path(home), default_ports())
        instances = instances_doc.setdefault("instances", {})
        ports = ports_doc.setdefault("ports", {})
        record = instances.pop(instance_id, None)
        if not record:
            raise HostManagerError(f"unknown instance: {instance_id}")
        port = str(record.get("server_port", ""))
        if port and ports.get(port, {}).get("instance_id") == instance_id:
            ports.pop(port, None)
        write_json_atomic(instances_path(home), instances_doc)
        write_json_atomic(ports_path(home), ports_doc)
    return {"ok": True, "removed": record}


def get_instance(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, "instance": get_instance_record(as_home(args.home), args.instance_id)}


def list_instances(args: argparse.Namespace) -> dict[str, Any]:
    home = as_home(args.home)
    instances_doc = load_json(instances_path(home), default_instances())
    records = list(instances_doc.get("instances", {}).values())
    records.sort(key=lambda item: (item.get("created_at", ""), item.get("instance_id", "")))
    return {"ok": True, "home": str(home), "count": len(records), "instances": records}


def set_image(args: argparse.Namespace) -> dict[str, Any]:
    home = as_home(args.home)
    instance_id = validate_instance_id(args.instance_id)
    with locked(home):
        instances_doc = load_json(instances_path(home), default_instances())
        record = instances_doc.setdefault("instances", {}).get(instance_id)
        if not record:
            raise HostManagerError(f"unknown instance: {instance_id}")
        record["image"] = args.image
        record["updated_at"] = now_iso()
        write_json_atomic(instances_path(home), instances_doc)
    return {"ok": True, "instance": record}


def audit(args: argparse.Namespace) -> dict[str, Any]:
    home = as_home(args.home)
    issues: list[str] = []
    host = load_json(host_path(home), {})
    instances_doc = load_json(instances_path(home), default_instances())
    ports_doc = load_json(ports_path(home), default_ports())
    instances = instances_doc.get("instances", {})
    ports = ports_doc.get("ports", {})

    if not host:
        issues.append("host.json missing")
    seen_ports: dict[str, str] = {}
    for instance_id, record in instances.items():
        try:
            validate_instance_id(instance_id)
        except HostManagerError as exc:
            issues.append(f"invalid instance id {instance_id}: {exc}")
        port = str(record.get("server_port", ""))
        if not port:
            issues.append(f"{instance_id}: missing server_port")
        elif port in seen_ports:
            issues.append(f"{instance_id}: duplicate port with {seen_ports[port]}")
        else:
            seen_ports[port] = instance_id
        if ports.get(port, {}).get("instance_id") != instance_id:
            issues.append(f"{instance_id}: missing/mismatched ports.json entry for {port}")
        expected_home = str(instance_home(home, instance_id))
        if record.get("instance_home") != expected_home:
            issues.append(f"{instance_id}: instance_home mismatch")
        if record.get("container_name") != container_name(instance_id):
            issues.append(f"{instance_id}: container_name mismatch")
        if record.get("compose_project") != safe_compose_project(instance_id):
            issues.append(f"{instance_id}: compose_project mismatch")

    for port, entry in ports.items():
        linked = entry.get("instance_id")
        if linked not in instances:
            issues.append(f"port {port}: references missing instance {linked}")

    return {"ok": not issues, "home": str(home), "issues": issues}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Miru host instance ledger")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-host")
    p.add_argument("--home")
    p.add_argument("--server-ip", required=True)
    p.add_argument("--port-start", type=int, default=DEFAULT_PORT_START)
    p.add_argument("--port-end", type=int, default=DEFAULT_PORT_END)
    p.add_argument("--default-image", default=DEFAULT_IMAGE)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=init_host)

    p = sub.add_parser("allocate-instance")
    p.add_argument("--home")
    p.add_argument("--instance-id")
    p.add_argument("--server-ip")
    p.add_argument("--port", type=int)
    p.add_argument("--image")
    p.set_defaults(func=allocate_instance)

    p = sub.add_parser("mark-instance")
    p.add_argument("--home")
    p.add_argument("--instance-id", required=True)
    p.add_argument("--status", choices=["creating", "ready", "failed"], required=True)
    p.add_argument("--invitation-code")
    p.add_argument("--error")
    p.set_defaults(func=mark_instance)

    p = sub.add_parser("remove-instance")
    p.add_argument("--home")
    p.add_argument("--instance-id", required=True)
    p.set_defaults(func=remove_instance)

    p = sub.add_parser("get-instance")
    p.add_argument("--home")
    p.add_argument("--instance-id", required=True)
    p.set_defaults(func=get_instance)

    p = sub.add_parser("list-instances")
    p.add_argument("--home")
    p.set_defaults(func=list_instances)

    p = sub.add_parser("set-image")
    p.add_argument("--home")
    p.add_argument("--instance-id", required=True)
    p.add_argument("--image", required=True)
    p.set_defaults(func=set_image)

    p = sub.add_parser("audit")
    p.add_argument("--home")
    p.set_defaults(func=audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except HostManagerError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
