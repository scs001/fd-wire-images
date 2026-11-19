"""fd-wire 的符合性适配器（establish-ontology-kernel 6.5）。

闸门要的是**可丢弃的沙箱**：写进去的东西一律随沙箱消失，绝不碰业务库。这里的
沙箱是一个进程内 SQLite 引擎 + 真实 ORM 表（``EntitiesBase`` / ``AiBase`` 各建
一次），端口则用真实实现的 :mod:`fd_wire_api.entities.kernel_ports`——于是闸门
检查的是「fd-wire 的落库映射」，而不是内核自带的内存样例。

**为什么先导 app**：``datasets`` 包在 import 期有循环引用（entities → datasets →
ai → materialization → datasets.ingest），单独导 ``fd_wire_api.entities.*`` 会炸在
「partially initialized module」上。测试套件与 ``scripts/check_ontology_digests.py``
同样是先导 app 才成立。

本模块因此放在 ``fd_wire_api/`` 顶层而不是 ``entities/`` 下：网关按
``fd_wire_api.conformance_adapter`` 导入，``fd_wire_api/__init__`` 是空的，于是
「先导 app」这条顺序能真正生效（放在 entities 包里时，包 ``__init__`` 会先跑，
那条 import 永远来不及）。
"""

from __future__ import annotations

import fd_wire_api.app  # noqa: F401  # isort: skip
from fd_ontology_kernel.conformance.spec import AdapterSpec
from fd_ontology_kernel.kernel import Kernel
from fd_ontology_kernel.manifest import SHAPE_ID_ADDRESSED

from .entities import kernel_ports  # noqa: F401  端口模块随适配器一同加载
from .entities.kernel_ports import (
    DECLARATION,
    RELATIONSHIP_KINDS,
    WireEventJournal,
    WireProposalBox,
    WirePublicationStore,
    WireUnitStore,
    WireVocabulary,
)


def _sandbox() -> Kernel:
    """一次性内存库 + 真实端口。每次调用都是新库，调用之间不共享状态。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from fd_wire_api.ai.db import AiBase
    from fd_wire_api.entities.db import EntitiesBase

    engine = create_engine(
        "sqlite://",
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    EntitiesBase.metadata.create_all(engine)
    AiBase.metadata.create_all(engine)
    session = Session(engine)

    return Kernel(
        units=WireUnitStore(session),
        events=WireEventJournal(session),
        vocabulary=WireVocabulary(),
        proposals=WireProposalBox(session),
        publications=WirePublicationStore(session),
        # fd-wire 没有分发注册表（消费者在 cell 侧），该端口留空是事实而非缺口。
        distribution=None,
        relationship_kinds=RELATIONSHIP_KINDS,
        # 本体里没有「条款」这一概念：条款父键规则在此不适用，套件会报 SKIP。
        clause_kinds=(),
        manifest_shape=SHAPE_ID_ADDRESSED,
        declaration=DECLARATION,
    )


def build():
    """``adapters.json`` 点名的工厂。"""

    return AdapterSpec(
        name="fd-wire",
        system=DECLARATION.system,
        declaration=DECLARATION,
        sandbox=_sandbox,
        notes="customer object surface (id-addressed, shape 1); SQLite sandbox over the real ORM",
        # 这个面**没有**可写提案（design D7：动作闸执行记录是只读投影）与分发注册表
        # （消费者在 cell 侧）。声明出来，相关断言记 SKIP 并带上这条依据——
        # 不声明的话会被按「什么都有」检查，然后红在一片它本来就不做的事上。
        capabilities={"proposals": "projection", "distribution": "absent"},
    )
