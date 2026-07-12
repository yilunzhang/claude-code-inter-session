"""End-to-end helper CLI tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from bin import shared

REPO = Path(__file__).resolve().parent.parent
BIN_DIR = REPO / "skills" / "inter-session" / "bin"


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    d = tmp_path / "inter-session"
    monkeypatch.setenv("INTER_SESSION_DATA_DIR", str(d))
    return d


@pytest.fixture
def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn_listener(port, name, env_data_dir, ppid_override):
    env = os.environ.copy()
    env["INTER_SESSION_DATA_DIR"] = str(env_data_dir)
    env["PYTHONPATH"] = str(REPO)
    env["INTER_SESSION_PPID_OVERRIDE"] = str(ppid_override)
    return subprocess.Popen(
        [sys.executable, str(BIN_DIR / "client.py"),
         "--port", str(port), "--name", name, "--idle-shutdown-minutes", "1"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )


def _run_helper(script: str, env_data_dir, ppid_override, *args, timeout=10.0):
    env = os.environ.copy()
    env["INTER_SESSION_DATA_DIR"] = str(env_data_dir)
    env["PYTHONPATH"] = str(REPO)
    env["INTER_SESSION_PPID_OVERRIDE"] = str(ppid_override)
    return subprocess.run(
        [sys.executable, str(BIN_DIR / script), *args],
        env=env, capture_output=True, text=True, timeout=timeout,
    )


def _wait_for_state(env_data_dir, ppid, timeout=5.0):
    """Wait for the listener state file at <ppid>.session to appear and be valid."""
    end = time.time() + timeout
    path = env_data_dir / "clients" / f"{ppid}.session"
    while time.time() < end:
        if path.exists():
            try:
                state = json.loads(path.read_text())
                if state.get("session_id"):
                    return state
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.1)
    return None


def _kill_server():
    """Kill any server.py spawned during the test. Handles endpoint-scoped
    pidfile names (server.<port>.pid) by globbing the data dir."""
    data_dir = shared.data_dir()
    if not data_dir.exists():
        return
    for pid_path in data_dir.glob("server.*.pid"):
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 9)
        except (OSError, ValueError):
            pass


@pytest.mark.slow
class TestSendHelper:
    def test_send_routes_via_control(self, tmp_data_dir, free_port):
        ppid_a = 20001
        ppid_b = 20002
        listener_a = _spawn_listener(free_port, "alpha", tmp_data_dir, ppid_a)
        listener_b = _spawn_listener(free_port, "beta", tmp_data_dir, ppid_b)
        try:
            _wait_for_state(tmp_data_dir, ppid_a)
            _wait_for_state(tmp_data_dir, ppid_b)
            time.sleep(0.5)
            r = _run_helper("send.py", tmp_data_dir, ppid_a,
                            "--to", "beta", "--text", "hi from alpha")
            assert r.returncode == 0, f"stderr={r.stderr!r}"
            time.sleep(0.5)
            output = listener_b.stdout.readline()
            assert "hi from alpha" in output
            # `from` should be alpha (the listener for ppid_a), not the control's session_id
            assert 'from="alpha"' in output
        finally:
            for p in (listener_a, listener_b):
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
            _kill_server()

    def test_send_unknown_peer(self, tmp_data_dir, free_port):
        ppid = 20003
        listener = _spawn_listener(free_port, "alpha", tmp_data_dir, ppid)
        try:
            _wait_for_state(tmp_data_dir, ppid)
            time.sleep(0.5)
            r = _run_helper("send.py", tmp_data_dir, ppid,
                            "--to", "nobody", "--text", "hi")
            assert r.returncode == 1
            assert "unknown_peer" in r.stderr or "no agent" in r.stderr
        finally:
            listener.terminate()
            try:
                listener.wait(timeout=2)
            except subprocess.TimeoutExpired:
                listener.kill()
            _kill_server()

    def test_send_no_listener_fails(self, tmp_data_dir, free_port):
        # No listener spawned. Helper should fail fast.
        r = _run_helper("send.py", tmp_data_dir, 99999,
                        "--to", "alpha", "--text", "hi")
        assert r.returncode == 1
        assert "not connected" in r.stderr

    def test_broadcast(self, tmp_data_dir, free_port):
        ppid_a, ppid_b = 20011, 20012
        listener_a = _spawn_listener(free_port, "alpha", tmp_data_dir, ppid_a)
        listener_b = _spawn_listener(free_port, "beta", tmp_data_dir, ppid_b)
        try:
            _wait_for_state(tmp_data_dir, ppid_a)
            _wait_for_state(tmp_data_dir, ppid_b)
            time.sleep(0.5)
            r = _run_helper("send.py", tmp_data_dir, ppid_a,
                            "--all", "--text", "hello everyone")
            assert r.returncode == 0
            time.sleep(0.5)
            # Reading non-blocking: the line should be in stdout buffer.
            line_b = listener_b.stdout.readline()
            assert "hello everyone" in line_b
            assert 'from="alpha"' in line_b
        finally:
            for p in (listener_a, listener_b):
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
            _kill_server()


@pytest.mark.slow
class TestListHelper:
    def test_list_shows_all_agents(self, tmp_data_dir, free_port):
        ppid_a, ppid_b = 20021, 20022
        listener_a = _spawn_listener(free_port, "alpha", tmp_data_dir, ppid_a)
        listener_b = _spawn_listener(free_port, "beta", tmp_data_dir, ppid_b)
        try:
            _wait_for_state(tmp_data_dir, ppid_a)
            _wait_for_state(tmp_data_dir, ppid_b)
            time.sleep(0.5)
            r = _run_helper("list.py", tmp_data_dir, ppid_a)
            assert r.returncode == 0, f"stderr={r.stderr!r}"
            assert "alpha" in r.stdout
            assert "beta" in r.stdout
        finally:
            for p in (listener_a, listener_b):
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
            _kill_server()

    def test_list_self(self, tmp_data_dir, free_port):
        ppid = 20031
        listener = _spawn_listener(free_port, "alpha", tmp_data_dir, ppid)
        try:
            _wait_for_state(tmp_data_dir, ppid)
            r = _run_helper("list.py", tmp_data_dir, ppid, "--self")
            assert r.returncode == 0
            assert "alpha" in r.stdout
        finally:
            listener.terminate()
            try:
                listener.wait(timeout=2)
            except subprocess.TimeoutExpired:
                listener.kill()
            _kill_server()


@pytest.mark.slow
class TestElection:
    def test_concurrent_ensure_spawns_single_server(self, tmp_data_dir, free_port):
        """Race regression: many peers calling ensure_server_running at once
        must spawn exactly ONE server (not several that clobber each other's
        identity). Before the election lock, SO_REUSEADDR let two bind() the
        port before either listened, so both spawned and identity got wiped."""
        import threading
        from bin import spawn

        n = 6
        results = []
        barrier = threading.Barrier(n)

        def worker():
            barrier.wait()  # release all threads together to maximize the race
            results.append(spawn.ensure_server_running(
                port=free_port, idle_shutdown_minutes=1))

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        try:
            assert all(results), f"not all callers saw a server up: {results}"
            pidfiles = list(tmp_data_dir.glob("server.*.pid"))
            assert len(pidfiles) == 1, f"expected exactly one server, got {pidfiles}"
            # And it's a real, identity-verified server (not a wiped pidfile).
            assert shared.verify_server_identity(port=free_port)
        finally:
            _kill_server()


@pytest.mark.slow
class TestStaleStateCleanup:
    """Regression: helpers used to surface raw `hello error: unknown_peer`
    when the listener referenced by the state file was dead. Now they
    print the friendly 'not connected' message AND delete the stale file."""

    def test_send_handles_stale_state(self, tmp_data_dir, free_port):
        ppid = 60001
        listener = _spawn_listener(free_port, "alpha", tmp_data_dir, ppid)
        state = _wait_for_state(tmp_data_dir, ppid)
        assert state is not None
        # SIGKILL bypasses atexit so state+lock files remain on disk;
        # SIGKILL also releases the flock so unlink_if_matches' flock check
        # succeeds. Then tamper state to simulate a stale entry the server no
        # longer recognizes.
        listener.kill()
        listener.wait(timeout=2)
        time.sleep(0.5)  # let server detect TCP close + unregister
        state_path = tmp_data_dir / "clients" / f"{ppid}.session"
        stale = dict(state)
        stale["session_id"] = "00000000-0000-0000-0000-000000000000"
        stale["nonce"] = "stale-nonce"
        state_path.write_text(json.dumps(stale) + "\n")
        try:
            r = _run_helper("send.py", tmp_data_dir, ppid,
                            "--to", "anyone", "--text", "hi")
            assert r.returncode == 1
            assert "not connected" in r.stderr, f"stderr={r.stderr!r}"
            assert not state_path.exists(), "stale state file should have been deleted"
        finally:
            _kill_server()

    def test_list_handles_stale_state(self, tmp_data_dir, free_port):
        ppid = 60002
        listener = _spawn_listener(free_port, "alpha", tmp_data_dir, ppid)
        state = _wait_for_state(tmp_data_dir, ppid)
        assert state is not None
        listener.kill()
        listener.wait(timeout=2)
        time.sleep(0.5)
        state_path = tmp_data_dir / "clients" / f"{ppid}.session"
        stale = dict(state)
        stale["session_id"] = "00000000-0000-0000-0000-000000000000"
        state_path.write_text(json.dumps(stale) + "\n")
        try:
            r = _run_helper("list.py", tmp_data_dir, ppid)
            assert r.returncode == 1
            assert "not connected" in r.stderr
            assert not state_path.exists()
        finally:
            _kill_server()

    def test_helper_does_not_unlink_when_listener_alive(self, tmp_data_dir, free_port):
        """New: with a live listener (lock held), even a tampered state file
        is preserved. The listener is the authority over its state file."""
        ppid = 60003
        listener = _spawn_listener(free_port, "alpha", tmp_data_dir, ppid)
        try:
            state = _wait_for_state(tmp_data_dir, ppid)
            assert state is not None
            state_path = tmp_data_dir / "clients" / f"{ppid}.session"
            stale = dict(state)
            stale["session_id"] = "00000000-0000-0000-0000-000000000000"
            state_path.write_text(json.dumps(stale) + "\n")
            r = _run_helper("send.py", tmp_data_dir, ppid,
                            "--to", "anyone", "--text", "hi")
            assert r.returncode == 1
            # State file MUST remain — the listener is alive and the lock is held
            assert state_path.exists(), "must not unlink while listener lock is held"
        finally:
            listener.terminate()
            try:
                listener.wait(timeout=2)
            except subprocess.TimeoutExpired:
                listener.kill()
            _kill_server()


@pytest.mark.slow
class TestStateFileLifecycle:
    """Regression: client.py should delete its state file on graceful exit
    (SIGTERM) so the next /inter-session sees a clean slate."""

    def test_state_deleted_on_sigterm(self, tmp_data_dir, free_port):
        ppid = 60003
        listener = _spawn_listener(free_port, "alpha", tmp_data_dir, ppid)
        try:
            state = _wait_for_state(tmp_data_dir, ppid)
            assert state is not None, "listener never wrote state"
            state_path = tmp_data_dir / "clients" / f"{ppid}.session"
            assert state_path.exists()
            # Graceful shutdown
            listener.terminate()
            try:
                listener.wait(timeout=4)
            except subprocess.TimeoutExpired:
                listener.kill()
                pytest.fail("listener did not exit cleanly on SIGTERM")
            # State file should be gone
            assert not state_path.exists(), "state file remained after SIGTERM"
        finally:
            _kill_server()


class TestUnlinkIfMatches:
    """Regression: send.py / list.py used to unconditionally unlink the
    state file on unknown_peer. If a fresh listener wrote new state between
    our read and unlink, we would delete the legitimate fresh file. Fix:
    only unlink when current contents match the stale state we read."""

    def test_unlinks_when_matches(self, tmp_data_dir):
        from bin import discover, shared
        shared.secure_dir(shared.clients_dir())
        path = shared.client_session_path(81001)
        state = {"session_id": "AAA", "nonce": "111", "name": "alpha"}
        path.write_text(json.dumps(state))
        assert discover.unlink_if_matches(path, state)
        assert not path.exists()

    def test_does_not_unlink_when_session_id_changed(self, tmp_data_dir):
        from bin import discover, shared
        shared.secure_dir(shared.clients_dir())
        path = shared.client_session_path(81002)
        old = {"session_id": "AAA", "nonce": "111", "name": "alpha"}
        new = {"session_id": "BBB", "nonce": "222", "name": "alpha"}
        path.write_text(json.dumps(new))  # fresh listener wrote new state
        assert not discover.unlink_if_matches(path, old)
        assert path.exists()  # not deleted

    def test_does_not_unlink_when_nonce_changed(self, tmp_data_dir):
        from bin import discover, shared
        shared.secure_dir(shared.clients_dir())
        path = shared.client_session_path(81003)
        old = {"session_id": "AAA", "nonce": "111", "name": "alpha"}
        new_nonce = {"session_id": "AAA", "nonce": "222", "name": "alpha"}
        path.write_text(json.dumps(new_nonce))
        assert not discover.unlink_if_matches(path, old)
        assert path.exists()

    def test_handles_missing_path(self):
        from bin import discover
        assert not discover.unlink_if_matches(None, {"session_id": "x"})

    def test_handles_corrupted_file(self, tmp_data_dir):
        from bin import discover, shared
        shared.secure_dir(shared.clients_dir())
        path = shared.client_session_path(81004)
        path.write_text("not json")
        assert not discover.unlink_if_matches(path, {"session_id": "AAA"})

    def test_refuses_when_listener_lock_held(self, tmp_data_dir):
        """Round-5 fix: even if state matches, don't unlink while a live
        listener holds <ppid>.lock — that means the matching state is fresh
        and the listener will rely on it."""
        import fcntl
        from bin import discover, shared
        shared.secure_dir(shared.clients_dir())
        ppid = 81005
        sess = shared.client_session_path(ppid)
        lock = shared.client_lock_path(ppid)
        state = {"session_id": "AAA", "nonce": "111"}
        sess.write_text(json.dumps(state))
        # Hold the lock as if a live listener
        fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert not discover.unlink_if_matches(sess, state), \
                "should refuse to unlink while listener lock is held"
            assert sess.exists()
        finally:
            os.close(fd)

    def test_unlinks_when_listener_lock_released(self, tmp_data_dir):
        """After a SIGKILL'd listener, the lock file remains but the kernel
        released the flock. unlink_if_matches should succeed."""
        from bin import discover, shared
        shared.secure_dir(shared.clients_dir())
        ppid = 81006
        sess = shared.client_session_path(ppid)
        lock = shared.client_lock_path(ppid)
        state = {"session_id": "AAA", "nonce": "111"}
        sess.write_text(json.dumps(state))
        # Lock file exists but no one holds it (simulates dead listener)
        lock.write_text("")
        assert discover.unlink_if_matches(sess, state)
        assert not sess.exists()


class TestDiscover:
    def test_returns_state_via_override(self, tmp_data_dir, monkeypatch):
        from bin import discover
        ppid = 30001
        clients_dir = tmp_data_dir / "clients"
        clients_dir.mkdir(parents=True)
        state = {
            "session_id": str(uuid.uuid4()),
            "name": "alpha",
            "label": "",
            "token": "abc",
            "nonce": "xyz",
            "listener_pid": 12345,
            "port": 9999,
        }
        (clients_dir / f"{ppid}.session").write_text(json.dumps(state))
        monkeypatch.setenv("INTER_SESSION_PPID_OVERRIDE", str(ppid))
        out = discover.find_listener_state()
        assert out is not None
        assert out["name"] == "alpha"
        assert out["nonce"] == "xyz"

    def test_returns_none_without_state(self, tmp_data_dir, monkeypatch):
        from bin import discover
        monkeypatch.setenv("INTER_SESSION_PPID_OVERRIDE", "99999")
        out = discover.find_listener_state()
        assert out is None
