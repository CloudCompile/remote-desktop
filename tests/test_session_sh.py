"""session.sh lifecycle tests (#32, #31): run the watchdog against fake
vncserver/ss/pactl/xrandr binaries in a sandbox PATH — no real X, no host
side effects.

Covers: PID-file ownership (two watchdogs cannot both own a display),
stale-pidfile takeover, start with the configured geometry/DPI, TERM
forwarding that stops Xvnc/PulseAudio and cleans the pid file, --status,
and RD_GEOMETRY / persisted display.json consumption.
"""
import json
import os
import shutil
import signal
import subprocess
import time

import pytest

SESSION_SH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session.sh")


@pytest.fixture
def sandbox(tmp_path):
    """Fake tool sandbox: bin/ with stubs, fake XDG state, marker files."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "listening"          # fake ss: exists == port listening
    calls = tmp_path / "calls.log"           # every fake tool appends its argv

    def stub(name, body):
        p = bin_dir / name
        p.write_text("#!/bin/sh\n"
                     f"echo \"$$(date '+%s') {name} $*\" >> '{calls}'\n" + body)
        os.chmod(p, 0o755)

    stub("ss", f"""if [ -f "{marker}" ]; then echo "LISTEN 0 5 127.0.0.1:5977"; fi
""")
    stub("vncserver", f"""if [ "$1" = "-kill" ]; then rm -f "{marker}"; exit 0; fi
touch "{marker}"
echo "$*" > '{tmp_path}/vnc-args'
exit 0
""")
    stub("pactl", """if [ "$1" = "list" ]; then echo "remote_out\\tdummy"; else exit 0; fi
""")
    stub("pulseaudio", "exit 0\n")
    stub("xrandr", "exit 0\n")

    xdg = tmp_path / "xdg"
    os.makedirs(xdg / "remote-desktop", exist_ok=True)
    rundir = tmp_path / "run"
    rundir.mkdir()
    return {"bin": bin_dir, "marker": marker, "calls": calls, "xdg": xdg,
            "rundir": rundir, "log": tmp_path / "watchdog.log",
            "vncargs": tmp_path / "vnc-args"}


def _start(sb, tmp_path, env_extra=None):
    env = dict(os.environ)
    env["PATH"] = f"{sb['bin']}:{env['PATH']}"
    env["RD_DISPLAY"] = ":77"
    env["RD_SESSION_LOG"] = str(sb["log"])
    env["RD_SESSION_VNC_LOG"] = str(tmp_path / "vnc-session.log")
    env["XDG_DATA_HOME"] = str(sb["xdg"])
    env["XDG_RUNTIME_DIR"] = str(sb["rundir"])
    env.update(env_extra or {})
    return subprocess.Popen(["/bin/sh", SESSION_SH], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_log(sb, needle, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if needle in sb["log"].read_text():
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def test_start_status_and_term_lifecycle(sandbox, tmp_path):
    proc = _start(sandbox, tmp_path)
    try:
        assert _wait_log(sandbox, "event=vnc-start-done"), "watchdog never started Xvnc"
        assert sandbox["marker"].exists()
        args = sandbox["vncargs"].read_text().strip()
        assert "-geometry" in args and "1280x800" in args  # default geometry
        # pid file exists and --status reports the watchdog
        pidfile = sandbox["rundir"] / "remote-session77.pid"
        assert pidfile.exists()
        r = subprocess.run(["/bin/sh", SESSION_SH, "--status"],
                           env={**os.environ, "RD_DISPLAY": ":77",
                                "XDG_RUNTIME_DIR": str(sandbox["rundir"])},
                           capture_output=True, text=True)
        assert r.returncode == 0 and "running" in r.stdout
        # a second watchdog must refuse to take over the same display
        dup = _start(sandbox, tmp_path)
        try:
            dup.wait(timeout=10)
            assert dup.returncode == 0
            assert "event=already-running" in sandbox["log"].read_text()
        finally:
            if dup.poll() is None:
                dup.kill()
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
    assert _wait_log(sandbox, "event=stopped"), "no clean stop logged"
    assert not sandbox["marker"].exists(), "Xvnc not stopped (marker still listening)"
    assert not (sandbox["rundir"] / "remote-session77.pid").exists(), "pid file not cleaned"
    calls = sandbox["calls"].read_text()
    assert "vncserver -kill :77" in calls, "Xvnc was never killed"
    assert "pulseaudio -k" in calls, "PulseAudio was never stopped"


def test_rd_geometry_and_dpi_used(sandbox, tmp_path):
    proc = _start(sandbox, tmp_path, {"RD_GEOMETRY": "1600x900", "RD_DPI": "120"})
    try:
        assert _wait_log(sandbox, "event=vnc-start-done")
        args = sandbox["vncargs"].read_text().strip()
        assert "1600x900" in args
        log = sandbox["log"].read_text()
        assert "geometry=1600x900" in log and "dpi=120" in log
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
    # xrandr --dpi was applied to the fresh session
    assert "xrandr --dpi 120" in sandbox["calls"].read_text()


def test_persisted_display_state_used(sandbox, tmp_path):
    (sandbox["xdg"] / "remote-desktop" / "display.json").write_text(
        json.dumps({"geometry": "1920x1080", "dpi": 144}))
    proc = _start(sandbox, tmp_path)
    try:
        assert _wait_log(sandbox, "event=vnc-start-done")
        assert "1920x1080" in sandbox["vncargs"].read_text()
        assert "dpi=144" in sandbox["log"].read_text()
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)


def test_stale_pidfile_is_taken_over(sandbox, tmp_path):
    pidfile = sandbox["rundir"] / "remote-session77.pid"
    pidfile.write_text("4194300\n")             # assume no such process
    proc = _start(sandbox, tmp_path)
    try:
        assert _wait_log(sandbox, "event=stale-pidfile")
        assert _wait_log(sandbox, "event=vnc-start-done")
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)


def test_status_stopped(sandbox):
    r = subprocess.run(["/bin/sh", SESSION_SH, "--status"],
                       env={**os.environ, "RD_DISPLAY": ":77",
                            "XDG_RUNTIME_DIR": str(sandbox["rundir"])},
                       capture_output=True, text=True)
    assert r.returncode == 1
    assert "stopped" in r.stdout


def test_sh_syntax():
    for script in ("session.sh", "start.sh", "webapps.sh", "tunnel-url.sh"):
        r = subprocess.run(["sh", "-n", os.path.join(os.path.dirname(SESSION_SH), script)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
