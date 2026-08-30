"""SSE (Server-Sent Events) broadcast module.

Manages connected clients and broadcasts events to all of them.
Used by core.py to push chat messages, typing status, and proactive
messages to all connected devices in real time.

Multi-tenant: each client is tagged with user_id, broadcasts are scoped.
"""

import json
import queue
import threading
import time

_clients: list[dict] = []  # [{queue, device_id, user_id, connected_at}]
_lock = threading.Lock()


def _push(event_type: str, data: dict, user_id: str | None):
    """Internal: format + enqueue for clients matching user_id (or all if None)."""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    message = f"event: {event_type}\ndata: {payload}\n\n"
    dead = []
    with _lock:
        for i, client in enumerate(_clients):
            if user_id is not None and client.get("user_id") != user_id:
                continue
            try:
                client["queue"].put_nowait(message)
            except queue.Full:
                dead.append(i)
        for i in reversed(dead):
            _clients.pop(i)


def broadcast(event_type: str, data: dict, *, user_id: str):
    """Push a per-user event to that user's connected SSE clients.

    user_id is keyword-only and required. For events that target ALL users
    (e.g. admin model switch), use broadcast_all() instead.
    """
    if not user_id:
        raise ValueError("sse.broadcast: user_id is required and must be non-empty. "
                         "Use sse.broadcast_all() for global events.")
    _push(event_type, data, user_id)


def broadcast_all(event_type: str, data: dict):
    """Push a global event to ALL connected SSE clients regardless of user.

    Use sparingly — only for admin/server-wide events (e.g. model swap).
    """
    _push(event_type, data, None)


def add_client(device_id: str = "unknown", user_id: str = "_admin") -> queue.Queue:
    """Register a new SSE client. Returns its message queue."""
    q = queue.Queue(maxsize=100)
    client = {
        "queue": q,
        "device_id": device_id,
        "user_id": user_id,
        "connected_at": time.time(),
    }
    with _lock:
        _clients.append(client)
    print(f"[SSE] Client connected: {device_id} user={user_id} (total: {len(_clients)})")
    return q


def remove_client(q: queue.Queue):
    """Remove a disconnected client by its queue reference."""
    with _lock:
        _clients[:] = [c for c in _clients if c["queue"] is not q]
    print(f"[SSE] Client disconnected (total: {len(_clients)})")


def disconnect_user(user_id: str) -> int:
    """Forcibly close every SSE stream belonging to user_id.

    Used by admin suspension and user self-deletion: after the user's status
    flips to inactive, any still-connected SSE clients should be terminated
    so they get an immediate 401 on reconnect (instead of lingering on a
    quietly orphaned stream until the next heartbeat or write attempt).

    Returns the number of streams closed.
    """
    if not user_id:
        return 0
    closed = 0
    with _lock:
        survivors = []
        for c in _clients:
            if c.get("user_id") == user_id:
                # Push a sentinel-style close event then poison the queue.
                # stream_generator's GeneratorExit handler will dequeue
                # and detect the closed marker, then unwind cleanly.
                try:
                    c["queue"].put_nowait("event: force_disconnect\ndata: {}\n\n")
                except queue.Full:
                    pass
                # Empty the queue and signal end-of-stream by raising on next get
                try:
                    while True:
                        c["queue"].get_nowait()
                except queue.Empty:
                    pass
                # Put a None sentinel — generator will exit on next consume
                try:
                    c["queue"].put_nowait(None)
                except queue.Full:
                    pass
                closed += 1
            else:
                survivors.append(c)
        _clients[:] = survivors
    if closed:
        print(f"[SSE] disconnect_user({user_id}): closed {closed} stream(s)")
    return closed


def get_client_count(user_id: str | None = None) -> int:
    """Return number of connected SSE clients, optionally filtered by user."""
    with _lock:
        if user_id:
            return sum(1 for c in _clients if c.get("user_id") == user_id)
        return len(_clients)


def stream_generator(q: queue.Queue):
    """Flask Response generator for SSE streaming.

    Yields SSE-formatted messages from the queue.
    Sends a heartbeat comment every 30 seconds to keep the connection alive.
    """
    # Initial connection confirmation
    yield "event: connected\ndata: {}\n\n"
    try:
        while True:
            try:
                msg = q.get(timeout=30)
                # None sentinel = forcible disconnect (admin suspend / self-delete)
                if msg is None:
                    return
                yield msg
            except queue.Empty:
                # Heartbeat to keep connection alive
                yield ": heartbeat\n\n"
    except GeneratorExit:
        pass
    finally:
        remove_client(q)
