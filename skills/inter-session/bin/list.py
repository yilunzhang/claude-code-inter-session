"""List connected agent sessions, then exit. role=control."""

from __future__ import annotations

# Bootstrap: re-exec under the project's isolated venv if it exists.
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
import json
import uuid
from datetime import datetime, timezone

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import websockets

from bin import shared, discover


def _format_since(iso: str) -> str:
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return "?"
    delta = datetime.now(tz=timezone.utc) - t
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h"


async def _run(args) -> int:
    state, state_path = discover.find_listener_state_with_path()
    if state is None:
        if args.self:
            print("not connected")
            return 0
        print("not connected; run /inter-session in this Claude Code session first",
              file=sys.stderr)
        return 1

    if args.self:
        listener_pid = state.get("listener_pid", 0)
        alive = shared.safe_pid_alive(int(listener_pid)) if listener_pid else False
        if not alive:
            # Stale state: listener process is gone. TOCTOU-safe cleanup —
            # don't delete a fresh state written by a reconnected listener.
            discover.unlink_if_matches(state_path, state)
            print("not connected (stale state cleaned up)")
            return 0
        print(f"name={state.get('name', '') or '(unnamed)'}")
        print(f"session_id={state['session_id']}")
        print(f"listener_pid={listener_pid}")
        print(f"host={state.get('host', '127.0.0.1')}")
        print(f"port={state.get('port', shared.DEFAULT_PORT)}")
        return 0

    host = state.get("host", "127.0.0.1")
    port = state.get("port", shared.DEFAULT_PORT)
    token = state["token"]

    if not shared.verify_server_identity(host, port):
        print(f"server identity check failed ({host}:{port} not held by bin/server.py)",
              file=sys.stderr)
        return 1

    try:
        ws = await websockets.connect(
            f"ws://{host}:{port}/", max_size=shared.WS_FRAME_CAP,
        )
    except OSError as e:
        print(f"connect failed: {e}", file=sys.stderr)
        return 1

    try:
        await ws.send(json.dumps({
            "op": "hello",
            "session_id": str(uuid.uuid4()),
            "name": "",
            "label": "",
            "cwd": os.getcwd(),
            "pid": os.getpid(),
            "role": shared.Role.CONTROL.value,
            "for_session": state["session_id"],
            "nonce": state["nonce"],
            "token": token,
        }))
        welcome_raw = await ws.recv()
        welcome = json.loads(welcome_raw)
        if welcome.get("op") == "error":
            code = welcome.get("code", "")
            if code in (shared.ErrorCode.UNKNOWN_PEER, shared.ErrorCode.UNAUTHORIZED):
                discover.unlink_if_matches(state_path, state)
                print("not connected; run /inter-session in this Claude Code session first",
                      file=sys.stderr)
                return 1
            print(f"hello error: {code}", file=sys.stderr)
            return 1
        await ws.send(json.dumps({"op": "list"}))
        resp_raw = await ws.recv()
        resp = json.loads(resp_raw)
        if resp.get("op") != "list_ok":
            print(f"unexpected response: {resp}", file=sys.stderr)
            return 1
        print(f"{'NAME':<24} {'LABEL':<24} {'CWD':<40} {'SINCE':<8} ID")
        for s in resp["sessions"]:
            name = s.get("name", "") or "(unnamed)"
            label = shared.sanitize_label_for_display(s.get("label", ""))
            cwd = s.get("cwd", "")
            if len(cwd) > 38:
                cwd = "…" + cwd[-37:]
            since = _format_since(s.get("since", ""))
            sid = s.get("session_id", "")[:8]
            print(f"{name:<24} {label:<24} {cwd:<40} {since:<8} {sid}")
    finally:
        await ws.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self", action="store_true",
                        help="print only this session's status")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("dependencies missing — run /inter-session install-deps", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
