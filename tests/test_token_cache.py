import os
import stat

import pytest

from pywaylandauto.token_cache import TokenCache


def test_save_load_round_trip(tmp_path):
    cache = TokenCache(str(tmp_path / "sub" / "portal.token"))
    cache.save("2b6ecc53-51c3-4263-aff1-d81f4796c0b4")
    assert cache.load() == "2b6ecc53-51c3-4263-aff1-d81f4796c0b4"


def test_file_and_dir_modes(tmp_path):
    path = tmp_path / "portal.token"
    TokenCache(str(path)).save("abc")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700


def test_load_missing_returns_none(tmp_path):
    assert TokenCache(str(tmp_path / "nope" / "token")).load() is None


def test_load_empty_file_returns_none(tmp_path):
    path = tmp_path / "token"
    path.write_text("   \n")
    assert TokenCache(str(path)).load() is None


def test_rotation_overwrites(tmp_path):
    path = str(tmp_path / "token")
    cache = TokenCache(path)
    cache.save("old-token")
    cache.save("new-token")
    assert cache.load() == "new-token"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    cache = TokenCache(str(tmp_path / "token"))
    for i in range(5):
        cache.save(f"token-{i}")
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".token-")]
    assert leftovers == []


def test_revoke(tmp_path):
    path = str(tmp_path / "token")
    cache = TokenCache(path)
    cache.save("abc")
    cache.revoke()
    assert cache.load() is None
    cache.revoke()  # idempotent


def test_refuses_empty_token(tmp_path):
    with pytest.raises(ValueError):
        TokenCache(str(tmp_path / "token")).save("  ")


def test_env_var_override(tmp_path, monkeypatch):
    path = str(tmp_path / "ci-token")
    monkeypatch.setenv("PYWAYLANDAUTO_TOKEN_FILE", path)
    cache = TokenCache()
    cache.save("ci-grant")
    assert TokenCache().load() == "ci-grant"
