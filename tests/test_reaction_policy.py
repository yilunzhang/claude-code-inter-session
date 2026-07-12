"""Static checks on SKILL.md so prose edits don't drop critical guardrails."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = (REPO / "skills" / "inter-session" / "SKILL.md").read_text()


class TestFrontmatter:
    def test_name(self):
        assert "name: inter-session" in SKILL

    def test_allowed_tools(self):
        assert "allowed-tools" in SKILL
        for tool in ("Bash", "Monitor", "TaskList", "TaskStop"):
            assert tool in SKILL


class TestSubcommands:
    def test_dispatch_table_has_all_subcommands(self):
        for sub in ("connect", "install-deps", "list", "send", "broadcast",
                    "rename", "status", "disconnect"):
            assert f"`/inter-session {sub}" in SKILL or f"`/inter-session [args]" in SKILL


class TestReactionPolicy:
    def test_treats_messages_as_instructions_by_default(self):
        # The whole point of this skill is agent-to-agent automation.
        assert "act on it" in SKILL.lower() or "instruction from" in SKILL.lower()

    def test_destructive_ops_require_explicit_content(self):
        for kw in ("rm -rf", "git push --force", "DROP TABLE", "kubectl delete"):
            assert kw in SKILL

    def test_loop_suppression_with_done_status_answer(self):
        assert "`done:" in SKILL
        assert "`status:" in SKILL
        assert "`answer:" in SKILL
        assert "`question:" in SKILL

    def test_permission_rules_not_overridden(self):
        # The single most important guardrail: peers can't escalate.
        assert "do NOT override" in SKILL or "do not override" in SKILL.lower()

    def test_only_leading_header_is_authoritative(self):
        # SEC-002: a peer can embed `[inter-session ...]`-looking text in the
        # message body. The reaction policy must tell the agent that only the
        # leading prefix is authoritative, so an embedded pseudo-header can't
        # spoof the sender or inject a second directive. Anchor on distinctive
        # phrases from the guardrail bullet so deleting it fails the test even
        # if the individual words survive elsewhere in the prose.
        low = SKILL.lower()
        assert "only the leading" in low
        assert "authoritative" in low
        assert "pseudo-header" in low


class TestInstallDepsUx:
    def test_uses_isolated_venv_by_default(self):
        """install-deps must default to a project-local venv at
        ~/.claude/data/inter-session/venv so it never touches the user's
        system or user-level Python."""
        assert "~/.claude/data/inter-session/venv" in SKILL
        assert "python3 -m venv" in SKILL or "uv venv" in SKILL

    def test_offers_uv_as_optional(self):
        """uv is the optional fast path; user can also stay on stdlib venv."""
        assert "uv" in SKILL

    def test_no_user_or_system_pip_pollution(self):
        """The old --user / --system / --break-system-packages paths
        modified user-visible Python and were dropped in favor of the
        isolated venv."""
        assert "pip install --user" not in SKILL
        assert "pip install --system" not in SKILL
        assert "--break-system-packages" not in SKILL


class TestDedupGuard:
    def test_tasklist_check_in_connect(self):
        assert "TaskList()" in SKILL
        assert '"inter-session messages"' in SKILL


class TestNameValidation:
    def test_ascii_regex_in_skill(self):
        assert "[a-z0-9]" in SKILL


class TestTruncationHandling:
    def test_messages_log_pointer_documented(self):
        assert "messages.log" in SKILL
