"""Every awgit test gets its own vcs data root.

Tests that install awgit's real hooks into a throwaway repo (bridge.install_hooks)
run the post-commit capture in a git subprocess that inherits os.environ. Without
VCS_DATA_ROOT that capture writes into the developer's real op-log at
~/.aither/awgit/data: measured 2026-10-15, one suite run added 80 ops for temp-repo
shas (f.py, m.py, ...) that exist in no repository, and `awgit evidence` counted
them. A test that wants a specific root still sets it; monkeypatch keeps the last.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolated_vcs_data_root(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path_factory.mktemp("vcs-data")))
