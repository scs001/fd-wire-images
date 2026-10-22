"""隔离保证（规格 wire-cell-tenancy「企业数据互不可见」在管理面的落地断言）：

两企业 cell 并存时——卷目录互不重叠、容器名互不相同、FakeDriver 视角
每容器只挂自己的卷（不存在读到对方数据根的路径）。
"""

from __future__ import annotations

from pathlib import Path


def test_two_cells_coexist_with_disjoint_names_and_volumes(manager, fake_driver, fake_registry, config):
    a = manager.provision("acme")
    b = manager.provision("globex")

    # 容器名 / registry 条目路径 / 卷路径两两互异
    assert a.container_name != b.container_name
    assert a.registry_path != b.registry_path
    vol_a, vol_b = Path(a.volume_path), Path(b.volume_path)
    assert vol_a != vol_b
    assert not vol_a.is_relative_to(vol_b)
    assert not vol_b.is_relative_to(vol_a)
    # 两容器同时在跑、两条目同时注册
    assert fake_driver.inspect(a.container_name).status == "running"
    assert fake_driver.inspect(b.container_name).status == "running"
    assert fake_registry.is_registered(a.registry_path)
    assert fake_registry.is_registered(b.registry_path)


def test_each_container_sees_only_its_own_volume(manager, fake_driver, config):
    a = manager.provision("acme")
    b = manager.provision("globex")

    vol_a, vol_b = Path(a.volume_path), Path(b.volume_path)
    mounts_a = fake_driver.inspect(a.container_name).mounts
    mounts_b = fake_driver.inspect(b.container_name).mounts

    # acme 容器：恰好一个挂载，且是自己的卷
    assert len(mounts_a) == 1
    assert Path(mounts_a[0].host_path) == vol_a
    # globex 容器：acme 的卷路径不可见（不存在读到对方数据根的挂载路径）
    assert Path(mounts_b[0].host_path) == vol_b
    assert all(vol_a != Path(m.host_path) for m in mounts_b)
    assert all(not Path(m.host_path).is_relative_to(vol_a) for m in mounts_b)


def test_tenant_file_in_one_volume_invisible_in_the_other(manager, config):
    a = manager.provision("acme")
    manager.provision("globex")

    secret = (Path(a.volume_path) / "daas.db")
    assert secret.is_file()
    # globex 卷里没有 acme 的任何文件
    globex_files = {p.name for p in (config.data_root / "globex").iterdir()}
    assert globex_files == {"daas.db"}
    assert secret not in list((config.data_root / "globex").iterdir())


def test_pausing_one_cell_does_not_affect_the_other(manager, fake_driver):
    a = manager.provision("acme")
    manager.provision("globex")

    manager.pause("acme")

    assert fake_driver.inspect(a.container_name).status == "stopped"
    assert fake_driver.inspect("fd-wire-cell-globex").status == "running"
    assert manager.status("globex").state == "active"
