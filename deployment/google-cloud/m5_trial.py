#!/usr/bin/env python3
"""Isolated M5 trial on the existing VM; never touches the live 8765 service.

This operator-only helper requires the already installed OIC runtime and a working
systemd user manager. It intentionally makes no cloud, firewall, DNS or SMTP changes.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from uuid import uuid4


HOME = Path.home()
DEFAULT_APP = HOME / ".local/share/open-inquiry-community-alpha2/app"
DEFAULT_PYTHON = HOME / ".local/share/open-inquiry-community-alpha2/venv/bin/python"
PARENT = HOME / ".local/share/eksmu-m5-acceptance-runs"
ORIGINAL_PORT = 8767
RESTORED_PORT = 8768
MARKER = ".eksmu-m5-acceptance"
STATE = "state.json"


def run(*args, cwd=None, env=None):
    subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True)


def free(port):
    with socket.socket() as sock:
        # Match the server's address reuse setting so completed health probes
        # in TIME_WAIT do not look like an active listener after a trial stops.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))


def health(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for _ in range(30):
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last = exc
        time.sleep(0.5)
    raise RuntimeError(f"Private trial health check on {port} failed: {last}")


def save(directory, state):
    temp = directory / (STATE + ".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, directory / STATE)


def load(directory):
    directory = Path(directory).expanduser()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Trial directory is absent or a symlink")
    directory = directory.resolve()
    if directory.parent != PARENT.resolve() or not directory.name.startswith("trial-"):
        raise ValueError("Refusing a directory outside the dedicated trial parent")
    if (directory / MARKER).read_text(encoding="ascii").strip() != directory.name:
        raise ValueError("Trial marker mismatch")
    state = json.loads((directory / STATE).read_text(encoding="utf-8"))
    if state["run_id"] != directory.name:
        raise ValueError("Trial state mismatch")
    return directory, state


def start_unit(name, app, python, database, port):
    # A user service inherits the user manager's environment. Remove everything
    # beyond the small set needed by this local trial, including any future SMTP
    # configuration, and do not pass the caller's credentials to systemd-run.
    manager = subprocess.run(["systemctl", "--user", "show-environment"],
                             capture_output=True, text=True, check=True)
    allowed = {"HOME", "USER", "LOGNAME", "PATH", "LANG", "XDG_RUNTIME_DIR",
               "DBUS_SESSION_BUS_ADDRESS"}
    remove = sorted({line.partition("=")[0] for line in manager.stdout.splitlines()
                     if line.partition("=")[0] not in allowed})
    clean_env = {key: value for key, value in os.environ.items() if key in allowed}
    properties = ["--property=CPUQuota=50%", "--property=MemoryMax=1G",
                  "--property=Environment=PYTHONDONTWRITEBYTECODE=1"]
    if remove:
        properties.append("--property=UnsetEnvironment=" + " ".join(remove))
    run("systemd-run", "--user", f"--unit={name}",
        f"--working-directory={app}", *properties, python, "-m", "eksmu",
        "--db", database, "serve", "--port", port, env=clean_env)
    health(port)


def stop_unit(name):
    if name:
        before = subprocess.run(["systemctl", "--user", "is-active", name],
                                capture_output=True, text=True, check=False)
        if before.stdout.strip() in ("active", "activating", "reloading"):
            run("systemctl", "--user", "stop", name)
        result = subprocess.run(["systemctl", "--user", "is-active", name],
                                capture_output=True, text=True, check=False)
        if result.stdout.strip() not in ("inactive", "failed", "unknown"):
            raise RuntimeError(f"Service did not stop: {name}: {result.stdout.strip()}")


def start(args):
    app = args.app_root.expanduser().resolve(strict=True)
    # Keep the venv entry point. Resolving its final symlink silently switches
    # execution to the system interpreter even when the venv path exists.
    python = Path(os.path.abspath(args.app_python.expanduser()))
    if not (app / "eksmu/__main__.py").is_file() or not os.access(python, os.X_OK):
        raise ValueError("Installed app or Python interpreter is unavailable")
    free(ORIGINAL_PORT)
    PARENT.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="trial-", dir=PARENT))
    run_id = directory.name
    data_dir = directory / "data"
    data_dir.mkdir(mode=0o700)
    (directory / MARKER).write_text(run_id + "\n", encoding="ascii")
    unit = f"eksmu-m5-{uuid4().hex[:12]}.service"
    state = {"run_id": run_id, "app": str(app), "python": str(python),
             "original_unit": unit, "restored_unit": None, "step": "created"}
    save(directory, state)
    try:
        start_unit(unit, app, python, data_dir / "community.sqlite", ORIGINAL_PORT)
    except Exception:
        stop_unit(unit)
        raise
    state["step"] = "original_running"
    save(directory, state)
    print(f"RUN_DIR={directory}")
    print(f"Private trial: http://127.0.0.1:{ORIGINAL_PORT}/")
    print("Live service at 127.0.0.1:8765 was not modified.")


def restore(args):
    directory, state = load(args.run_dir)
    if state["step"] != "original_running":
        raise ValueError("Trial is not in the original-running stage")
    free(RESTORED_PORT)
    app, python = state["app"], state["python"]
    database = directory / "data/community.sqlite"
    backup, restored = directory / "backup", directory / "restored"
    if backup.exists() or restored.exists():
        raise ValueError("Backup or restored target already exists; preserve evidence and inspect")
    stop_unit(state["original_unit"])
    state["step"] = "original_stopped"
    save(directory, state)
    run(python, "-m", "eksmu", "--db", database, "backup", backup, "--offline", cwd=app)
    run(python, "-m", "eksmu", "verify-backup", backup, cwd=app)
    run(python, "-m", "eksmu", "restore", backup, "--into", restored, cwd=app)
    # The source is this stopped, most recent trial DB; restoration revokes sessions.
    run(python, "-m", "eksmu", "--db", restored / "community.sqlite",
        "reconcile-accounts", "--from-db", database, "--offline", cwd=app)
    unit = f"eksmu-m5-restored-{uuid4().hex[:12]}.service"
    state["restored_unit"] = unit
    state["step"] = "restored_prepared"
    save(directory, state)
    try:
        start_unit(unit, app, python, restored / "community.sqlite", RESTORED_PORT)
    except Exception:
        stop_unit(unit)
        raise
    state["step"] = "restored_running"
    save(directory, state)
    print(f"RUN_DIR={directory}")
    print(f"Restored private trial: http://127.0.0.1:{RESTORED_PORT}/")
    print("All old sessions were revoked; log in with the same test email and password.")


def stop(args):
    directory, state = load(args.run_dir)
    stop_unit(state["original_unit"])
    stop_unit(state["restored_unit"])
    state["step"] = "stopped"
    save(directory, state)
    print(f"Stopped trial units. Preserved isolated data at {directory}.")


def cleanup(args):
    directory, state = load(args.run_dir)
    if args.confirm != state["run_id"]:
        raise ValueError("Explicit --confirm must match the trial run directory name")
    stop_unit(state["original_unit"])
    stop_unit(state["restored_unit"])
    shutil.rmtree(directory)
    print(f"Removed only the confirmed trial directory: {directory}")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initial = commands.add_parser("start")
    initial.add_argument("--app-root", type=Path, default=DEFAULT_APP)
    initial.add_argument("--app-python", type=Path, default=DEFAULT_PYTHON)
    for command in ("restore", "stop", "cleanup"):
        sub = commands.add_parser(command)
        sub.add_argument("run_dir", type=Path)
        if command == "cleanup":
            sub.add_argument("--confirm", required=True)
    args = parser.parse_args()
    try:
        {"start": start, "restore": restore, "stop": stop, "cleanup": cleanup}[args.command](args)
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Trial operation failed: {exc}\n")


if __name__ == "__main__":
    main()
