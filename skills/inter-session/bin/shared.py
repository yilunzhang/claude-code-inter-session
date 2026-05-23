"""Shared helpers: paths, validation, sanitization, atomic token, role enum, protocol constants."""

from __future__ import annotations

import enum
import errno
import json
import os
import re
import secrets
import unicodedata
from urllib.parse import quote
from pathlib import Path

DEFAULT_PORT = 9473
WS_FRAME_CAP = 16 * 1024 * 1024
TEXT_CAP = 10 * 1024 * 1024
BROADCAST_TEXT_CAP = 256 * 1024
# Body cap for the stdout notification line that Claude Code's monitor
# delivers to the receiving LLM. Empirically (issue #2), CC clips each
# notification at ~512 chars total, so above this budget the truncated=
# marker and cont-pointer line never reach the LLM. 400 leaves room for
# our prefix (`[inter-session msg=… from="…" "…" truncated=N] `) under
# the 512 limit in typical cases; very long name+label combos may still
# clip, but the cont-pointer line is short and always fits, so the LLM
# still gets the messages.log path for full content.
STDOUT_CAP = 400
MAX_HOPS = 4
PING_INTERVAL_S = 15
PONG_TIMEOUT_S = 30
RECONNECT_BACKOFF_MIN_S = 0.25
RECONNECT_BACKOFF_MAX_S = 4.0
RECONNECT_JITTER_FRAC = 0.2
ELECTION_BIND_RETRIES = 8
BROADCAST_RATE_LIMIT_PER_MIN = 60

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LABEL_MAX_CP = 60


class Role(str, enum.Enum):
    AGENT = "agent"
    CONTROL = "control"


class ErrorCode:
    INVALID_NAME = "invalid_name"
    INVALID_LABEL = "invalid_label"
    INVALID_PAYLOAD = "invalid_payload"
    NAME_TAKEN = "name_taken"
    UNKNOWN_PEER = "unknown_peer"
    AMBIGUOUS = "ambiguous"
    TEXT_TOO_LONG = "text_too_long"
    UNAUTHORIZED = "unauthorized"
    RATE_LIMITED = "rate_limited"
    HOP_LIMIT = "hop_limit"
    UNKNOWN_OP = "unknown_op"


def data_dir() -> Path:
    env = os.environ.get("INTER_SESSION_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "data" / "inter-session"


def server_log_path() -> Path:
    return data_dir() / "server.log"


def messages_log_path() -> Path:
    return data_dir() / "messages.log"


def token_path() -> Path:
    return data_dir() / "token"


def _identity_stem(port: int = DEFAULT_PORT, host: str | None = None) -> str:
    """Filename stem for server identity.

    Keep the default 127.0.0.1 path as `server.<port>` for compatibility with
    older installs/tests. Non-default hosts include a percent-encoded host
    component so endpoints like localhost:9473 and 127.0.0.1:9473 do not
    clobber each other in a shared data_dir.
    """
    if host is None or host == "127.0.0.1":
        return f"server.{port}"
    return f"server.{quote(host, safe='')}.{port}"


def pidfile_path(port: int = DEFAULT_PORT, host: str | None = None) -> Path:
    """Endpoint-scoped pidfile for a listener host+port."""
    return data_dir() / f"{_identity_stem(port, host)}.pid"


def pidfile_meta_path(port: int = DEFAULT_PORT, host: str | None = None) -> Path:
    return data_dir() / f"{_identity_stem(port, host)}.pid.meta"


def clients_dir() -> Path:
    return data_dir() / "clients"


def client_lock_path(ppid: int) -> Path:
    return clients_dir() / f"{ppid}.lock"


def client_session_path(ppid: int) -> Path:
    return clients_dir() / f"{ppid}.session"


def validate_name(s: str) -> bool:
    if not isinstance(s, str):
        return False
    return bool(NAME_RE.match(s))


def validate_label(s: str) -> bool:
    if not isinstance(s, str):
        return False
    if s == "":
        return True
    nfc = unicodedata.normalize("NFC", s)
    if len(nfc) > LABEL_MAX_CP:
        return False
    for ch in nfc:
        if ch == " ":
            continue
        cat = unicodedata.category(ch)
        if cat[0] == "C" or cat[0] == "Z":
            return False
    return True


def normalize_label(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def sanitize_for_stdout(s: str) -> str:
    s = ANSI_RE.sub("", s)
    out = []
    for ch in s:
        if ch == "\t":
            out.append(ch)
        elif ch == "\n" or ch == "\r":
            out.append("↵")
        elif unicodedata.category(ch).startswith("C"):
            continue
        else:
            out.append(ch)
    return "".join(out)


def truncate_for_stdout(s: str, cap: int = STDOUT_CAP) -> tuple[str, bool, int]:
    full_len = len(s)
    if full_len <= cap:
        return s, False, full_len
    return s[:cap], True, full_len


def ensure_token(path: Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Re-tighten perms in case the file was created with looser permissions
        # (older version, accidental chmod, restore from backup, etc.). Also
        # refuse to follow symlinks since that's a leak vector.
        try:
            st = os.lstat(str(path))
            import stat as _stat
            if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISREG(st.st_mode):
                raise RuntimeError(f"token path {path} is not a regular file")
            os.chmod(str(path), 0o600)
        except OSError:
            pass
        return _read_token_with_retry(path)
    try:
        token = secrets.token_urlsafe(32)
        os.write(fd, token.encode("utf-8"))
        os.fsync(fd)
        return token
    finally:
        os.close(fd)


def _read_token_with_retry(path: Path, attempts: int = 50, delay: float = 0.005) -> str:
    """Read a token file that may currently be empty because another process
    just created it (O_CREAT|O_EXCL) and hasn't yet written the body.
    """
    import time
    for _ in range(attempts):
        try:
            content = path.read_text().strip()
            if content:
                return content
        except OSError:
            pass
        time.sleep(delay)
    raise RuntimeError(f"token file at {path} is empty after {attempts} retries")


MESSAGES_LOG_MAX_BYTES = 50 * 1024 * 1024
MESSAGES_LOG_BACKUPS = 5


def rotate_log_if_needed(path: Path, max_bytes: int, backups: int) -> None:
    """Size-based rotation. Single-writer assumption (server-only writer).
    Standard shift: drop the oldest, then path.<i-1> → path.<i> for i in
    [backups..2], then path → path.1.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    for i in range(backups, 0, -1):
        nxt = path.parent / f"{path.name}.{i}"
        prev = path if i == 1 else path.parent / f"{path.name}.{i - 1}"
        if i == backups:
            try:
                nxt.unlink()
            except OSError:
                pass
        try:
            prev.rename(nxt)
        except OSError:
            pass


def safe_pid_alive(pid: int) -> bool:
    """True if a process with `pid` is alive (or we can signal 0 to it)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False


def write_server_identity(pid: int, host: str, port: int) -> None:
    """Write endpoint-scoped server identity files before the listener starts
    accepting clients. Default-host files keep the historical
    `server.<port>.pid` name; non-default hosts include host+port so multiple
    endpoints in the same data_dir don't clobber each other's identity."""
    pid_path = pidfile_path(port, host)
    meta_path = pidfile_meta_path(port, host)
    pid_path.write_text(str(pid))
    secure_file(pid_path)
    meta_path.write_text(json.dumps({
        "pid": pid,
        "host": host,
        "port": port,
    }))
    secure_file(meta_path)


def _pidfile_meta_matches(
    pid: int, host: str | None, port: int | None, meta_path: Path,
) -> bool:
    if host is None and port is None:
        return True
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(meta, dict):
        return False
    if meta.get("pid") != pid:
        return False
    if port is not None and meta.get("port") != port:
        return False
    if host is not None and meta.get("host") != host:
        return False
    return True


def _cmdline_port_matches(cmdline: list[str], port: int | None) -> bool:
    if port is None:
        return True
    for i, arg in enumerate(cmdline):
        if arg == "--port" and i + 1 < len(cmdline):
            try:
                return int(cmdline[i + 1]) == port
            except ValueError:
                return False
        if arg.startswith("--port="):
            try:
                return int(arg.split("=", 1)[1]) == port
            except ValueError:
                return False
    # Older/manual default-port servers may not have an explicit --port arg.
    return port == DEFAULT_PORT


def _cmdline_host_matches(cmdline: list[str], host: str | None) -> bool:
    """Symmetric to _cmdline_port_matches for --host.
    A missing --host arg implies the server's default (127.0.0.1)."""
    if host is None:
        return True
    for i, arg in enumerate(cmdline):
        if arg == "--host" and i + 1 < len(cmdline):
            return cmdline[i + 1] == host
        if arg.startswith("--host="):
            return arg.split("=", 1)[1] == host
    return host == "127.0.0.1"


def _identity_lookup_paths(
    host: str | None, port: int | None,
) -> tuple[Path, Path, bool]:
    lookup_port = port if port is not None else DEFAULT_PORT
    if host is None:
        return pidfile_path(lookup_port), pidfile_meta_path(lookup_port), False
    pid_path = pidfile_path(lookup_port, host)
    meta_path = pidfile_meta_path(lookup_port, host)
    if pid_path.exists() or meta_path.exists():
        return pid_path, meta_path, False
    # Upgrade path: previous versions wrote only server.<port>.pid(.meta),
    # even for custom --host. Keep accepting those if metadata/cmdline prove
    # the exact endpoint.
    return pidfile_path(lookup_port), pidfile_meta_path(lookup_port), True


def verify_server_identity(host: str | None = None, port: int | None = None) -> bool:
    """Defense-in-depth against port squatting: confirm the listener on our
    port is actually our server.py (not a hostile local process that bound
    9473 first to harvest tokens from connecting clients).

    If host/port are supplied, the pidfile metadata must match the exact
    listener endpoint the caller is about to dial. Otherwise a legitimate
    server on one port could make a squatter on another port look trusted.

    Fails closed:
      - missing pidfile        → False (squatter scenario)
      - unreadable / bad pid   → False
      - dead pid               → False
      - missing/mismatched endpoint metadata when host/port supplied → False
      - cmdline doesn't include bin/server.py → False
      - cmdline includes bin/server.py        → True

    Soft-trust fallback: if psutil isn't available, trust an alive pidfile
    pid (the protection degrades but doesn't break setups without psutil).

    Server-side ordering note: server.py writes the pidfile BEFORE
    `await websockets.serve()`, so by the time a client's WS upgrade
    completes, the pidfile exists. No race window for legit clients.

    Threat model caveat: a same-UID attacker can spoof the pidfile, since
    it lives in the user's data dir. This isn't a crypto guarantee — it
    closes the opportunistic squatting vector but assumes the user trusts
    same-UID code (per README threat model).
    """
    pid_path, meta_path, using_legacy_path = _identity_lookup_paths(host, port)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        return False
    if not safe_pid_alive(pid):
        return False
    endpoint_check_needed = host is not None or port is not None
    meta_exists = meta_path.exists()
    meta_verified = False
    if endpoint_check_needed and meta_exists:
        if not _pidfile_meta_matches(pid, host, port, meta_path):
            return False
        meta_verified = True
    try:
        import psutil
    except ImportError:
        return not endpoint_check_needed or meta_verified
    try:
        cmdline = psutil.Process(pid).cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return not endpoint_check_needed or meta_verified
    cmd = " ".join(cmdline)
    if "bin/server.py" not in cmd:
        return False
    if endpoint_check_needed and (not meta_exists or using_legacy_path):
        # Cmdline-only path: must prove BOTH port AND host. Otherwise an
        # old default-host pidfile could trust a different requested host.
        return (_cmdline_port_matches(cmdline, port)
                and _cmdline_host_matches(cmdline, host))
    return True


def auto_name_from_cwd(cwd: str = "") -> str:
    """Best-effort name derivation from the current working directory's basename.

    Returns a name matching `validate_name`, or '' if no valid name can be
    derived. Used in plugin mode where the user/agent didn't supply --name.
    """
    base = os.path.basename(cwd or os.getcwd()).lower()
    cleaned: list[str] = []
    for ch in base:
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            cleaned.append(ch)
        elif cleaned and cleaned[-1] != "-":
            cleaned.append("-")
    name = "".join(cleaned).strip("-")[:40]
    if not name or not validate_name(name):
        return ""
    return name


def secure_dir(path: Path) -> bool:
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(str(path), 0o700)
        return True
    except OSError:
        return False


def secure_file(path: Path) -> bool:
    path = Path(path)
    try:
        os.chmod(str(path), 0o600)
        return True
    except OSError:
        return False


def find_cc_ancestor_pid() -> int:
    """Walk up the process tree, return the pid of the first Claude Code
    ancestor. Returns -1 if none found.

    Why: In real CC, the monitor (`client.py`) and helper CLIs (`send.py` etc.)
    are spawned by *different* Bash subshells. They are siblings, not
    ancestor-descendant. `os.getppid()` of one cannot reach the other. But
    both share the CC main process as a common ancestor — keying state files
    by that pid makes them mutually discoverable.

    Detection: match on `cmdline[0]` basename, NOT `psutil.Process.name()`.
    Modern CC sets its proctitle to its version string (e.g., "2.1.119"),
    which makes `p.name()` useless. The actual binary basename in
    `cmdline[0]` is reliably `claude` (or `node` for older bundles).
    """
    try:
        import psutil
    except ImportError:
        return -1

    pid = os.getppid()
    seen: set[int] = set()
    while pid and pid not in seen and pid != 1:
        seen.add(pid)
        try:
            p = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return -1
        try:
            cmd = p.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            cmd = []
        if cmd:
            exe = os.path.basename(cmd[0]).lower()
            if exe == "claude" or exe.startswith("claude-"):
                return pid
            # Background sessions launch the Claude binary by its versioned
            # path (e.g. ~/.local/share/claude/versions/2.1.146), whose
            # basename is a version number, not "claude". Without recognizing
            # it, the walk skips the per-session process and resolves to the
            # shared supervisor (claude daemon run), so distinct background
            # sessions collide on one ppid lock and client/helper self-detection
            # mismatches. Match the versioned path so each bg session keys on
            # its own process; intra-session dedup is preserved.
            full = cmd[0].lower()
            if "/claude/versions/" in full or "/.local/share/claude/" in full:
                return pid
            if exe in ("node", "node.exe"):
                joined = " ".join(cmd[1:]).lower()
                if (
                    "/claude" in joined
                    or "claude-code" in joined
                    or "/.claude/" in joined
                ):
                    return pid
        try:
            pid = p.ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return -1
    return -1


def resolve_listener_key() -> int:
    """Pid used as the state-file key for the inter-session listener of the
    current Claude Code session. Both `client.py` (writer) and helpers
    (`send.py`/`list.py` readers) must compute this the same way.

    Resolution order:
      1. `INTER_SESSION_PPID_OVERRIDE` (test/debug)
      2. The CC main process pid (production: a stable common ancestor)
      3. `os.getppid()` (fallback for non-CC environments)
    """
    override = os.environ.get("INTER_SESSION_PPID_OVERRIDE")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    cc = find_cc_ancestor_pid()
    if cc > 0:
        return cc
    return os.getppid()
