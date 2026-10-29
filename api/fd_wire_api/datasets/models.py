"""datasets 切片 ORM 模型（契约 §3.2 六表）。

- ``connectors`` / ``connector_keys`` / ``connector_pairing_tokens``：
  出站连接器身份（配对令牌 15 分钟单次、key 存 sha256、吊销即时生效）。
- ``datasets``：客户数据集登记（1:1 对应一张源表/一个文件），有效数据按
  ``storage_tier`` 分层——cell 层在引擎侧、api 层在本库轻量行存储；本表
  只存元数据（绝不存客户源 DSN/账密）。
- ``dataset_syncs``：同步历史（status 终态 ok|failed|drift|quota；
  在飞行为 ``syncing`` 标记，abort 时整行清理——见 sync_service 说明）。
- ``uploads``：上传原件登记（stored_path 指向 api 侧暂存 CSV）。
- ``dataset_columns`` / ``dataset_rows``：api 层轻量行存储（免费档，
  wire-free-tier-data-gate D2——列元数据 + JSON 行，SQLite/PG 双方言）。
- ``dataset_corrections``：api 层本地修正覆盖层（wire-dataset-form-editing，
  镜像引擎覆盖层语义——只追加、回滚留痕）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import DatasetsBase


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_uuid() -> str:
    return str(uuid.uuid4())


#: JSON 行存储的双方言类型：SQLite 用 JSON、PG 用 JSONB（design D2；
#: wire-dedicated-postgres 的双方言审计覆盖本列）。
JSON_DUAL_DIALECT = JSON().with_variant(JSONB(), "postgresql")


class Connector(DatasetsBase):
    """客户内网出站连接器（一个企业可有多个，各自独立 key 与白名单）。"""

    __tablename__ = "connectors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 落库状态仅作留档；对外状态一律由心跳推导（见 service.connector_status）
    status: Mapped[str] = mapped_column(String(16), default="offline")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ConnectorKey(DatasetsBase):
    """连接器 key（明文只在下发当次出现，库内只存 sha256）。"""

    __tablename__ = "connector_keys"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    connector_id: Mapped[str] = mapped_column(String(36), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ConnectorPairingToken(DatasetsBase):
    """配对令牌：15 分钟有效、单次使用（库内只存 sha256）。"""

    __tablename__ = "connector_pairing_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Dataset(DatasetsBase):
    """客户数据集登记（元数据；cell 层有效数据在引擎侧、api 层在本库）。"""

    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("customer_id", "key", name="uq_datasets_customer_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    #: dataset_key（^[a-z][a-z0-9_]{0,39}$，企业内唯一；也是引擎表名后缀）
    key: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(200))
    #: "upload" | "connector"
    source_kind: Mapped[str] = mapped_column(String(16))
    #: 存储分层（wire-free-tier-data-gate D1）："cell"=引擎物化（付费档默认，
    #: 存量行同值）、"api"=本库轻量行存储（免费档建集时落值）。
    #: 与 source_kind（从哪来）正交——source 是来源，tier 是存哪里。
    storage_tier: Mapped[str] = mapped_column(String(16), default="cell")
    connector_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    #: 连接器本地源名（连接器本地配置 sources[].name 的映射键）；云端绝不含 DSN
    source_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: 源内的表名/文件名（连接器拉数对象）或上传文件名；云端绝不含 DSN
    source_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: 连接器源的声明式请求形状（wire-rest-connector）：URL/method/分页/投影/
    #: 水位响应路径/非敏感头。**零凭据**——登记端点拒凭据类头名，认证只存
    #: 连接器本地配置文件（ADR-0004）。非 rest 源为 NULL。
    source_config_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: [{name, type(INTEGER|REAL|TEXT)}]（漂移基线）
    columns_json: Mapped[list] = mapped_column(JSON, default=list)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sync_cron: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: idle|syncing|ok|failed|paused_drift|paused_quota
    sync_status: Mapped[str] = mapped_column(String(16), default="idle")
    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 溯源档位 off|chain_only|anchored_batch|anchored_realtime（wire-provenance-ledger）
    provenance_tier: Mapped[str] = mapped_column(String(24), default="chain_only")
    #: 连接器同步模式 snapshot（全量替换，默认）| incremental（增量水位，
    #: wire-connector-incremental-sync）；仅 source_kind=connector 有意义
    sync_mode: Mapped[str] = mapped_column(String(16), default="snapshot")
    #: 增量水位列（须 ∈ columns_json 既有列名；sync-settings 端点校验）
    watermark_column: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 最近水位值（TEXT 字符串原样——保整型精度；commit 上报捕获、config 下发；
    #: 非空时 begin 给引擎 ingest_begin 传 mode=merge，否则不传=replace）
    watermark_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, onupdate=_utcnow_naive
    )


class DatasetSync(DatasetsBase):
    """同步历史行（wire-provenance-ledger 起终态留痕、不再删行）。

    终态 status ∈ {ok, failed, drift, quota, aborted}；``syncing`` 是在飞行
    标记（begin 落行、commit/abort 改终态——中止落 ``aborted`` 含原因而非
    删行），保证 chunk/commit 的会话映射可跨请求查回。终态行由触发器
    禁改禁删（迁移 0013）。
    """

    __tablename__ = "dataset_syncs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    dataset_id: Mapped[str] = mapped_column(String(36), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    rows: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 本轮同步模式 snapshot|incremental（begin 时按数据集配置落值；
    #: wire-connector-incremental-sync——历史行记录每轮口径）
    mode: Mapped[str] = mapped_column(String(16), default="snapshot")


class Upload(DatasetsBase):
    """上传登记：列与类型预览的落点，建档消费后置 consumed_at。"""

    __tablename__ = "uploads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    columns_json: Mapped[list] = mapped_column(JSON, default=list)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    #: api 侧暂存路径（CSV 原样存；Excel 转 CSV 暂存）
    stored_path: Mapped[str] = mapped_column(String(1024))
    #: 原始文件内容哈希 sha256:<hex>（存量行无原件、如实置 NULL——wire-provenance-ledger）
    content_sha256: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DatasetColumn(DatasetsBase):
    """api 层数据集列元数据（wire-free-tier-data-gate D2，免费档轻量存储）。

    与 ``datasets.columns_json`` 同源（建档时一次写入两处：JSON 作卡片
    快照，本表作查询面的列注册表——过滤/排序/聚合的列校验与 dtype 依据）。
    """

    __tablename__ = "dataset_columns"
    __table_args__ = (
        UniqueConstraint("dataset_id", "name", name="uq_dataset_columns_dataset_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(200))
    #: INTEGER | REAL | TEXT（与上传类型推断同集，值归一见 api_store）
    dtype: Mapped[str] = mapped_column(String(16))
    ordinal: Mapped[int] = mapped_column(Integer, default=0)


class DatasetRow(DatasetsBase):
    """api 层数据集行存储（JSON 行；row_index 建集时定序、删除不回收）。

    ``data`` 为「列名 → 已按 dtype 归一的值」；缺列即键不存在（与上传
    短行语义一致）。SQLite/PG 双方言：PG 端 JSONB（见 JSON_DUAL_DIALECT）。
    """

    __tablename__ = "dataset_rows"
    __table_args__ = (
        UniqueConstraint("dataset_id", "row_index", name="uq_dataset_rows_dataset_index"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(36), index=True)
    row_index: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON_DUAL_DIALECT)


class DatasetCorrection(DatasetsBase):
    """api 层本地修正覆盖层（wire-dataset-form-editing，镜像引擎覆盖层语义）。

    约束（云端不破坏基行 / 永不丢原始数据）：
    - ``dataset_rows`` 基行**绝不**因修正被改写或删除——本表只追加覆盖
      记录，有效视图 = 基行 + 未回滚修正的实时合并（见 api_store）；
    - 回滚仅置 ``reverted_at``（留痕不删行，审计可回放）；
    - ``op`` ∈ insert|update|delete：insert 的 ``row_anchor`` 为空、
      ``value_json``=整行 dict；update 的 ``row_anchor``=基行 row_index
      字符串、``column_name``+``value_json``=单值；delete 仅 ``row_anchor``；
    - 悬空修正（锚行不在基表）允许落层，列表时如实标注 dangling——
      与引擎悬空语义一致，不静默丢弃。
    """

    __tablename__ = "dataset_corrections"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(36), index=True)
    #: insert | update | delete
    op: Mapped[str] = mapped_column(String(16))
    #: 行锚：基行 row_index 字符串（如 "7"）或插入行合成锚 "c{correction_id}"；
    #: insert 修正无锚（为空）
    row_anchor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: update 修正的目标列（必须在 dataset_columns 列图中）；insert/delete 为空
    column_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: update=单值 / insert=整行 dict（写入前已按列 dtype 归一）；delete 为空
    value_json: Mapped[Any] = mapped_column(JSON_DUAL_DIALECT, nullable=True)
    actor: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    #: 回滚时间（仅置位，永不删行）
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DataPlaneKey(DatasetsBase):
    """REST 数据面 key（wire-customer-data-engine §5，Q6/ADR 词汇表「数据面 key」）。

    明文永不落库（sha256 唯一索引）；吊销 = status 翻转，验证路径即时拒。
    allowlist 为空 = 本 cell 全部数据集；非空 = 仅列出的 dataset_id。
    沿用长命 key 生命周期语义：不过期、可吊销且吊销即时生效。
    """

    __tablename__ = "data_plane_keys"
    __table_args__ = (UniqueConstraint("key_hash", name="uq_data_plane_key_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(200))
    #: sha256(明文) hex —— 明文仅铸造响应返回一次
    key_hash: Mapped[str] = mapped_column(String(64), index=True)
    #: 展示指纹（key_hash 前 12 位），审计与控制台显示用
    fingerprint: Mapped[str] = mapped_column(String(16))
    #: [dataset_id…]；空数组 = 全部
    allowlist_json: Mapped[list] = mapped_column(JSON, default=list)
    #: active | revoked
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)