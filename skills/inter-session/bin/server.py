"""WebSocket server for the inter-session bus."""

from __future__ import annotations

# Bootstrap: re-exec under the project's isolated venv if it exists.
# (Server is normally spawned by client.py with sys.executable already
# pointing at the venv; this guards against direct `python3 server.py`
# invocations.)
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
import logging
import signal
import socket
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import websockets
from websockets.server import WebSocketServerProtocol

# Allow running as a script: ensure the skill dir is on sys.path so
# `from bin import shared` finds the package at <skill-dir>/bin/.
_SKILL_DIR = Path(__file__).resolve().parent.parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

from bin import shared

log = logging.getLogger("inter-session.server")


@dataclass
class ClientState:
    session_id: str
    role: shared.Role
    name: str
    label: str
    cwd: str
    pid: int
    nonce: str
    ws: WebSocketServerProtocol
    since: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


class Server:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = shared.DEFAULT_PORT,
        idle_shutdown_minutes: float = 10,
        sock: Optional[socket.socket] = None,
    ):
        self.host = host
        self.port = port
        self.idle_shutdown_seconds = max(0, idle_shutdown_minutes * 60)
        self._sock = sock
        self._token: Optional[str] = None
        self._registry: dict[str, ClientState] = {}
        self._registry_lock = asyncio.Lock()
        # Rate-limit windows keyed by the *listener* session_id so helpers
        # (control role) can't bypass by opening fresh connections per send.
        self._broadcast_windows: dict[str, deque] = {}
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._last_activity = time.monotonic()

    async def serve(self) -> None:
        shared.secure_dir(shared.data_dir())
        self._token = shared.ensure_token(shared.token_path())

        listen_sock = self._sock
        if listen_sock is None:
            # Direct-bind path: bind ourselves while the socket is NOT yet
            # listening. This keeps the "publish identity before TCP probes can
            # succeed" invariant without trusting a pidfile for a process that
            # may fail to bind because the port is already taken.
            listen_sock = self._bind_socket()

        # Whether the socket came from spawn.py's --fd election or from the
        # direct-bind path above, it is bound but NOT yet listening. Write
        # identity FIRST, then call listen(), so wait_for_server's TCP probe
        # never succeeds before the pidfile exists. Closes the race where a
        # client would refuse to connect ("server identity check failed")
        # because the kernel started accepting connections before identity
        # publish.
        try:
            shared.write_server_identity(os.getpid(), self.host, self.port)
            listen_sock.listen(socket.SOMAXCONN)
            listen_sock.setblocking(False)

            kwargs = dict(max_size=shared.WS_FRAME_CAP, max_queue=1, ping_interval=None)
            server = await websockets.serve(self._handler, sock=listen_sock, **kwargs)
        except Exception:
            listen_sock.close()
            self._unlink_own_identity()
            raise

        self._ready.set()
        log.info("inter-session server listening on %s", self.port)

        idle_task = asyncio.create_task(self._idle_shutdown_loop())
        try:
            await self._stop.wait()
        finally:
            idle_task.cancel()
            server.close()
            await server.wait_closed()
            # Only remove the pidfile/meta if they still belong to this server
            # — avoids race where a fresh server has already taken over the
            # endpoint and rewritten identity before we exit cleanup.
            self._unlink_own_identity()

    def _unlink_own_identity(self) -> None:
        for p in (
            shared.pidfile_path(self.port, self.host),
            shared.pidfile_meta_path(self.port, self.host),
        ):
            try:
                if p.name.endswith(".pid"):
                    try:
                        current_pid = int(p.read_text().strip())
                    except (OSError, ValueError):
                        continue
                    if current_pid != os.getpid():
                        continue
                else:
                    try:
                        meta = json.loads(p.read_text())
                        if not isinstance(meta, dict) or meta.get("pid") != os.getpid():
                            continue
                    except (OSError, json.JSONDecodeError):
                        continue
                p.unlink()
            except OSError:
                pass

    def _bind_socket(self) -> socket.socket:
        infos = socket.getaddrinfo(
            self.host,
            self.port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        last_error: Optional[OSError] = None
        for family, socktype, proto, _canonname, sockaddr in infos:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(sockaddr)
                return sock
            except OSError as e:
                last_error = e
                sock.close()
        if last_error is not None:
            raise last_error
        raise OSError(f"could not resolve bind address {self.host!r}:{self.port}")

    async def wait_ready(self) -> None:
        await self._ready.wait()

    def stop(self) -> None:
        self._stop.set()

    async def _idle_shutdown_loop(self) -> None:
        if self.idle_shutdown_seconds <= 0:
            return
        while not self._stop.is_set():
            try:
                await asyncio.sleep(min(5, self.idle_shutdown_seconds))
            except asyncio.CancelledError:
                return
            now = time.monotonic()
            async with self._registry_lock:
                if not self._registry and (now - self._last_activity) >= self.idle_shutdown_seconds:
                    log.info("idle shutdown")
                    self._stop.set()
                    return

    async def _handler(self, ws: WebSocketServerProtocol) -> None:
        state: Optional[ClientState] = None
        try:
            try:
                first = await asyncio.wait_for(ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                return
            try:
                payload = json.loads(first)
            except json.JSONDecodeError:
                await self._send_error(ws, shared.ErrorCode.UNKNOWN_OP, "malformed JSON")
                return
            if not isinstance(payload, dict):
                await self._send_error(ws, shared.ErrorCode.INVALID_PAYLOAD,
                                       "frame must be a JSON object")
                return
            if payload.get("op") != "hello":
                await self._send_error(ws, shared.ErrorCode.UNKNOWN_OP, "first frame must be hello")
                return
            state = await self._handle_hello(ws, payload)
            if state is None:
                return
            await self._dispatch_loop(state)
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            log.exception("handler error: %s", e)
        finally:
            if state is not None:
                await self._unregister(state)

    async def _handle_hello(self, ws, payload) -> Optional[ClientState]:
        if payload.get("token") != self._token:
            await self._send_error(ws, shared.ErrorCode.UNAUTHORIZED, "bad token")
            return None
        # Type-check fields before passing to validators / int(...). Hostile or
        # buggy peers might send `name=null`, `pid="x"`, `label=123`, etc.
        name = payload.get("name", "")
        label = payload.get("label", "")
        if not isinstance(name, str) or not isinstance(label, str):
            await self._send_error(ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "name and label must be strings")
            return None
        if name and not shared.validate_name(name):
            await self._send_error(ws, shared.ErrorCode.INVALID_NAME, "invalid name")
            return None
        if not shared.validate_label(label):
            await self._send_error(ws, shared.ErrorCode.INVALID_LABEL, "invalid label")
            return None
        try:
            role = shared.Role(payload.get("role", "agent"))
        except (ValueError, TypeError):
            await self._send_error(ws, shared.ErrorCode.INVALID_PAYLOAD, "bad role")
            return None
        # Coerce pid; accept missing/invalid as 0.
        try:
            pid_int = int(payload.get("pid", 0))
        except (TypeError, ValueError):
            pid_int = 0
        # Session_id and nonce must be strings if provided.
        sid_raw = payload.get("session_id")
        if sid_raw is not None and not isinstance(sid_raw, str):
            await self._send_error(ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "session_id must be a string")
            return None
        nonce_raw = payload.get("nonce", "")
        if not isinstance(nonce_raw, str):
            await self._send_error(ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "nonce must be a string")
            return None

        # Control role: act on behalf of an existing agent. Validate `for_session`
        # plus its `nonce` against the registered listener.
        if role == shared.Role.CONTROL:
            for_session = payload.get("for_session", "")
            if not isinstance(for_session, str):
                await self._send_error(ws, shared.ErrorCode.INVALID_PAYLOAD,
                                       "for_session must be a string")
                return None
            reject: Optional[tuple[str, str]] = None
            ctrl_state: Optional[ClientState] = None
            listener_name = listener_label = listener_cwd = ""
            async with self._registry_lock:
                listener = self._registry.get(for_session)
                if listener is None or listener.role != shared.Role.AGENT:
                    reject = (shared.ErrorCode.UNKNOWN_PEER,
                              f"no listener for {for_session!r}")
                elif not nonce_raw or nonce_raw != listener.nonce:
                    reject = (shared.ErrorCode.UNAUTHORIZED,
                              "stale listener state; reconnect")
                else:
                    listener_name = listener.name
                    listener_label = listener.label
                    listener_cwd = listener.cwd
                    ctrl_state = ClientState(
                        session_id=str(uuid.uuid4()),
                        role=role,
                        name=listener_name,
                        label=listener_label,
                        cwd=listener_cwd,
                        pid=pid_int,
                        nonce=nonce_raw,
                        ws=ws,
                    )
                    ctrl_state._for_session = for_session  # type: ignore[attr-defined]
            if reject is not None:
                code, message = reject
                await self._send_error(ws, code, message)
                return None
            await ws.send(json.dumps({
                "op": "welcome",
                "session_id": ctrl_state.session_id,
                "assigned_name": listener_name,
                "for_session": for_session,
            }))
            return ctrl_state

        # Agent role
        sid = sid_raw or str(uuid.uuid4())
        cwd_raw = payload.get("cwd", "")
        if not isinstance(cwd_raw, str):
            cwd_raw = ""
        # Sanitize cwd before storing — it's reflected through `list_ok` and
        # rendered by helpers' table output. Prevents terminal-escape
        # injection by a hostile (or path-with-control-chars) peer.
        cwd_clean = shared.sanitize_for_stdout(cwd_raw)[:256]
        # Defer the close of any replaced ws AND error-send to AFTER lock
        # release so neither stalls other handlers that need the lock.
        old_ws_to_close = None
        reject: Optional[tuple[str, str, dict]] = None  # (code, message, extras)
        state: Optional[ClientState] = None
        async with self._registry_lock:
            existing_same_sid = self._registry.get(sid)
            if existing_same_sid is not None:
                # Reconnect-replace must prove continuity via the nonce. The
                # session_id leaks via `list_ok` and msg metadata, so anyone
                # with the token could otherwise impersonate / kick a peer
                # by reusing its session_id.
                if not nonce_raw or nonce_raw != existing_same_sid.nonce:
                    reject = (
                        shared.ErrorCode.UNAUTHORIZED,
                        "session_id is in use by another connection",
                        {},
                    )
                else:
                    old_ws_to_close = existing_same_sid.ws
                    self._registry.pop(sid, None)
            if reject is None and name:
                for existing in self._registry.values():
                    if existing.role == shared.Role.AGENT and existing.name == name:
                        candidates = [f"{name}-{i}" for i in range(2, 5)]
                        reject = (
                            shared.ErrorCode.NAME_TAKEN,
                            f"name {name!r} is taken",
                            {"candidates": candidates},
                        )
                        break
            if reject is None:
                state = ClientState(
                    session_id=sid,
                    role=role,
                    name=name,
                    label=label,
                    cwd=cwd_clean,
                    pid=pid_int,
                    nonce=nonce_raw,
                    ws=ws,
                )
                self._registry[sid] = state
                self._last_activity = time.monotonic()
        # Lock released — now do any blocking network work.
        if reject is not None:
            code, message, extras = reject
            await self._send_error(ws, code, message, **extras)
            return None
        if old_ws_to_close is not None:
            try:
                await old_ws_to_close.close(code=4000, reason="replaced")
            except Exception:
                pass
        await ws.send(json.dumps({
            "op": "welcome",
            "session_id": sid,
            "assigned_name": name,
        }))
        await self._broadcast_event({"op": "peer_joined", "session_id": sid, "name": name},
                                    exclude=sid)
        return state

    async def _dispatch_loop(self, state: ClientState) -> None:
        async for raw in state.ws:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await self._send_error(state.ws, shared.ErrorCode.UNKNOWN_OP, "malformed JSON")
                continue
            if not isinstance(payload, dict):
                await self._send_error(state.ws, shared.ErrorCode.INVALID_PAYLOAD,
                                       "frame must be a JSON object")
                continue
            op = payload.get("op")
            if op == "ping":
                await state.ws.send(json.dumps({"op": "pong"}))
            elif op == "list":
                await self._handle_list(state)
            elif op == "send":
                await self._handle_send(state, payload)
            elif op == "broadcast":
                await self._handle_broadcast(state, payload)
            elif op == "rename":
                await self._handle_rename(state, payload)
            elif op == "relabel":
                await self._handle_relabel(state, payload)
            elif op == "bye":
                return
            elif op == "hello":
                await self._send_error(state.ws, shared.ErrorCode.UNKNOWN_OP, "duplicate hello")
            else:
                await self._send_error(state.ws, shared.ErrorCode.UNKNOWN_OP, f"unknown op {op!r}")

    async def _handle_list(self, state: ClientState) -> None:
        # Same liveness invariant as send/broadcast/rename: a control whose
        # listener has disappeared shouldn't be able to keep enumerating peers.
        reject: Optional[tuple[str, str]] = None
        sessions: list = []
        async with self._registry_lock:
            if not self._listener_alive_locked(state):
                reject = (shared.ErrorCode.UNAUTHORIZED, "listener no longer connected")
            else:
                sessions = [
                    {
                        "session_id": c.session_id,
                        "name": c.name,
                        "label": c.label,
                        "cwd": c.cwd,
                        "since": c.since.isoformat(),
                    }
                    for c in self._registry.values()
                    if c.role == shared.Role.AGENT
                ]
        if reject is not None:
            code, message = reject
            await self._send_error(state.ws, code, message)
            return
        await state.ws.send(json.dumps({"op": "list_ok", "sessions": sessions}))

    def _from_id_for(self, state: ClientState) -> str:
        if state.role == shared.Role.CONTROL:
            return getattr(state, "_for_session", state.session_id)
        return state.session_id

    def _listener_alive_locked(self, state: ClientState) -> bool:
        """Caller must hold _registry_lock. For agent role: alive iff the
        connection is open (always True from a handler's perspective; the
        handler exits on disconnect). For control role: alive iff the
        impersonated listener is still in the registry."""
        if state.role != shared.Role.CONTROL:
            return True
        listener_sid = getattr(state, "_for_session", None)
        if not listener_sid:
            return False
        return listener_sid in self._registry

    async def _resolve_send_target(
        self, state: ClientState, target: str,
    ) -> tuple[Optional[ClientState], Optional[tuple[str, str, dict]]]:
        """Combined liveness + target resolution under a single lock.
        Returns (target_state, None) on success, (None, (code, msg, extras))
        on rejection."""
        from_id = self._from_id_for(state)
        async with self._registry_lock:
            if not self._listener_alive_locked(state):
                return None, (shared.ErrorCode.UNAUTHORIZED,
                              "listener no longer connected", {})
            agents = [c for c in self._registry.values() if c.role == shared.Role.AGENT]
            # 1) exact session_id
            for c in agents:
                if c.session_id == target:
                    if c.session_id == from_id:
                        return None, (shared.ErrorCode.UNKNOWN_PEER,
                                      "cannot send to self", {})
                    return c, None
            # 2) exact name
            named = [c for c in agents if c.name]
            for c in named:
                if c.name == target:
                    if c.session_id == from_id:
                        return None, (shared.ErrorCode.UNKNOWN_PEER,
                                      "cannot send to self", {})
                    return c, None
            # 3) name prefix
            if target:
                matches = [c for c in named if c.name.startswith(target)]
                if len(matches) == 1:
                    if matches[0].session_id == from_id:
                        return None, (shared.ErrorCode.UNKNOWN_PEER,
                                      "cannot send to self", {})
                    return matches[0], None
                if len(matches) > 1:
                    return None, (shared.ErrorCode.AMBIGUOUS,
                                  f"ambiguous prefix {target!r}",
                                  {"matches": [c.name for c in matches]})
            # 4) session_id prefix (for the short-id shown in `list` output).
            #    Only kicks in if no name matched. Require ≥4 chars to avoid
            #    accidental matches.
            if target and len(target) >= 4:
                sid_matches = [c for c in agents if c.session_id.startswith(target)]
                if len(sid_matches) == 1:
                    if sid_matches[0].session_id == from_id:
                        return None, (shared.ErrorCode.UNKNOWN_PEER,
                                      "cannot send to self", {})
                    return sid_matches[0], None
                if len(sid_matches) > 1:
                    return None, (shared.ErrorCode.AMBIGUOUS,
                                  f"ambiguous session_id prefix {target!r}",
                                  {"matches": [c.session_id[:8] for c in sid_matches]})
            return None, (shared.ErrorCode.UNKNOWN_PEER,
                          f"no agent matches {target!r}", {})

    async def _resolve_broadcast_targets(
        self, state: ClientState, from_id: str,
    ) -> tuple[list, Optional[tuple[str, str]]]:
        """Combined liveness + targets snapshot. Returns (targets, None) on
        success, ([], (code, msg)) on rejection."""
        async with self._registry_lock:
            if not self._listener_alive_locked(state):
                return [], (shared.ErrorCode.UNAUTHORIZED,
                            "listener no longer connected")
            targets = [c for c in self._registry.values()
                       if c.role == shared.Role.AGENT and c.session_id != from_id]
            return targets, None

    async def _handle_send(self, state: ClientState, payload) -> None:
        text = payload.get("text", "")
        if not isinstance(text, str):
            await self._send_error(state.ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "text must be a string")
            return
        if len(text) > shared.TEXT_CAP:
            await self._send_error(state.ws, shared.ErrorCode.TEXT_TOO_LONG,
                                   "text exceeds direct send cap")
            return
        target = payload.get("to", "")
        if not isinstance(target, str):
            await self._send_error(state.ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "to must be a string")
            return
        # Single locked phase: verify listener liveness AND resolve target,
        # so a control whose listener disappears can't slip a message
        # through between the liveness check and the send.
        target_state, reject = await self._resolve_send_target(state, target)
        if reject is not None:
            code, message, extras = reject
            await self._send_error(state.ws, code, message, **extras)
            return
        msg = {
            "op": "msg",
            "msg_id": uuid.uuid4().hex[:8],
            "from": self._from_id_for(state),
            "from_name": state.name,
            "from_label": state.label,
            "to": target_state.name,
            "to_session_id": target_state.session_id,
            "text": text,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
        }
        self._log_message(msg, kind="direct")
        await self._safe_send(target_state, msg)
        self._last_activity = time.monotonic()

    async def _handle_broadcast(self, state: ClientState, payload) -> None:
        text = payload.get("text", "")
        if not isinstance(text, str):
            await self._send_error(state.ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "text must be a string")
            return
        if len(text) > shared.BROADCAST_TEXT_CAP:
            await self._send_error(state.ws, shared.ErrorCode.TEXT_TOO_LONG,
                                   "text exceeds broadcast cap")
            return
        from_id = self._from_id_for(state)
        # Liveness + targets snapshot first — a stale control connection
        # whose listener has gone away should not burn a rate-limit slot.
        targets, reject = await self._resolve_broadcast_targets(state, from_id)
        if reject is not None:
            code, message = reject
            await self._send_error(state.ws, code, message)
            return
        # Rate limit keyed by listener session_id, not per-connection: helpers
        # (role=control) open a fresh connection per send and would otherwise
        # bypass the cap. Charge AFTER liveness so dead-listener attempts
        # don't consume quota.
        now = time.monotonic()
        window = self._broadcast_windows.setdefault(from_id, deque())
        while window and (now - window[0]) > 60:
            window.popleft()
        if len(window) >= shared.BROADCAST_RATE_LIMIT_PER_MIN:
            await self._send_error(state.ws, shared.ErrorCode.RATE_LIMITED,
                                   "broadcast rate limit exceeded")
            return
        window.append(now)
        msg = {
            "op": "msg",
            "msg_id": uuid.uuid4().hex[:8],
            "from": from_id,
            "from_name": state.name,
            "from_label": state.label,
            "text": text,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
        }
        self._log_message(msg, kind="broadcast")
        for target in targets:
            await self._safe_send(target, msg)
        self._last_activity = time.monotonic()

    def _log_message(self, msg: dict, kind: str) -> None:
        """Single-writer JSONL log on the server side. Eliminates the
        previous double-write where every receiver appended the same msg.
        Rotates by size."""
        try:
            shared.secure_dir(shared.data_dir())
            path = shared.messages_log_path()
            shared.rotate_log_if_needed(
                path, shared.MESSAGES_LOG_MAX_BYTES, shared.MESSAGES_LOG_BACKUPS,
            )
            rec = {
                "ts": msg["ts"],
                "msg_id": msg["msg_id"],
                "kind": kind,
                "from": msg["from"],
                "from_name": msg["from_name"],
                "from_label": msg.get("from_label", ""),
                "to": msg.get("to", ""),
                "to_session_id": msg.get("to_session_id", ""),
                "text": msg["text"],
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError:
            pass

    async def _handle_rename(self, state: ClientState, payload) -> None:
        new_name = payload.get("name", "")
        if not isinstance(new_name, str):
            await self._send_error(state.ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "name must be a string")
            return
        if new_name and not shared.validate_name(new_name):
            await self._send_error(state.ws, shared.ErrorCode.INVALID_NAME, "invalid name")
            return
        # Resolve the listener entry to mutate. For agent role it's `state` itself;
        # for control role it's the registered listener whose session_id is `_for_session`.
        target_sid = self._from_id_for(state)
        reject: Optional[tuple[str, str]] = None  # (code, message)
        async with self._registry_lock:
            target = self._registry.get(target_sid)
            if target is None or target.role != shared.Role.AGENT:
                reject = (shared.ErrorCode.UNKNOWN_PEER, "no listener to rename")
            elif new_name:
                for c in self._registry.values():
                    if (c.session_id != target_sid and
                            c.role == shared.Role.AGENT and c.name == new_name):
                        reject = (shared.ErrorCode.NAME_TAKEN, f"name {new_name!r} is taken")
                        break
            if reject is None:
                target.name = new_name
                if state is not target:
                    state.name = new_name
        if reject is not None:
            code, message = reject
            await self._send_error(state.ws, code, message)
            return
        await state.ws.send(json.dumps({"op": "renamed", "name": new_name}))
        await self._broadcast_event(
            {"op": "renamed", "session_id": target_sid, "name": new_name},
            exclude=target_sid,
        )

    async def _handle_relabel(self, state: ClientState, payload) -> None:
        """In-place display-label update — no reconnect, same session_id.
        Mirrors _handle_rename: for a control connection the target is the
        registered listener (`_for_session`), not the ephemeral control."""
        new_label = payload.get("label", "")
        if not isinstance(new_label, str):
            await self._send_error(state.ws, shared.ErrorCode.INVALID_PAYLOAD,
                                   "label must be a string")
            return
        if not shared.validate_label(new_label):
            await self._send_error(state.ws, shared.ErrorCode.INVALID_LABEL, "invalid label")
            return
        target_sid = self._from_id_for(state)
        reject: Optional[tuple[str, str]] = None
        target_name = ""
        async with self._registry_lock:
            target = self._registry.get(target_sid)
            if target is None or target.role != shared.Role.AGENT:
                reject = (shared.ErrorCode.UNKNOWN_PEER, "no listener to relabel")
            else:
                target.label = new_label
                target_name = target.name
                if state is not target:
                    state.label = new_label
        if reject is not None:
            code, message = reject
            await self._send_error(state.ws, code, message)
            return
        await state.ws.send(json.dumps({"op": "relabeled", "label": new_label}))
        await self._broadcast_event(
            {"op": "relabeled", "session_id": target_sid,
             "name": target_name, "label": new_label},
            exclude=target_sid,
        )

    async def _broadcast_event(self, msg: dict, exclude: Optional[str] = None) -> None:
        async with self._registry_lock:
            targets = [c for c in self._registry.values()
                       if c.role == shared.Role.AGENT and c.session_id != exclude]
        payload = json.dumps(msg)
        for c in targets:
            try:
                await c.ws.send(payload)
            except websockets.ConnectionClosed:
                pass

    async def _safe_send(self, target: ClientState, msg: dict) -> None:
        try:
            await target.ws.send(json.dumps(msg))
        except websockets.ConnectionClosed:
            pass

    async def _unregister(self, state: ClientState) -> None:
        async with self._registry_lock:
            current = self._registry.get(state.session_id)
            if current is state:
                self._registry.pop(state.session_id, None)
                self._last_activity = time.monotonic()
                should_announce = state.role == shared.Role.AGENT
            else:
                should_announce = False
        if should_announce:
            await self._broadcast_event(
                {"op": "peer_left", "session_id": state.session_id},
                exclude=state.session_id,
            )

    async def _send_error(self, ws, code: str, message: str, **extra) -> None:
        try:
            await ws.send(json.dumps({"op": "error", "code": code, "message": message, **extra}))
        except websockets.ConnectionClosed:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=shared.DEFAULT_PORT)
    parser.add_argument("--fd", type=int, default=None,
                        help="Adopt an inherited bound socket fd (set by client.py)")
    parser.add_argument("--idle-shutdown-minutes", type=float, default=10)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    # Quiet `websockets.server` INFO chatter ("connection rejected (400)"
    # etc.) — these are mostly from `is_server_up()` doing a TCP probe with
    # no Upgrade header, which is expected behavior, not a problem.
    logging.getLogger("websockets.server").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    sock = None
    if args.fd is not None:
        # Adopt the bound (NOT yet listening) fd. Server.serve() will write
        # identity FIRST, then listen() — closing the race where
        # wait_for_server's TCP probe sees "server up" before pidfile exists.
        sock = socket.socket(fileno=args.fd)
        sock.setblocking(False)

    srv = Server(
        host=args.host,
        port=args.port,
        idle_shutdown_minutes=args.idle_shutdown_minutes,
        sock=sock,
    )
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, srv.stop)
        except NotImplementedError:
            pass
    try:
        loop.run_until_complete(srv.serve())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
