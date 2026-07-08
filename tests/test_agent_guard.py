"""Tests for the agent-context auto-connect guard (client._in_agent_context).

The plugin's monitors.json auto-start fires in EVERY Claude Code process,
including agent-team teammates / subagents spawned as child claude processes
(`claude --agent-id worker@team --parent-session-id <sid> ...`). Each worker
then auto-joins the bus with a cwd-derived name — roster pollution — and a
broadcast reaches mid-task workers as an instruction. The guard detects a
claude agent ancestor and skips the auto-start path only; explicit --name
connects still work.
"""
import sys
import types

from bin import client


class _FakeProc:
    """Minimal psutil.Process stand-in. `cmdline` may be a list or an
    exception instance (raised on access, like psutil.AccessDenied)."""

    def __init__(self, cmdline, parent=None, pid=1234):
        self._cmdline = cmdline
        self._parent = parent
        self.pid = pid

    def cmdline(self):
        if isinstance(self._cmdline, Exception):
            raise self._cmdline
        return self._cmdline

    def parent(self):
        return self._parent


def _fake_psutil(ancestor_cmdlines):
    """Fake psutil module whose Process().parent() chain yields
    `ancestor_cmdlines`, nearest ancestor first."""
    top = None
    for cmd in reversed(ancestor_cmdlines):
        top = _FakeProc(cmd, parent=top)
    mod = types.ModuleType("psutil")

    class _Self:
        def parent(self):
            return top

    mod.Process = lambda *a, **k: _Self()
    return mod


class TestAgentContextGuard:
    def test_detects_claude_ancestor_with_agent_flags(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([
            ["/bin/zsh", "-c", "python3 client.py"],
            ["claude", "--agent-id", "worker-1@session-abc",
             "--parent-session-id", "abc"],
        ]))
        assert client._in_agent_context() is True

    def test_detects_equals_form_flag(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([
            ["claude", "--agent-id=worker-1@session-abc"],
        ]))
        assert client._in_agent_context() is True

    def test_detects_versioned_binary_path(self, monkeypatch):
        # Observed live shape: teammates spawn with the versioned install
        # path as argv[0] (basename "2.1.204"), not "claude".
        monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([
            ["/home/u/.local/share/claude/versions/2.1.204",
             "--agent-id", "w@s", "--parent-session-id", "s"],
        ]))
        assert client._in_agent_context() is True

    def test_non_claude_binary_with_flag_tokens_does_not_match(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([
            ["/usr/bin/somethingelse", "--agent-id", "w@s"],
        ]))
        assert client._in_agent_context() is False

    def test_plain_interactive_claude_does_not_match(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([
            ["/bin/zsh", "-c", "..."],
            ["claude"],
            ["-zsh"],
        ]))
        assert client._in_agent_context() is False

    def test_shell_quoting_flag_text_does_not_match(self, monkeypatch):
        # A shell ancestor merely QUOTING the flag text (e.g. a test
        # command or a script echoing it) must not trip the guard.
        monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([
            ["/bin/bash", "-c", "echo claude --agent-id x@y"],
            ["claude"],
        ]))
        assert client._in_agent_context() is False

    def test_accessdenied_ancestor_does_not_stop_walk(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([
            RuntimeError("AccessDenied"),  # e.g. login/launchd
            ["claude", "--agent-id", "w@s"],
        ]))
        assert client._in_agent_context() is True

    def test_fail_open_without_psutil(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "psutil", None)
        assert client._in_agent_context() is False

    def test_walk_depth_bounded(self, monkeypatch):
        # A matching ancestor beyond the depth cap is not reached.
        chain = [["/bin/sh", "-c", "x"]] * 20 + [["claude", "--agent-id", "w@s"]]
        monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(chain))
        assert client._in_agent_context() is False
