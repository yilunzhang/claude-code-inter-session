"""Server tests — protocol semantics + handlers."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import websockets

from bin import shared, spawn

# Server module imported lazily to give tests a chance to run shared first
from bin.server import Server


@pytest.fixture
async def running_server(tmp_data_dir, free_port):
    shared.secure_dir(tmp_data_dir)
    token = shared.ensure_token(shared.token_path())
    srv = Server(host="127.0.0.1", port=free_port, idle_shutdown_minutes=10)
    task = asyncio.create_task(srv.serve())
    await srv.wait_ready()
    yield srv, free_port, token
    srv.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()


async def _connect(port, **kwargs):
    return await websockets.connect(f"ws://127.0.0.1:{port}/", max_size=shared.WS_FRAME_CAP)


async def _send_op(ws, op: str, **fields):
    await ws.send(json.dumps({"op": op, **fields}))


async def _recv(ws, timeout=2.0):
    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(msg)


async def _recv_until(ws, ops, timeout=2.0, drop=("peer_joined", "peer_left", "renamed")):
    """Drain `drop` notifications and return the next frame whose op is in `ops`.
    `ops` may be a single string or an iterable. Errors are always returned."""
    if isinstance(ops, str):
        ops = (ops,)
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"never received {ops}")
        msg = await _recv(ws, timeout=remaining)
        if msg.get("op") in ops or msg.get("op") == "error":
            return msg
        if msg.get("op") in drop:
            continue
        # Unexpected op — return so caller sees it
        return msg


async def _drain(ws, count: int = None, timeout=0.5):
    """Drain up to `count` frames or until idle for `timeout`. Returns drained frames."""
    drained = []
    while True:
        try:
            msg = await _recv(ws, timeout=timeout)
            drained.append(msg)
            if count is not None and len(drained) >= count:
                return drained
        except asyncio.TimeoutError:
            return drained


class TestServerIdentityOrdering:
    @pytest.mark.asyncio
    async def test_direct_bind_writes_identity_before_listen(
        self, tmp_data_dir, free_port, monkeypatch,
    ):
        """Direct server.py launches must not become TCP-reachable before the
        pidfile/meta exist. Otherwise a client can pass wait_for_server's TCP
        probe and then fail verify_server_identity."""
        shared.secure_dir(tmp_data_dir)
        original_write = shared.write_server_identity

        def checked_write(pid, host, port):
            assert not spawn.is_server_up(host, port, timeout=0.01)
            return original_write(pid, host, port)

        monkeypatch.setattr(shared, "write_server_identity", checked_write)
        srv = Server(host="127.0.0.1", port=free_port, idle_shutdown_minutes=10)
        task = asyncio.create_task(srv.serve())
        await srv.wait_ready()
        try:
            assert spawn.is_server_up("127.0.0.1", free_port)
            assert shared.pidfile_path(free_port).exists()
            assert shared.pidfile_meta_path(free_port).exists()
        finally:
            srv.stop()
            await asyncio.wait_for(task, timeout=2.0)


async def _hello(ws, token, name="alpha", label="", role=shared.Role.AGENT, session_id=None, **extra):
    sid = session_id or str(uuid.uuid4())
    await _send_op(
        ws,
        "hello",
        session_id=sid,
        name=name,
        label=label,
        cwd="/tmp",
        pid=12345,
        role=role.value if isinstance(role, shared.Role) else role,
        token=token,
        **extra,
    )
    welcome = await _recv(ws)
    return sid, welcome


class TestHello:
    async def test_hello_returns_welcome(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            sid, welcome = await _hello(ws, token)
            assert welcome["op"] == "welcome"
            assert welcome["session_id"] == sid
        finally:
            await ws.close()

    async def test_invalid_name_rejected(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _send_op(
                ws, "hello",
                session_id=str(uuid.uuid4()),
                name="Bad Name!",  # uppercase + space + !
                label="",
                cwd="/tmp",
                pid=1,
                role="agent",
                token=token,
            )
            err = await _recv(ws)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.INVALID_NAME
        finally:
            await ws.close()

    async def test_invalid_label_rejected(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _send_op(
                ws, "hello",
                session_id=str(uuid.uuid4()),
                name="alpha",
                label="bad\nlabel",  # newline = Cc
                cwd="/tmp",
                pid=1,
                role="agent",
                token=token,
            )
            err = await _recv(ws)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.INVALID_LABEL
        finally:
            await ws.close()

    async def test_unauthorized_rejected(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _send_op(
                ws, "hello",
                session_id=str(uuid.uuid4()),
                name="alpha",
                label="",
                cwd="/tmp",
                pid=1,
                role="agent",
                token="wrong-token",
            )
            err = await _recv(ws)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.UNAUTHORIZED
        finally:
            await ws.close()

    async def test_name_taken(self, running_server):
        srv, port, token = running_server
        ws1 = await _connect(port)
        ws2 = await _connect(port)
        try:
            await _hello(ws1, token, name="alpha")
            await _send_op(ws2, "hello", session_id=str(uuid.uuid4()), name="alpha",
                           label="", cwd="/tmp", pid=2, role="agent", token=token)
            err = await _recv(ws2)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.NAME_TAKEN
            assert "candidates" in err
            assert err["candidates"][0].startswith("alpha-")
        finally:
            await ws1.close()
            await ws2.close()

    async def test_empty_name_allowed(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            sid, welcome = await _hello(ws, token, name="")
            assert welcome["op"] == "welcome"
        finally:
            await ws.close()


class TestList:
    async def test_lists_agents_not_controls(self, running_server):
        srv, port, token = running_server
        ws_a = await _connect(port)
        ws_b = await _connect(port)
        ws_c = await _connect(port)
        try:
            await _hello(ws_a, token, name="alpha", role=shared.Role.AGENT)
            await _hello(ws_b, token, name="beta", role=shared.Role.AGENT)
            await _hello(ws_c, token, name="gamma", role=shared.Role.CONTROL)
            # Drain peer_joined notifications on ws_a so list response is the next frame.
            for _ in range(2):
                try:
                    await _recv(ws_a, timeout=0.5)
                except asyncio.TimeoutError:
                    break
            await _send_op(ws_a, "list")
            resp = await _recv(ws_a)
            assert resp["op"] == "list_ok"
            names = {s["name"] for s in resp["sessions"]}
            assert names == {"alpha", "beta"}
        finally:
            await ws_a.close()
            await ws_b.close()
            await ws_c.close()


class TestSend:
    async def test_routes_to_target(self, running_server):
        srv, port, token = running_server
        ws_a = await _connect(port)
        ws_b = await _connect(port)
        try:
            sid_a, _ = await _hello(ws_a, token, name="alpha")
            sid_b, _ = await _hello(ws_b, token, name="beta")
            # drain peer_joined on b
            try:
                await _recv(ws_b, timeout=0.5)
            except asyncio.TimeoutError:
                pass
            await _send_op(ws_a, "send", to="beta", text="hi there")
            msg = await _recv(ws_b)
            assert msg["op"] == "msg"
            assert msg["from"] == sid_a
            assert msg["from_name"] == "alpha"
            assert msg["text"] == "hi there"
            assert "msg_id" in msg
        finally:
            await ws_a.close()
            await ws_b.close()

    async def test_unknown_peer_errors(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _hello(ws, token, name="alpha")
            await _send_op(ws, "send", to="nonexistent", text="hi")
            err = await _recv(ws)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.UNKNOWN_PEER
        finally:
            await ws.close()

    async def test_ambiguous_prefix_errors(self, running_server):
        srv, port, token = running_server
        ws_a = await _connect(port)
        ws_b1 = await _connect(port)
        ws_b2 = await _connect(port)
        try:
            await _hello(ws_a, token, name="alpha")
            await _hello(ws_b1, token, name="beta-one")
            await _hello(ws_b2, token, name="beta-two")
            await _send_op(ws_a, "send", to="beta", text="hi")
            err = await _recv_until(ws_a, "error")
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.AMBIGUOUS
            assert {"beta-one", "beta-two"}.issubset(set(err["matches"]))
        finally:
            await ws_a.close()
            await ws_b1.close()
            await ws_b2.close()

    async def test_unique_prefix_succeeds(self, running_server):
        srv, port, token = running_server
        ws_a = await _connect(port)
        ws_b = await _connect(port)
        try:
            await _hello(ws_a, token, name="alpha")
            await _hello(ws_b, token, name="banana")
            try:
                await _recv(ws_b, timeout=0.5)
            except asyncio.TimeoutError:
                pass
            await _send_op(ws_a, "send", to="ban", text="hi")
            msg = await _recv(ws_b)
            assert msg["op"] == "msg"
            assert msg["text"] == "hi"
        finally:
            await ws_a.close()
            await ws_b.close()

    async def test_text_too_long_rejected(self, running_server):
        srv, port, token = running_server
        ws_a = await _connect(port)
        ws_b = await _connect(port)
        try:
            await _hello(ws_a, token, name="alpha")
            await _hello(ws_b, token, name="beta")
            big = "x" * (shared.TEXT_CAP + 1)
            await _send_op(ws_a, "send", to="beta", text=big)
            err = await _recv_until(ws_a, "error", timeout=10.0)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.TEXT_TOO_LONG
        finally:
            await ws_a.close()
            await ws_b.close()


class TestSelfSend:
    async def test_self_send_rejected(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            sid, _ = await _hello(ws, token, name="alpha")
            await _send_op(ws, "send", to="alpha", text="self-ping")
            err = await _recv_until(ws, "error")
            assert err["code"] == shared.ErrorCode.UNKNOWN_PEER
            assert "self" in err["message"].lower()
        finally:
            await ws.close()

    async def test_control_self_send_rejected(self, running_server):
        """A control connection acting for listener X can't send a message
        with `to=X` — would create an instant loop."""
        srv, port, token = running_server
        nonce = "ctl-self-test-nonce"
        sid = str(uuid.uuid4())
        ws_listener = await _connect(port)
        ws_control = await _connect(port)
        try:
            # Listener with explicit non-empty nonce
            await _send_op(
                ws_listener, "hello",
                session_id=sid, name="alpha", label="",
                cwd="/tmp", pid=1, role="agent",
                nonce=nonce, token=token,
            )
            await _recv(ws_listener)  # welcome
            # Control acting for alpha
            await _send_op(
                ws_control, "hello",
                session_id=str(uuid.uuid4()), name="", label="",
                cwd="/tmp", pid=1, role="control",
                for_session=sid, nonce=nonce, token=token,
            )
            await _recv(ws_control)  # welcome
            # Control sends to its own listener — must be rejected
            await _send_op(ws_control, "send", to="alpha", text="self")
            err = await _recv_until(ws_control, "error")
            assert err["code"] == shared.ErrorCode.UNKNOWN_PEER
            assert "self" in err["message"].lower()
        finally:
            await ws_listener.close()
            await ws_control.close()


class TestBroadcast:
    async def test_routes_to_all_others(self, running_server):
        srv, port, token = running_server
        ws_a = await _connect(port)
        ws_b = await _connect(port)
        ws_c = await _connect(port)
        try:
            await _hello(ws_a, token, name="alpha")
            await _hello(ws_b, token, name="beta")
            await _hello(ws_c, token, name="gamma")
            await _send_op(ws_a, "broadcast", text="hello all")
            msg_b = await _recv_until(ws_b, "msg")
            msg_c = await _recv_until(ws_c, "msg")
            assert msg_b["text"] == "hello all"
            assert msg_c["text"] == "hello all"
            # Sender should NOT receive a "msg" frame for its own broadcast,
            # though it may have peer_joined frames from b and c.
            sender_drain = await _drain(ws_a, timeout=0.3)
            assert all(m.get("op") != "msg" for m in sender_drain)
        finally:
            await ws_a.close()
            await ws_b.close()
            await ws_c.close()

    async def test_broadcast_text_too_long(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _hello(ws, token, name="alpha")
            big = "x" * (shared.BROADCAST_TEXT_CAP + 1)
            await _send_op(ws, "broadcast", text=big)
            err = await _recv(ws)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.TEXT_TOO_LONG
        finally:
            await ws.close()

    async def test_rate_limit_per_listener_not_per_connection(self, running_server):
        """Helpers (role=control) must not bypass the broadcast rate limit by
        opening a fresh connection per send."""
        srv, port, token = running_server
        listener_nonce = "test-nonce-abc123"
        # Listener with explicit non-empty nonce
        ws_listener = await _connect(port)
        sid = str(uuid.uuid4())
        await _send_op(
            ws_listener, "hello",
            session_id=sid, name="alpha", label="",
            cwd="/tmp", pid=1, role="agent",
            nonce=listener_nonce, token=token,
        )
        await _recv(ws_listener)  # welcome
        # Other listener so broadcasts have a target
        ws_peer = await _connect(port)
        await _hello(ws_peer, token, name="beta")
        try:
            errs = 0
            successes = 0
            for i in range(shared.BROADCAST_RATE_LIMIT_PER_MIN + 5):
                ws_ctrl = await _connect(port)
                try:
                    await _send_op(
                        ws_ctrl, "hello",
                        session_id=str(uuid.uuid4()), name="", label="",
                        cwd="/tmp", pid=1, role="control",
                        for_session=sid, nonce=listener_nonce, token=token,
                    )
                    welcome = await _recv(ws_ctrl)
                    if welcome["op"] == "error":
                        break
                    await _send_op(ws_ctrl, "broadcast", text=f"rl#{i}")
                    try:
                        resp = await _recv(ws_ctrl, timeout=0.3)
                        if resp.get("op") == "error" and resp.get("code") == shared.ErrorCode.RATE_LIMITED:
                            errs += 1
                    except asyncio.TimeoutError:
                        successes += 1
                finally:
                    await ws_ctrl.close()
            assert errs >= 1, f"rate limit never fired ({errs} errs / {successes} successes)"
        finally:
            await ws_listener.close()
            await ws_peer.close()


class TestRename:
    async def test_valid_rename(self, running_server):
        srv, port, token = running_server
        ws_a = await _connect(port)
        ws_b = await _connect(port)
        try:
            sid_a, _ = await _hello(ws_a, token, name="alpha")
            await _hello(ws_b, token, name="beta")
            await _send_op(ws_a, "rename", name="alpha2")
            ack = await _recv_until(ws_a, "renamed")
            assert ack["name"] == "alpha2"
            event = await _recv_until(ws_b, "renamed")
            assert event["session_id"] == sid_a
        finally:
            await ws_a.close()
            await ws_b.close()

    async def test_invalid_rename(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _hello(ws, token, name="alpha")
            await _send_op(ws, "rename", name="Bad Name")
            err = await _recv(ws)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.INVALID_NAME
        finally:
            await ws.close()


class TestRelabel:
    async def test_valid_relabel_updates_and_broadcasts(self, running_server):
        srv, port, token = running_server
        ws_a = await _connect(port)
        ws_b = await _connect(port)
        try:
            sid_a, _ = await _hello(ws_a, token, name="alpha", label="old")
            await _hello(ws_b, token, name="beta")
            await _send_op(ws_a, "relabel", label="the controller")
            ack = await _recv_until(ws_a, "relabeled")
            assert ack["label"] == "the controller"
            # Peer sees the live event referencing the SAME session_id (no reconnect).
            event = await _recv_until(ws_b, "relabeled")
            assert event["session_id"] == sid_a
            assert event["name"] == "alpha"
            assert event["label"] == "the controller"
            # list reflects the new label.
            await _send_op(ws_b, "list")
            resp = await _recv_until(ws_b, "list_ok")
            alpha = next(s for s in resp["sessions"] if s["session_id"] == sid_a)
            assert alpha["label"] == "the controller"
        finally:
            await ws_a.close()
            await ws_b.close()

    async def test_relabel_empty_clears(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            sid, _ = await _hello(ws, token, name="alpha", label="old")
            await _send_op(ws, "relabel", label="")
            ack = await _recv_until(ws, "relabeled")
            assert ack["label"] == ""
            await _send_op(ws, "list")
            resp = await _recv_until(ws, "list_ok")
            me = next(s for s in resp["sessions"] if s["session_id"] == sid)
            assert me["label"] == ""
        finally:
            await ws.close()

    async def test_invalid_label_rejected(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _hello(ws, token, name="alpha")
            await _send_op(ws, "relabel", label="a\nb")  # newline is invalid
            err = await _recv(ws)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.INVALID_LABEL
        finally:
            await ws.close()

    async def test_relabel_non_string(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _hello(ws, token, name="alpha")
            await _send_op(ws, "relabel", label=42)
            err = await _recv(ws)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.INVALID_PAYLOAD
        finally:
            await ws.close()

    async def test_control_relabel_mutates_listener(self, running_server):
        srv, port, token = running_server
        sid = str(uuid.uuid4())
        nonce = "ctl-relabel-nonce"
        ws_listener = await _connect(port)
        await _send_op(
            ws_listener, "hello", session_id=sid, name="alpha", label="",
            cwd="/tmp", pid=1, role="agent", nonce=nonce, token=token,
        )
        await _recv(ws_listener)
        ws_ctrl = await _connect(port)
        await _send_op(
            ws_ctrl, "hello", session_id=str(uuid.uuid4()), name="", label="",
            cwd="/tmp", pid=2, role="control", for_session=sid, nonce=nonce, token=token,
        )
        await _recv(ws_ctrl)  # welcome
        ws_watcher = await _connect(port)
        await _hello(ws_watcher, token, name="watcher")
        try:
            for _ in range(2):
                try:
                    await _recv(ws_watcher, timeout=0.3)
                except asyncio.TimeoutError:
                    break
            # Control relabels — must mutate the LISTENER, not the control conn.
            await _send_op(ws_ctrl, "relabel", label="ctl-label")
            ack = await _recv_until(ws_ctrl, "relabeled")
            assert ack["label"] == "ctl-label"
            event = await _recv_until(ws_watcher, "relabeled")
            assert event["session_id"] == sid
            assert event["name"] == "alpha"
            assert event["label"] == "ctl-label"
        finally:
            await ws_listener.close()
            await ws_ctrl.close()
            await ws_watcher.close()


class TestReconnect:
    async def test_same_session_id_replaces(self, running_server):
        srv, port, token = running_server
        ws1 = await _connect(port)
        sid, _ = await _hello(ws1, token, name="alpha")
        # Disconnect rudely
        await ws1.close()
        await asyncio.sleep(0.1)
        # Reconnect with the same session_id
        ws2 = await _connect(port)
        try:
            await _send_op(
                ws2, "hello",
                session_id=sid, name="alpha", label="",
                cwd="/tmp", pid=1, role="agent", token=token,
            )
            welcome = await _recv(ws2)
            assert welcome["op"] == "welcome"
            assert welcome["session_id"] == sid
            # list should show exactly one
            await _send_op(ws2, "list")
            resp = await _recv(ws2)
            # NB: the connecting client doesn't appear in its own list (it's an agent though),
            # so list should show 0 OR include the session itself depending on server policy.
            # Assert no duplicate sessions with the same sid.
            ids = [s["session_id"] for s in resp["sessions"]]
            assert ids.count(sid) <= 1
        finally:
            await ws2.close()


class TestPing:
    async def test_pong(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _hello(ws, token, name="alpha")
            await _send_op(ws, "ping")
            resp = await _recv(ws)
            assert resp["op"] == "pong"
        finally:
            await ws.close()


class TestSendByShortSessionId:
    """Round-11 fix: list.py displays short session_id; users naturally try
    to copy that into `send --to`. The server now matches session_id prefix
    (≥4 chars) when it doesn't match an exact name."""

    async def test_send_by_session_id_prefix(self, running_server):
        srv, port, token = running_server
        ws_a = await _connect(port)
        ws_b = await _connect(port)
        try:
            sid_a, _ = await _hello(ws_a, token, name="alpha")
            sid_b, _ = await _hello(ws_b, token, name="beta")
            try:
                await _recv(ws_b, timeout=0.5)
            except asyncio.TimeoutError:
                pass
            # Use first 8 chars of session_id (what list.py shows)
            short = sid_b[:8]
            await _send_op(ws_a, "send", to=short, text="via short id")
            msg = await _recv_until(ws_b, "msg")
            assert msg["text"] == "via short id"
        finally:
            await ws_a.close()
            await ws_b.close()


class TestListListenerLiveness:
    """Round-11 fix: control connection cannot enumerate peers after its
    listener has disconnected."""

    async def test_list_rejected_after_listener_gone(self, running_server):
        srv, port, token = running_server
        sid = str(uuid.uuid4())
        nonce = "list-liveness-nonce"
        ws_listener = await _connect(port)
        await _send_op(
            ws_listener, "hello",
            session_id=sid, name="alpha", label="",
            cwd="/tmp", pid=1, role="agent",
            nonce=nonce, token=token,
        )
        await _recv(ws_listener)
        ws_ctrl = await _connect(port)
        await _send_op(
            ws_ctrl, "hello",
            session_id=str(uuid.uuid4()), name="", label="",
            cwd="/tmp", pid=2, role="control",
            for_session=sid, nonce=nonce, token=token,
        )
        await _recv(ws_ctrl)
        try:
            await ws_listener.close()
            await asyncio.sleep(0.3)
            await _send_op(ws_ctrl, "list")
            err = await _recv_until(ws_ctrl, "error")
            assert err["code"] == shared.ErrorCode.UNAUTHORIZED
        finally:
            await ws_ctrl.close()


class TestControlAfterListenerGone:
    """Round-9 fix: a control connection can NOT keep sending messages with
    the listener's identity after the listener disconnects."""

    async def test_control_send_rejected_after_listener_gone(self, running_server):
        srv, port, token = running_server
        sid = str(uuid.uuid4())
        nonce = "post-disconnect-nonce"
        ws_listener = await _connect(port)
        await _send_op(
            ws_listener, "hello",
            session_id=sid, name="alpha", label="",
            cwd="/tmp", pid=1, role="agent",
            nonce=nonce, token=token,
        )
        await _recv(ws_listener)
        # Establish control connection while listener is alive
        ws_ctrl = await _connect(port)
        await _send_op(
            ws_ctrl, "hello",
            session_id=str(uuid.uuid4()), name="", label="",
            cwd="/tmp", pid=2, role="control",
            for_session=sid, nonce=nonce, token=token,
        )
        await _recv(ws_ctrl)  # welcome
        # Spawn another agent to be a target
        ws_target = await _connect(port)
        await _hello(ws_target, token, name="target")
        try:
            # Listener disconnects (graceful close)
            await ws_listener.close()
            # Wait for server to unregister
            await asyncio.sleep(0.3)
            # Now control tries to send via the (gone) listener
            await _send_op(ws_ctrl, "send", to="target", text="should-fail")
            err = await _recv_until(ws_ctrl, "error")
            assert err["code"] == shared.ErrorCode.UNAUTHORIZED
        finally:
            await ws_ctrl.close()
            await ws_target.close()

    async def test_control_broadcast_rejected_after_listener_gone(self, running_server):
        srv, port, token = running_server
        sid = str(uuid.uuid4())
        nonce = "post-disconnect-nonce-2"
        ws_listener = await _connect(port)
        await _send_op(
            ws_listener, "hello",
            session_id=sid, name="alpha", label="",
            cwd="/tmp", pid=1, role="agent",
            nonce=nonce, token=token,
        )
        await _recv(ws_listener)
        ws_ctrl = await _connect(port)
        await _send_op(
            ws_ctrl, "hello",
            session_id=str(uuid.uuid4()), name="", label="",
            cwd="/tmp", pid=2, role="control",
            for_session=sid, nonce=nonce, token=token,
        )
        await _recv(ws_ctrl)
        try:
            await ws_listener.close()
            await asyncio.sleep(0.3)
            await _send_op(ws_ctrl, "broadcast", text="should-fail")
            err = await _recv_until(ws_ctrl, "error")
            assert err["code"] == shared.ErrorCode.UNAUTHORIZED
        finally:
            await ws_ctrl.close()


class TestControlRoleRename:
    """Round-8 fix: rename via a control connection used to mutate the
    transient control state instead of the registered listener."""

    async def test_control_rename_mutates_listener(self, running_server):
        srv, port, token = running_server
        sid = str(uuid.uuid4())
        nonce = "ctl-rename-nonce"
        # Listener
        ws_listener = await _connect(port)
        await _send_op(
            ws_listener, "hello",
            session_id=sid, name="alpha", label="",
            cwd="/tmp", pid=1, role="agent",
            nonce=nonce, token=token,
        )
        await _recv(ws_listener)
        # Control
        ws_ctrl = await _connect(port)
        await _send_op(
            ws_ctrl, "hello",
            session_id=str(uuid.uuid4()), name="", label="",
            cwd="/tmp", pid=2, role="control",
            for_session=sid, nonce=nonce, token=token,
        )
        await _recv(ws_ctrl)  # welcome
        # Watcher
        ws_watcher = await _connect(port)
        await _hello(ws_watcher, token, name="watcher")
        try:
            # Drain peer_joined chatter on watcher
            for _ in range(2):
                try:
                    await _recv(ws_watcher, timeout=0.3)
                except asyncio.TimeoutError:
                    break
            # Control issues rename — should mutate the listener entry, not control.
            await _send_op(ws_ctrl, "rename", name="alpha2")
            ack = await _recv_until(ws_ctrl, "renamed")
            assert ack["name"] == "alpha2"
            # The watcher's renamed event should reference the LISTENER's session_id,
            # not the control connection's session_id.
            event = await _recv_until(ws_watcher, "renamed")
            assert event["session_id"] == sid
            assert event["name"] == "alpha2"
            # Verify list reflects the new name
            await _send_op(ws_watcher, "list")
            resp = await _recv_until(ws_watcher, "list_ok")
            names = {s["name"] for s in resp["sessions"]}
            assert "alpha2" in names
            assert "alpha" not in names
        finally:
            await ws_listener.close()
            await ws_ctrl.close()
            await ws_watcher.close()


class TestNonDictPayload:
    """Round-7 fix: a JSON array/string at the top-level used to crash the
    handler with AttributeError. Now it's rejected with INVALID_PAYLOAD."""

    async def test_first_frame_array_rejected(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await ws.send(json.dumps([1, 2, 3]))
            err = await _recv(ws)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.INVALID_PAYLOAD
        finally:
            await ws.close()

    async def test_first_frame_string_rejected(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await ws.send(json.dumps("just a string"))
            err = await _recv(ws)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.INVALID_PAYLOAD
        finally:
            await ws.close()

    async def test_dispatch_array_rejected(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _hello(ws, token, name="alpha")
            await ws.send(json.dumps([1, 2, 3]))
            err = await _recv_until(ws, "error")
            assert err["code"] == shared.ErrorCode.INVALID_PAYLOAD
        finally:
            await ws.close()


class TestSessionIdHijackPrevention:
    """Round-6 fix: another token holder can NOT seize an existing
    listener's session_id without knowing its nonce."""

    async def test_reconnect_with_correct_nonce_replaces(self, running_server):
        srv, port, token = running_server
        sid = str(uuid.uuid4())
        nonce = "shared-nonce-X"
        ws1 = await _connect(port)
        await _send_op(
            ws1, "hello",
            session_id=sid, name="alpha", label="",
            cwd="/tmp", pid=1, role="agent",
            nonce=nonce, token=token,
        )
        await _recv(ws1)  # welcome
        # Reconnect with the SAME session_id and nonce — should replace
        ws2 = await _connect(port)
        try:
            await _send_op(
                ws2, "hello",
                session_id=sid, name="alpha", label="",
                cwd="/tmp", pid=2, role="agent",
                nonce=nonce, token=token,
            )
            welcome = await _recv(ws2)
            assert welcome["op"] == "welcome"
            assert welcome["session_id"] == sid
        finally:
            await ws1.close()
            await ws2.close()

    async def test_hijack_with_wrong_nonce_rejected(self, running_server):
        srv, port, token = running_server
        sid = str(uuid.uuid4())
        nonce = "real-nonce"
        ws_real = await _connect(port)
        await _send_op(
            ws_real, "hello",
            session_id=sid, name="alpha", label="",
            cwd="/tmp", pid=1, role="agent",
            nonce=nonce, token=token,
        )
        await _recv(ws_real)  # welcome
        # Attacker discovers sid (from list response), tries to hijack with no
        # / wrong nonce. Should be rejected; legit listener untouched.
        ws_attacker = await _connect(port)
        try:
            await _send_op(
                ws_attacker, "hello",
                session_id=sid, name="evil", label="",
                cwd="/tmp", pid=2, role="agent",
                nonce="guessed-wrong", token=token,
            )
            err = await _recv(ws_attacker)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.UNAUTHORIZED
            # The original listener is still up: a list call from another
            # client should still see it.
            ws_check = await _connect(port)
            await _hello(ws_check, token, name="checker")
            try:
                await _send_op(ws_check, "list")
                resp = await _recv_until(ws_check, "list_ok")
                ids = {s["session_id"] for s in resp["sessions"]}
                assert sid in ids, "legit listener was wrongly evicted"
            finally:
                await ws_check.close()
        finally:
            await ws_attacker.close()
            await ws_real.close()


class TestCwdSanitization:
    """Round-6 fix: cwd from hello is sanitized server-side so list output
    can't be poisoned with terminal escape sequences."""

    async def test_cwd_with_ansi_is_stripped(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            evil = "/tmp/\x1b[31mEVIL\x1b[0m\nstuff"
            await _send_op(
                ws, "hello",
                session_id=str(uuid.uuid4()), name="alpha", label="",
                cwd=evil, pid=1, role="agent",
                nonce="x", token=token,
            )
            welcome = await _recv(ws)
            assert welcome["op"] == "welcome"
            await _send_op(ws, "list")
            resp = await _recv_until(ws, "list_ok")
            cwds = [s["cwd"] for s in resp["sessions"] if s["name"] == "alpha"]
            assert cwds, "alpha not in list"
            # No ANSI escape, no newline
            assert "\x1b" not in cwds[0]
            assert "\n" not in cwds[0]
        finally:
            await ws.close()


class TestPayloadTypeSafety:
    """Regression: hello/send/broadcast used to crash the handler when given
    non-string fields (e.g., name=null, text=[1,2]). Now they reject with
    INVALID_PAYLOAD."""

    async def test_hello_non_string_name(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _send_op(
                ws, "hello",
                session_id=str(uuid.uuid4()), name=123, label="",
                cwd="/tmp", pid=1, role="agent", token=token,
            )
            err = await _recv(ws)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.INVALID_PAYLOAD
        finally:
            await ws.close()

    async def test_hello_non_string_label(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _send_op(
                ws, "hello",
                session_id=str(uuid.uuid4()), name="alpha", label=None,
                cwd="/tmp", pid=1, role="agent", token=token,
            )
            err = await _recv(ws)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.INVALID_PAYLOAD
        finally:
            await ws.close()

    async def test_hello_pid_string_coerced(self, running_server):
        """pid as string should be silently accepted (coerced to int 0 on failure)
        rather than crashing the handler."""
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _send_op(
                ws, "hello",
                session_id=str(uuid.uuid4()), name="alpha", label="",
                cwd="/tmp", pid="not-a-number", role="agent", token=token,
            )
            welcome = await _recv(ws)
            assert welcome["op"] == "welcome"
        finally:
            await ws.close()

    async def test_send_non_string_text(self, running_server):
        srv, port, token = running_server
        ws_a = await _connect(port)
        ws_b = await _connect(port)
        try:
            await _hello(ws_a, token, name="alpha")
            await _hello(ws_b, token, name="beta")
            await _send_op(ws_a, "send", to="beta", text=["this", "is", "a", "list"])
            err = await _recv_until(ws_a, "error")
            assert err["code"] == shared.ErrorCode.INVALID_PAYLOAD
        finally:
            await ws_a.close()
            await ws_b.close()

    async def test_broadcast_non_string_text(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _hello(ws, token, name="alpha")
            await _send_op(ws, "broadcast", text={"some": "dict"})
            err = await _recv_until(ws, "error")
            assert err["code"] == shared.ErrorCode.INVALID_PAYLOAD
        finally:
            await ws.close()

    async def test_rename_non_string(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _hello(ws, token, name="alpha")
            await _send_op(ws, "rename", name=42)
            err = await _recv_until(ws, "error")
            assert err["code"] == shared.ErrorCode.INVALID_PAYLOAD
        finally:
            await ws.close()


class TestUnknownOp:
    async def test_returns_error(self, running_server):
        srv, port, token = running_server
        ws = await _connect(port)
        try:
            await _hello(ws, token, name="alpha")
            await _send_op(ws, "do-the-thing")
            err = await _recv(ws)
            assert err["op"] == "error"
            assert err["code"] == shared.ErrorCode.UNKNOWN_OP
        finally:
            await ws.close()


class TestPeerEvents:
    async def test_peer_joined_and_left(self, running_server):
        srv, port, token = running_server
        ws_a = await _connect(port)
        await _hello(ws_a, token, name="alpha")
        ws_b = await _connect(port)
        try:
            sid_b, _ = await _hello(ws_b, token, name="beta")
            joined = await _recv(ws_a)
            assert joined["op"] == "peer_joined"
            assert joined["session_id"] == sid_b
            await ws_b.close()
            left = await _recv(ws_a, timeout=5.0)
            assert left["op"] == "peer_left"
            assert left["session_id"] == sid_b
        finally:
            await ws_a.close()
