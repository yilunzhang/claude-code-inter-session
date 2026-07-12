"""Server election + detached spawn. Unix-only for v1."""

from __future__ import annotations

import errno
import fcntl
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from bin import shared

_SERVER_PATH = Path(__file__).parent / "server.py"


def _acquire_election_lock(host: str, port: int, timeout: float = 8.0):
    """Serialize the server election. Returns an fd holding an exclusive flock
    on the endpoint's election-lock file, or None if another elector already
    brought the server up (or held the lock past `timeout`). The caller must
    close the fd (which releases the lock) once its bind+spawn is done.

    Non-blocking + polling rather than a blocking flock so we can short-circuit
    the moment a peer's server appears, and never wedge on a stuck holder.
    """
    shared.secure_dir(shared.data_dir())
    path = shared.election_lock_path(host=host, port=port)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_server_up(host, port):
            os.close(fd)
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                os.close(fd)
                raise
            time.sleep(0.05)
    os.close(fd)
    return None


def is_server_up(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_server(host: str, port: int, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if is_server_up(host, port):
            return True
        time.sleep(0.05)
    return False


def ensure_server_running(
    port: int = shared.DEFAULT_PORT,
    host: str = "127.0.0.1",
    idle_shutdown_minutes: float = 10,
    server_path: Path = _SERVER_PATH,
    python: str = sys.executable,
) -> bool:
    """Ensure a server is listening on (host, port).

    Race-safe via a per-endpoint election flock: concurrent callers are
    serialized so exactly one binds + spawns the server while the rest wait for
    it to appear (see _acquire_election_lock). Returns True if a server is up
    after this call — preexisting, spawned by us, or spawned by the peer that
    won the election.
    """
    import logging
    log = logging.getLogger("inter-session.spawn")
    if is_server_up(host, port):
        log.info("ensure: already up")
        return True

    # Serialize the election: SO_REUSEADDR lets two peers both bind() this port
    # before either listens, so without this lock both would spawn a server and
    # the loser's cleanup would wipe the winner's identity. Only the lock holder
    # binds + spawns; everyone else waits for that server to appear.
    lock_fd = _acquire_election_lock(host, port)
    if lock_fd is None:
        log.info("ensure: another elector active; waiting for server")
        return wait_for_server(host, port, timeout=5.0)

    try:
        # A peer may have finished starting the server between our is_server_up
        # check inside the lock acquire and now — re-check before binding.
        if is_server_up(host, port):
            log.info("ensure: server appeared while acquiring lock")
            return True

        log.info("ensure: won election; attempting bind")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR=1: allow rebind after a previous server crashed (its
        # connections may sit in TIME_WAIT). Safe now that the flock guarantees
        # we're the only binder.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as e:
            s.close()
            log.info("ensure: bind failed errno=%s", e.errno)
            if e.errno in (errno.EADDRINUSE, errno.EACCES):
                return wait_for_server(host, port, timeout=2.0)
            raise

        log.info("ensure: bind succeeded; spawning server")
        shared.ensure_token(shared.token_path())

        os.set_inheritable(s.fileno(), True)
        log_path = shared.server_log_path()
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            proc = subprocess.Popen(
                [
                    python,
                    str(server_path),
                    "--fd", str(s.fileno()),
                    "--host", str(host),
                    "--port", str(port),
                    "--idle-shutdown-minutes", str(idle_shutdown_minutes),
                ],
                pass_fds=(s.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,
                close_fds=True,
            )
            log.info("ensure: spawned server pid=%s", proc.pid)
        finally:
            os.close(log_fd)
        s.close()
        ready = wait_for_server(host, port, timeout=5.0)
        log.info("ensure: wait_for_server returned %s", ready)
        return ready
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
