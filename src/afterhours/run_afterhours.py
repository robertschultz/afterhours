"""Run both local services. Starting the app never starts playback or generation."""
import argparse
import importlib.util
import signal
import socket
import subprocess
import sys
import time

from afterhours.native_runtime import StartupError, read_json
from afterhours.paths import data_root

SERVICES = (("music", "afterhours.server", 8765, "/api/config"),
            ("performance", "afterhours.performance_server", 8768, "/api/performance/config"))


def check_installation():
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required.")
    missing = [name for name in ("PIL", "numpy") if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError("Missing Python dependencies. Run ./setup first.")


def check_ports():
    # Check every port before starting either service; never kill an existing app.
    for _, _, port, _ in SERVICES:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"Port {port} is already in use. Stop the existing local service before starting another.") from exc


def stop_children(children):
    for child in children:
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
    for child in children:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def wait_ready(children):
    pending = {port: (name, route) for name, _, port, route in SERVICES}
    deadline = time.monotonic() + 15
    while pending and time.monotonic() < deadline:
        if any(child.poll() is not None for child in children):
            raise RuntimeError("A service stopped during startup. See .runtime/*-service.log.")
        for port, (name, route) in list(pending.items()):
            try:
                if read_json(port, route).get("service") == f"afterhours-{name}":
                    del pending[port]
            except (OSError, ValueError, StartupError):
                pass
        if pending:
            time.sleep(.1)
    if pending:
        raise RuntimeError("The services did not become ready. See .runtime/*-service.log.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check dependencies without opening services or contacting providers")
    args = parser.parse_args()
    check_installation()
    if args.check:
        print("Afterhours dependencies are ready.")
        return 0
    check_ports()
    runtime = data_root() / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    children, logs = [], []
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        for name, module, _, _ in SERVICES:
            log = (runtime / f"{name}-service.log").open("ab")
            logs.append(log)
            children.append(subprocess.Popen([sys.executable, "-u", "-m", module],
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
        wait_ready(children)
        print("Afterhours services are ready.", flush=True)
        print("Ready. Playback, microphone and paid generation remain off. Ctrl+C stops both services.", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(.3)
        raise RuntimeError("A service stopped. See .runtime/*-service.log.")
    except KeyboardInterrupt:
        return 0
    finally:
        stop_children(children)
        for log in logs:
            log.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Afterhours: {exc}", file=sys.stderr)
        sys.exit(1)
