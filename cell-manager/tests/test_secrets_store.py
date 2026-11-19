"""FileSecretStore 文件托管（无网、无 PG）：三件套与失败指引。

覆盖：put/get/delete 正常路径、逻辑路径→文件名映射、0600、缺失→None、
delete 幂等、路径逃逸拒绝、损坏 JSON 防御、错误消息不含条目明文、
写失败的 CELL_SECRETS_DIR 指引（原 OpenBAO 客户端测试等价形态）。
"""

from __future__ import annotations

import json
import stat

import pytest

from cell_manager.secrets_store import FileSecretStore, SecretsStoreError


@pytest.fixture
def store(tmp_path):
    return FileSecretStore(tmp_path / "secrets")


class TestHappyPaths:
    def test_put_get_roundtrip_and_file_shape(self, store):
        store.put("wire/cell-db/acme", {"url": "postgresql://x", "password": "pw1"})

        entry_file = store.dir / "wire-cell-db-acme.json"
        assert entry_file.is_file()
        # 0600（组/其他零权限）
        mode = stat.S_IMODE(entry_file.stat().st_mode)
        assert mode == 0o600
        assert json.loads(entry_file.read_text()) == {
            "url": "postgresql://x", "password": "pw1"
        }
        assert store.get("wire/cell-db/acme") == {
            "url": "postgresql://x", "password": "pw1"
        }
        # 原子写不留临时文件
        assert sorted(p.name for p in store.dir.iterdir()) == ["wire-cell-db-acme.json"]

    def test_put_overwrites_existing_entry(self, store):
        store.put("wire/cell-db/acme", {"password": "pw1"})
        store.put("wire/cell-db/acme", {"password": "pw2"})
        assert store.get("wire/cell-db/acme") == {"password": "pw2"}

    def test_get_missing_returns_none(self, store):
        assert store.get("wire/cell-db/ghost") is None

    def test_delete_is_idempotent_when_missing(self, store):
        store.delete("wire/cell-db/never-existed")  # 不抛
        store.delete("wire/cell-db/never-existed")  # 再删一次也不抛

    def test_delete_removes_entry(self, store):
        store.put("wire/cell-db/acme", {"password": "pw1"})
        store.delete("wire/cell-db/acme")
        assert store.get("wire/cell-db/acme") is None


class TestPathValidation:
    @pytest.mark.parametrize(
        "path",
        [
            "/wire/cell-db/acme",  # 绝对路径
            "wire/../escape",
            "wire/./cell-db/acme",  # 空段/点段
            "wire//cell-db",
            "",
            "wire/cell db/acme",  # 空格
        ],
    )
    def test_unsafe_logical_path_rejected(self, store, path):
        with pytest.raises(SecretsStoreError, match="invalid credential path"):
            store.get(path)
        with pytest.raises(SecretsStoreError, match="invalid credential path"):
            store.put(path, {"password": "x"})
        # 目录里没落任何文件
        assert not store.dir.exists() or list(store.dir.iterdir()) == []


class TestErrors:
    def test_corrupt_json_raises_with_guidance(self, store):
        store.put("wire/cell-db/acme", {"password": "pw1"})
        target = store.dir / "wire-cell-db-acme.json"
        target.write_bytes(b"{not json")
        with pytest.raises(SecretsStoreError, match="unreadable/corrupt"):
            store.get("wire/cell-db/acme")

    def test_non_object_json_raises_with_guidance(self, store):
        store.put("wire/cell-db/acme", {"password": "pw1"})
        target = store.dir / "wire-cell-db-acme.json"
        target.write_text("[1, 2, 3]")
        with pytest.raises(SecretsStoreError, match="unexpected payload shape"):
            store.get("wire/cell-db/acme")

    def test_unwritable_dir_mentions_secrets_dir_guidance(self, tmp_path):
        # secrets_dir 指向一个普通文件 → mkdir 失败 → 指引核对 CELL_SECRETS_DIR
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        store = FileSecretStore(blocker)
        with pytest.raises(SecretsStoreError, match="CELL_SECRETS_DIR"):
            store.put("wire/cell-db/acme", {"password": "pw1"})

    def test_error_message_never_contains_entry_plaintext(self, tmp_path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        store = FileSecretStore(blocker)
        try:
            store.put("wire/cell-db/acme", {"password": "super-secret-pw"})
        except SecretsStoreError as exc:
            assert "super-secret-pw" not in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected SecretsStoreError")
