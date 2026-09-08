"""Verify that the pet follows the launcher process, not its spawner thread."""
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux parent lifecycle")]


def test_pet_survives_spawner_thread_and_exits_with_parent(tmp_path):
    pytest.importorskip("Xlib")
    root = Path(__file__).resolve().parents[1]
    result_file = tmp_path / "child.json"
    # The child uses the actual watcher, without starting Qt or opening a display.
    child_code = "import os,threading; from linux_pet import _watch_parent; print('ready',flush=True); _watch_parent(int(os.environ['AIRI_PET_PARENT_PID']),threading.Event())"
    parent_code = f"""
import json,os,subprocess,sys,threading,time
from pathlib import Path
def spawn():
    child=subprocess.Popen([sys.executable,'-s','-c',{child_code!r}],stdout=subprocess.PIPE,text=True,
                           env={{**os.environ,'AIRI_PET_PARENT_PID':str(os.getpid())}})
    assert child.stdout.readline().strip()=='ready'
    Path({str(result_file)!r}).write_text(json.dumps({{'pid':child.pid}}))
t=threading.Thread(target=spawn)
t.start(); t.join()
while True: time.sleep(1)
"""
    parent = subprocess.Popen([sys.executable, "-s", "-c", parent_code], cwd=root)
    child_fd = None
    try:
        deadline = time.monotonic() + 10
        while not result_file.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        child_pid = json.loads(result_file.read_text())["pid"]
        child_fd = os.pidfd_open(child_pid)
        time.sleep(.7)  # The creating thread has ended; the process is still alive.
        stat = Path(f"/proc/{child_pid}/stat")
        def alive():
            try:
                return stat.read_text().split()[2] != "Z"
            except FileNotFoundError:
                return False
        assert alive()
        parent.terminate()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while alive() and time.monotonic() < deadline:
            time.sleep(.05)
        assert not alive()
    finally:
        if parent.poll() is None:
            parent.terminate()
            parent.wait(timeout=5)
        if child_fd is not None:
            try:
                signal.pidfd_send_signal(child_fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(child_fd)
