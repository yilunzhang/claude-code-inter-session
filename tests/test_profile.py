"""Per-project label persistence (bin/profile.py)."""

from __future__ import annotations

import json

import pytest

from bin import profile, shared


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    d = tmp_path / "inter-session"
    monkeypatch.setenv("INTER_SESSION_DATA_DIR", str(d))
    return d


class TestProjectRoot:
    def test_git_ancestor_is_root(self, tmp_path):
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        assert profile.project_root(str(sub)) == str(tmp_path.resolve())

    def test_git_file_worktree_is_root(self, tmp_path):
        # Worktrees/submodules use a `.git` *file*, not a directory.
        (tmp_path / ".git").write_text("gitdir: /elsewhere")
        sub = tmp_path / "x"
        sub.mkdir()
        assert profile.project_root(str(sub)) == str(tmp_path.resolve())

    def test_no_repo_falls_back_to_cwd(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        assert profile.project_root(str(d)) == str(d.resolve())

    def test_same_repo_subdirs_share_profile(self, tmp_path):
        (tmp_path / ".git").mkdir()
        a = tmp_path / "a"
        b = tmp_path / "a" / "deep" / "nested"
        b.mkdir(parents=True)
        a.mkdir(exist_ok=True)
        assert profile.profile_path(str(a)) == profile.profile_path(str(b))


class TestLoadSave:
    def test_load_missing_is_empty(self, tmp_data_dir, tmp_path):
        assert profile.load_label(str(tmp_path)) == ""

    def test_save_then_load(self, tmp_data_dir, tmp_path):
        profile.save_label("Payments 🐛 v2", str(tmp_path))
        assert profile.load_label(str(tmp_path)) == "Payments 🐛 v2"

    def test_save_empty_clears(self, tmp_data_dir, tmp_path):
        profile.save_label("keep", str(tmp_path))
        profile.save_label("", str(tmp_path))
        assert profile.load_label(str(tmp_path)) == ""

    def test_profile_file_mode_0600(self, tmp_data_dir, tmp_path):
        import os
        import stat
        profile.save_label("x", str(tmp_path))
        p = profile.profile_path(str(tmp_path))
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600

    def test_invalid_stored_label_ignored(self, tmp_data_dir, tmp_path):
        # A hand-edited/corrupt file with a label the live path would reject
        # must not surface it.
        shared.secure_dir(profile.profiles_dir())
        p = profile.profile_path(str(tmp_path))
        p.write_text(json.dumps({"label": "bad\nlabel"}))  # newline is invalid
        assert profile.load_label(str(tmp_path)) == ""

    def test_corrupt_json_ignored(self, tmp_data_dir, tmp_path):
        shared.secure_dir(profile.profiles_dir())
        profile.profile_path(str(tmp_path)).write_text("{not json")
        assert profile.load_label(str(tmp_path)) == ""

    def test_invalid_label_not_saved(self, tmp_data_dir, tmp_path):
        profile.save_label("a\nb", str(tmp_path))
        assert not profile.profile_path(str(tmp_path)).exists()

    def test_save_preserves_other_keys(self, tmp_data_dir, tmp_path):
        shared.secure_dir(profile.profiles_dir())
        p = profile.profile_path(str(tmp_path))
        p.write_text(json.dumps({"team": "payments", "label": "old"}))
        profile.save_label("new", str(tmp_path))
        data = json.loads(p.read_text())
        assert data["label"] == "new"
        assert data["team"] == "payments"  # forward-compat field untouched
        assert "updated_at" in data and "path" in data


class TestLabelLengthBoundary:
    def test_save_and_load_at_max_length(self, tmp_data_dir, tmp_path):
        label = "a" * shared.LABEL_MAX_CP  # exactly 60 — valid
        profile.save_label(label, str(tmp_path))
        assert profile.load_label(str(tmp_path)) == label

    def test_save_over_max_length_is_noop(self, tmp_data_dir, tmp_path):
        # A label longer than the maximum must not be persisted at all.
        over = "a" * (shared.LABEL_MAX_CP + 1)  # 61 — invalid
        profile.save_label(over, str(tmp_path))
        assert not profile.profile_path(str(tmp_path)).exists()
        assert profile.load_label(str(tmp_path)) == ""

    def test_save_over_max_does_not_overwrite_existing(self, tmp_data_dir, tmp_path):
        profile.save_label("keep", str(tmp_path))
        profile.save_label("a" * (shared.LABEL_MAX_CP + 1), str(tmp_path))
        assert profile.load_label(str(tmp_path)) == "keep"  # prior value survives

    def test_load_ignores_over_max_length_on_disk(self, tmp_data_dir, tmp_path):
        # A hand-edited file with an over-length label is ignored on load.
        shared.secure_dir(profile.profiles_dir())
        profile.profile_path(str(tmp_path)).write_text(
            json.dumps({"label": "a" * (shared.LABEL_MAX_CP + 1)})
        )
        assert profile.load_label(str(tmp_path)) == ""


class TestMalformedProfile:
    def test_load_non_dict_json_ignored(self, tmp_data_dir, tmp_path):
        shared.secure_dir(profile.profiles_dir())
        profile.profile_path(str(tmp_path)).write_text(json.dumps(["not", "a", "dict"]))
        assert profile.load_label(str(tmp_path)) == ""

    def test_load_non_string_label_ignored(self, tmp_data_dir, tmp_path):
        shared.secure_dir(profile.profiles_dir())
        profile.profile_path(str(tmp_path)).write_text(json.dumps({"label": 123}))
        assert profile.load_label(str(tmp_path)) == ""

    def test_load_missing_label_key(self, tmp_data_dir, tmp_path):
        shared.secure_dir(profile.profiles_dir())
        profile.profile_path(str(tmp_path)).write_text(json.dumps({"team": "x"}))
        assert profile.load_label(str(tmp_path)) == ""


class TestResolveLabel:
    def test_none_loads_persisted(self, tmp_data_dir, tmp_path):
        profile.save_label("stored", str(tmp_path))
        assert profile.resolve_label(None, str(tmp_path)) == "stored"

    def test_none_with_nothing_persisted_is_empty(self, tmp_data_dir, tmp_path):
        assert profile.resolve_label(None, str(tmp_path)) == ""

    def test_explicit_persists_and_returns(self, tmp_data_dir, tmp_path):
        assert profile.resolve_label("set-me", str(tmp_path)) == "set-me"
        assert profile.load_label(str(tmp_path)) == "set-me"  # persisted

    def test_explicit_empty_clears_and_returns_empty(self, tmp_data_dir, tmp_path):
        profile.save_label("old", str(tmp_path))
        assert profile.resolve_label("", str(tmp_path)) == ""
        assert profile.load_label(str(tmp_path)) == ""  # cleared

    def test_explicit_overrides_persisted(self, tmp_data_dir, tmp_path):
        profile.save_label("old", str(tmp_path))
        assert profile.resolve_label("new", str(tmp_path)) == "new"
        assert profile.load_label(str(tmp_path)) == "new"
