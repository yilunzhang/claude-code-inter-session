"""Long-lived per-session client. Stdout = notifications to Claude."""

from __future__ import annotations

# Bootstrap: re-exec under the project's isolated venv if it exists,
# so we use isolated deps (websockets, psutil) rather than the user's
# system/user-level Python. Tests opt out via INTER_SESSION_NO_REEXEC=1.
import os
import sys
from pathlib import Path
_VENV = Path.home() / ".claude" / "data" / "inter-session" / "venv"
_VENV_PY = _VENV / "bin" / "python"
if (not os.environ.get("INTER_SESSION_NO_REEXEC")
        and _VENV_PY.is_file()
        and Path(sys.prefix).resolve() != _VENV.resolve()):
    os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])

import argparse
import asyncio
import atexit
import errno
import fcntl
import json
import logging
import random
import secrets
import signal
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import websockets

# Allow running as a script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import shared, spawn, profile

log = logging.getLogger("inter-session.client")


def _print_line(line: str) -> None:
    """Emit a single notification line to stdout, flushed."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _format_msg(msg: dict) -> str:
    sanitized = shared.sanitize_for_stdout(msg.get("text", ""))
    truncated, was_truncated, full_len = shared.truncate_for_stdout(sanitized)
    from_name = msg.get("from_name") or msg.get("from", "?")[:8]
    from_label = msg.get("from_label", "")
    msg_id = msg.get("msg_id", "")
    label_part = f' "{from_label}"' if from_label else ""
    if was_truncated:
        prefix = f'[inter-session msg={msg_id} from="{from_name}"{label_part} truncated={full_len}]'
    else:
        prefix = f'[inter-session msg={msg_id} from="{from_name}"{label_part}]'
    return f"{prefix} {truncated}"


def _format_truncation_pointer(msg_id: str, full_len: int) -> str:
    log_path = shared.messages_log_path()
    return f"[inter-session msg={msg_id} cont] full text {full_len} bytes at {log_path}"


def _write_session_state(ppid: int, state: dict) -> None:
    """Atomically write the listener state file (0600), so helpers never
    observe a partially-written file."""
    shared.secure_dir(shared.clients_dir())
    shared.atomic_write_text(shared.client_session_path(ppid), json.dumps(state) + "\n")


def _delete_session_state(ppid: int) -> None:
    """Best-effort cleanup of `.session` on graceful exit. SIGKILL bypasses
    atexit; helpers handle that case via the `unknown_peer` path.

    Note: the `.lock` file is intentionally NOT unlinked. flock is fd-scoped
    and the kernel releases it when the process dies; the file can stay on
    disk indefinitely. Unlinking it would create a TOCTOU window where a
    new client's flock applies to a different inode than another concurrent
    actor's flock attempt — which can lead to two clients both holding "the
    lock" on different inodes for the same path.
    """
    try:
        shared.client_session_path(ppid).unlink()
    except OSError:
        pass


def _resolve_ppid() -> int:
    """Resolve the state-file key (also the dedup-lock key) for this listener.

    Delegates to shared.resolve_listener_key, which prefers the CC main process
    pid in real Claude Code so helper CLIs (run from a *different* Bash
    subshell than the monitor) can find the same key.
    """
    return shared.resolve_listener_key()


def _acquire_ppid_lock(ppid: int) -> Optional[int]:
    """Return an open fd holding an exclusive lock for this ppid, or None if already held."""
    shared.secure_dir(shared.clients_dir())
    lock_path = shared.client_lock_path(ppid)
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EAGAIN, errno.EACCES):
            return None
        raise


def _read_existing_session_state(ppid: int) -> Optional[dict]:
    """Read the existing listener's session-state file. Used when our
    flock acquire fails so the caller can embed the existing
    connection's identity (name, listener_pid, session_id) in the
    'already running' error message — saves a follow-up `list.py --self`
    round-trip in the skill's connect flow."""
    try:
        path = shared.client_session_path(ppid)
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


class Client:
    def __init__(
        self,
        port: int = shared.DEFAULT_PORT,
        host: str = "127.0.0.1",
        name: str = "",
        label: str = "",
        idle_shutdown_minutes: float = 10,
        ppid: Optional[int] = None,
        verbose: bool = False,
        max_collision_retries: int = 3,
    ):
        self.port = port
        self.host = host
        self.name = name
        self.label = label
        self.idle_shutdown_minutes = idle_shutdown_minutes
        self.ppid = ppid if ppid is not None else _resolve_ppid()
        self.verbose = verbose
        self.session_id = str(uuid.uuid4())
        self.nonce = secrets.token_urlsafe(16)
        self._stop = asyncio.Event()
        self._lock_fd: Optional[int] = None
        self._max_collision_retries = max_collision_retries
        self._collision_retries = 0
        self._connect_task: Optional[asyncio.Task] = None

    def stop(self) -> None:
        self._stop.set()
        t = self._connect_task
        if t is not None and not t.done():
            t.cancel()

    async def run(self) -> int:
        # Per-CC-session dedup lock (skill mode). Plugin mode uses monitor name dedup.
        self._lock_fd = _acquire_ppid_lock(self.ppid)
        if self._lock_fd is None:
            info = _read_existing_session_state(self.ppid)
            if info:
                _print_line(
                    "[inter-session] another monitor for this session is already running "
                    f"— name={info.get('name', '')!r}, "
                    f"listener_pid={info.get('listener_pid', '')}, "
                    f"session_id={info.get('session_id', '')}; exiting"
                )
            else:
                _print_line("[inter-session] another monitor for this session is already running — exiting")
            return 0

        # Best-effort: delete state file on graceful exit so helpers don't
        # see a stale entry pointing at our dead session_id.
        atexit.register(_delete_session_state, self.ppid)

        try:
            backoff = shared.RECONNECT_BACKOFF_MIN_S
            while not self._stop.is_set():
                spawn.ensure_server_running(self.port, self.host, self.idle_shutdown_minutes)
                self._connect_task = asyncio.create_task(self._connect_and_serve())
                try:
                    await self._connect_task
                    backoff = shared.RECONNECT_BACKOFF_MIN_S  # successful disconnect; reset
                except asyncio.CancelledError:
                    # stop() cancels the active connect task to break out of
                    # blocking recv loops on SIGTERM/SIGINT.
                    pass
                except (ConnectionRefusedError, OSError) as e:
                    if self.verbose:
                        log.info("connect failed: %s", e)
                except websockets.InvalidHandshake as e:
                    _print_line(f"[inter-session] connected to a non-inter-session service on port {self.port}: {e}")
                    return 1
                except websockets.ConnectionClosed:
                    pass
                finally:
                    self._connect_task = None
                if self._stop.is_set():
                    break
                jitter = backoff * shared.RECONNECT_JITTER_FRAC
                delay = backoff + random.uniform(-jitter, jitter)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, shared.RECONNECT_BACKOFF_MAX_S)
            return 0
        finally:
            if self._lock_fd is not None:
                try:
                    os.close(self._lock_fd)
                except OSError:
                    pass

    async def _connect_and_serve(self) -> None:
        # Defense-in-depth against port squatting: refuse to send the bearer
        # token to a process that doesn't claim to be our server.
        if not shared.verify_server_identity(self.host, self.port):
            _print_line(
                "[inter-session] server identity check failed "
                f"(port {self.port} is held by something that isn't bin/server.py); "
                "refusing to connect"
            )
            self._stop.set()
            return
        token = shared.ensure_token(shared.token_path())
        async with websockets.connect(
            f"ws://{self.host}:{self.port}/",
            max_size=shared.WS_FRAME_CAP,
        ) as ws:
            await ws.send(json.dumps({
                "op": "hello",
                "session_id": self.session_id,
                "name": self.name,
                "label": self.label,
                "cwd": os.getcwd(),
                "pid": os.getpid(),
                "role": shared.Role.AGENT.value,
                "token": token,
                "nonce": self.nonce,
            }))
            welcome_raw = await ws.recv()
            welcome = json.loads(welcome_raw)
            if welcome.get("op") == "error":
                code = welcome.get("code", "")
                if code == shared.ErrorCode.NAME_TAKEN:
                    candidates = welcome.get("candidates", [])
                    if (candidates and
                            self._collision_retries < self._max_collision_retries):
                        # Auto-retry with server's first suggestion. Common case:
                        # two CC sessions start in the same cwd, propose the same
                        # name, second retries to <name>-2 transparently.
                        new_name = candidates[0]
                        old_name = self.name
                        self.name = new_name
                        self._collision_retries += 1
                        _print_line(
                            f"[inter-session] name {old_name!r} taken; "
                            f"using {new_name!r}"
                        )
                        return  # main loop will reconnect with self.name = new_name
                    # Out of retries — surface and stop. Caller picks a new name.
                    _print_line(
                        f"[inter-session] name {self.name!r} taken after "
                        f"{self._collision_retries} retries; "
                        f"run /inter-session connect <other-name>"
                    )
                    self._stop.set()
                    return
                _print_line(f"[inter-session] hello rejected: {code} {welcome.get('message', '')}")
                self._stop.set()
                return
            if welcome.get("op") != "welcome":
                _print_line(f"[inter-session] unexpected hello response: {welcome}")
                return

            _write_session_state(self.ppid, {
                "session_id": self.session_id,
                "name": self.name,
                "label": self.label,
                "token": token,
                "nonce": self.nonce,
                "listener_pid": os.getpid(),
                "host": self.host,
                "port": self.port,
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
            })

            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    op = payload.get("op")
                    if op == "msg":
                        line = _format_msg(payload)
                        _print_line(line)
                        sanitized = shared.sanitize_for_stdout(payload.get("text", ""))
                        truncated, was_truncated, full_len = shared.truncate_for_stdout(sanitized)
                        if was_truncated:
                            _print_line(_format_truncation_pointer(payload.get("msg_id", ""), full_len))
                    elif op in ("peer_joined", "peer_left", "renamed"):
                        if self.verbose:
                            _print_line(f"[inter-session] {op}: {payload}")
                    elif op == "pong":
                        pass
                    else:
                        if self.verbose:
                            _print_line(f"[inter-session] {op}: {payload}")
            finally:
                ping_task.cancel()

    async def _ping_loop(self, ws) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(shared.PING_INTERVAL_S)
                await ws.send(json.dumps({"op": "ping"}))
            except (websockets.ConnectionClosed, asyncio.CancelledError):
                return


def _resolve_label(cli_label, env_label, cwd=None) -> str:
    """Resolve (and persist) the session's display label. Precedence:

      1. `--label` given (cli_label is not None): validate, persist it for this
         project ('' clears the persisted label), and use it.
      2. else `$INTER_SESSION_LABEL` (env_label truthy): validate and use as a
         one-off runtime override — NOT persisted.
      3. else: load the persisted per-project label (or '').

    Raises ValueError(bad_label) if an explicitly-supplied label is invalid, so
    the caller can surface it and exit.
    """
    if cli_label is not None:
        if not shared.validate_label(cli_label):
            raise ValueError(cli_label)
        return profile.resolve_label(cli_label, cwd)
    if env_label:
        if not shared.validate_label(env_label):
            raise ValueError(env_label)
        return env_label
    return profile.resolve_label(None, cwd)


def _env_int(*keys, default: int) -> int:
    for k in keys:
        v = os.environ.get(k)
        if v:
            try:
                return int(v)
            except ValueError:
                continue
    return default


def _env_float(*keys, default: float) -> float:
    for k in keys:
        v = os.environ.get(k)
        if v:
            try:
                return float(v)
            except ValueError:
                continue
    return default


def main() -> int:
    # Resolution order for port / idle-shutdown:
    #   1. CLI arg (explicit override)
    #   2. CLAUDE_PLUGIN_OPTION_<KEY>  (set by CC when installed via /plugin install + userConfig)
    #   3. INTER_SESSION_<KEY>         (manual env override)
    #   4. Hard-coded default
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("INTER_SESSION_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=_env_int(
        "CLAUDE_PLUGIN_OPTION_PORT", "INTER_SESSION_PORT", default=shared.DEFAULT_PORT,
    ))
    parser.add_argument("--name", default=os.environ.get("INTER_SESSION_NAME", ""))
    # Default None (not "") so we can tell "flag absent" (→ load the persisted
    # per-project label) apart from an explicit "--label ''" (→ clear it).
    parser.add_argument("--label", default=None)
    parser.add_argument("--idle-shutdown-minutes", type=float, default=_env_float(
        "CLAUDE_PLUGIN_OPTION_IDLE_SHUTDOWN_MINUTES", "INTER_SESSION_IDLE_MINUTES",
        default=10,
    ))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        stream=sys.stderr)

    if args.name and not shared.validate_name(args.name):
        _print_line(f"[inter-session] invalid name {args.name!r}")
        return 1

    try:
        final_label = _resolve_label(args.label, os.environ.get("INTER_SESSION_LABEL"))
    except ValueError as e:
        _print_line(f"[inter-session] invalid label {e.args[0]!r}")
        return 1

    # Plugin auto-start path: monitors.json doesn't pass --name (so the user
    # doesn't have to set INTER_SESSION_NAME). Fall back to a name derived from
    # the cwd basename so the listener doesn't show as `(unnamed)`.
    final_name = args.name
    auto_named = False
    if not final_name:
        final_name = shared.auto_name_from_cwd()
        if final_name:
            auto_named = True
            _print_line(
                f"[inter-session] no --name given; auto-named {final_name!r} "
                f"from cwd (rename with /inter-session rename)"
            )

    client = Client(
        host=args.host, port=args.port, name=final_name, label=final_label,
        idle_shutdown_minutes=args.idle_shutdown_minutes,
        verbose=args.verbose,
    )

    loop = asyncio.new_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, client.stop)
        except NotImplementedError:
            pass
    try:
        return loop.run_until_complete(client.run())
    except ImportError as e:
        _print_line(f"[inter-session] dependencies missing — run /inter-session install-deps ({e})")
        return 0
    finally:
        loop.close()


if __name__ == "__main__":
    try:
        import websockets  # noqa: F401
    except ImportError:
        _print_line("[inter-session] dependencies missing — run /inter-session install-deps")
        sys.exit(0)
    sys.exit(main())
