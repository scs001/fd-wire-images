"""注销保留期：retention 状态与剩余天数 / 到期 purge / 保留期内 cancel 恢复。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from cell_manager import RETENTION, CellManager, InvalidCellStateError


def test_deprovision_enters_retention_with_remaining_days(manager, fake_driver, config):
    manager.provision("acme")
    status = manager.deprovision("acme")

    assert status.state == RETENTION
    assert status.days_remaining == 30  # CELL_RETENTION_DAYS=30
    assert status.purge_at is not None
    # 容器已停（成员无法访问），但卷完整保留
    assert fake_driver.inspect("fd-wire-cell-acme").status == "stopped"
    assert (config.data_root / "acme" / "daas.db").is_file()


def test_deprovision_from_paused_cell_works(manager, config):
    manager.provision("acme")
    manager.pause("acme")
    status = manager.deprovision("acme")

    assert status.state == RETENTION
    assert (config.data_root / "acme" / "daas.db").is_file()


def test_days_remaining_counts_down(manager, clock):
    manager.provision("acme")
    manager.deprovision("acme")

    clock.advance(days=29)
    assert manager.status("acme").days_remaining == 1

    clock.advance(days=1)  # 恰好到期
    assert manager.status("acme").days_remaining == 0

    clock.advance(days=2)  # 过期不出现负数
    assert manager.status("acme").days_remaining == 0


def test_purge_expired_removes_volume_and_registry_entry(manager, fake_driver, fake_registry, clock, config):
    manager.provision("acme")
    manager.deprovision("acme")
    manager.provision("globex")  # 另一家不受牵连

    clock.advance(days=31)  # 超过保留期
    purged = manager.purge_expired()

    assert [p.customer_id for p in purged] == ["acme"]
    # 卷已删
    assert not (config.data_root / "acme").exists()
    # registry 条目已摘除
    assert not fake_registry.is_registered("fd-wire-cell-acme")
    # 授权束同步回收（1.6）：注销条目后删授权 scope（宽幂等兜底）
    assert fake_registry.calls[-2] == ("unregister", "fd-wire-cell-acme")
    assert fake_registry.calls[-1] == ("delete_scope", "mcp-wire-cell-acme")
    assert fake_registry.scope("mcp-wire-cell-acme") is None
    # 簿记清除 → 不存在
    assert manager.status("acme").state == "not_found"
    # 容器已移除
    assert not fake_driver.exists("fd-wire-cell-acme")
    # 另一家原样
    assert manager.status("globex").state == "active"
    assert fake_registry.is_registered("fd-wire-cell-globex")


def test_purge_expired_before_deadline_is_noop(manager, fake_registry, clock):
    manager.provision("acme")
    manager.deprovision("acme")

    clock.advance(days=29)
    assert manager.purge_expired() == []
    assert manager.status("acme").state == RETENTION
    assert fake_registry.is_registered("fd-wire-cell-acme")


def test_cancel_deprovision_within_retention_restores_active(manager, fake_driver, fake_registry, clock, config):
    manager.provision("acme")
    manager.deprovision("acme")
    clock.advance(days=10)  # 保留期内（剩 20 天）

    status = manager.cancel_deprovision("acme")

    assert status.state == "active"
    assert status.days_remaining is None
    # 原卷未动、容器重启、registry 条目保持
    assert (config.data_root / "acme" / "daas.db").is_file()
    assert fake_driver.inspect("fd-wire-cell-acme").status == "running"
    assert fake_registry.is_registered("fd-wire-cell-acme")


def test_cancel_deprovision_rejected_when_active(manager):
    manager.provision("acme")
    with pytest.raises(InvalidCellStateError, match="active"):
        manager.cancel_deprovision("acme")


def test_zero_retention_days_purges_on_next_sweep(config, fake_driver, fake_registry, clock):
    # 同一 home/state：retention=0 的管理面看到同一份簿记
    cfg0 = replace(config, retention_days=0)
    manager = CellManager(driver=fake_driver, registry=fake_registry, config=cfg0, clock=clock)

    manager.provision("acme")
    manager.deprovision("acme")
    purged = manager.purge_expired()  # 0 天保留 → 立即到期

    assert [p.customer_id for p in purged] == ["acme"]
    assert manager.status("acme").state == "not_found"
