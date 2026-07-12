"""Update this session's display label in place (no reconnect), then exit.
role=control. On success also persists the label per-project (bin/profile.py)
so it survives the next restart. `--label ""` clears the label."""

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

# Allow running as a script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import websockets

from bin import shared, discover, profile


async def _run(args) -> int:
    if not shared.validate_label(args.label):
        print(f"invalid label {args.label!r}", file=sys.stderr)
        return 1

    state, state_path = discover.find_listener_state_with_path()
    if state is None:
        print("not connected; run /inter-session in this Claude Code session first",
              file=sys.stderr)
        return 1

    host = state.get("host", "127.0.0.1")
    port = state.get("port", shared.DEFAULT_PORT)
    token = state["token"]
    for_session = state["session_id"]
    nonce = state["nonce"]

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
            "for_session": for_session,
            "nonce": nonce,
            "token": token,
        }))
        welcome = json.loads(await ws.recv())
        if welcome.get("op") == "error":
            code = welcome.get("code", "")
            if code in (shared.ErrorCode.UNKNOWN_PEER, shared.ErrorCode.UNAUTHORIZED):
                discover.unlink_if_matches(state_path, state)
                print("not connected; run /inter-session in this Claude Code session first",
                      file=sys.stderr)
                return 1
            print(f"hello error: {code} {welcome.get('message', '')}", file=sys.stderr)
            return 1

        await ws.send(json.dumps({"op": "relabel", "label": args.label}))
        try:
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
        except asyncio.TimeoutError:
            print("no response from server", file=sys.stderr)
            return 1
        if resp.get("op") == "error":
            print(f"error: {resp.get('code', '')}: {resp.get('message', '')}",
                  file=sys.stderr)
            return 1
    finally:
        await ws.close()

    # Live update succeeded — persist so the label also survives restart.
    profile.save_label(args.label)
    print(f"relabeled to {args.label!r}" if args.label else "label cleared")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    # Required so `relabel` always carries an intent; an empty string clears.
    parser.add_argument("--label", required=True, help='new label ("" clears it)')
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("dependencies missing — run /inter-session install-deps", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
