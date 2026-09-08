"""Linux desktop entry point; shares the existing desktop lifecycle/bridge."""
import os
import sys


def x11_session_available():
    session = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    return bool(os.environ.get("DISPLAY")) and session != "wayland" and (
        session == "x11" or not os.environ.get("WAYLAND_DISPLAY"))


def main():
    if not sys.platform.startswith("linux"):
        raise RuntimeError("linux_launcher.py must run on Linux")
    if not os.environ.get("DISPLAY"):
        print("Start Miru inside your Ubuntu X11 desktop session.", file=sys.stderr)
        return 1
    if not x11_session_available():
        print("This preview requires Ubuntu on Xorg (Wayland capture is not implemented).", file=sys.stderr)
        return 1
    from windows_launcher import main as desktop_main
    os.environ["MIRU_DESKTOP_PLATFORM"] = "linux"
    return desktop_main(platform_name="linux")


if __name__ == "__main__":
    raise SystemExit(main())
