"""datasets API 的请求/响应模型（pydantic v2，契约 §3.3 路径与载荷）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ColumnType = Literal["INTEGER", "REAL", "TEXT"]

#: REST 源登记时**拒收**的请求头名（云端零凭据，ADR-0004）。客户端本地配置里
#: 可以带这些头，云端登记面一律不接受——防凭据误入库/误下发。
CREDENTIAL_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "apikey",
        "x-auth-token",
        "x-access-token",
        "private-token",
        "x-amz-security-token",
    }
)

#: 投影列类型（与引擎三型同口径；缺省 TEXT 保守）
RestColumnType = Literal["INTEGER", "REAL", "TEXT"]

#: 单次 REST 同步的页数上限（防失控；适配器同口径缺省 100）
REST_MAX_PAGES_CEILING = 1000
#: 单页大小上限（防上游被单页压垮）
REST_PAGE_SIZE_CEILING = 10_000


class RestProjectionIn(BaseModel):
    """一条投影：响应 JSON 路径 → 数据集列名（声明式，不做隐式猜测）。"""

    #: 响应内的点分路径（如 ``data.id`` / ``items.0.name``）
    path: str = Field(min_length=1, max_length=512)
    #: 落地列名（须与 columns_json 对齐；同名重复在服务层拦）
    column: str = Field(min_length=1, max_length=200)
    type: RestColumnType = "TEXT"
    model_config = ConfigDict(extra="forbid")


class RestPaginationIn(BaseModel):
    """分页声明（page 式或 cursor 式二选一，由 ``mode`` 判别）。"""

    mode: Literal["page", "cursor"]
    #: page 式：页码参数名（如 page / pageNo / pageIndex）
    page_param: str | None = Field(default=None, max_length=64)
    #: page 式：每页参数名
    size_param: str | None = Field(default=None, max_length=64)
    #: page 式：起始页（缺省 1）
    start_page: int | None = Field(default=None, ge=0)
    #: cursor 式：游标参数名
    cursor_param: str | None = Field(default=None, max_length=64)
    #: cursor 式：下一页游标的响应路径（空/缺 = 无下一页）
    cursor_path: str | None = Field(default=None, max_length=512)
    #: 每页大小（两式共用；≤10000）
    page_size: int | None = Field(default=None, ge=1, le=REST_PAGE_SIZE_CEILING)
    #: 单轮页数上限（≤1000）
    max_pages: int | None = Field(default=None, ge=1, le=REST_MAX_PAGES_CEILING)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _shape(self) -> "RestPaginationIn":
        if self.mode == "page" and not self.page_param:
            raise ValueError("page 式分页必须声明 page_param")
        if self.mode == "cursor" and not self.cursor_param:
            raise ValueError("cursor 式分页必须声明 cursor_param")
        return self


class RestSourceConfig(BaseModel):
    """REST 源声明式配置（云端只存这一层，**零凭据**）。

    与连接器本地配置的 ``kind=rest`` 源同形：认证（auth）只在本地，云端此
    模型**没有 auth 字段**——带 auth 的载荷因 ``extra="forbid"`` 直接被拒。
    """

    #: http(s) 完整地址（凭据/查询串由客户自担：不要放 URL 里）
    url: str = Field(min_length=1, max_length=2048)
    #: HTTP 方法（缺省 GET）
    method: Literal["GET", "POST"] | None = None
    #: 非敏感自定义头（凭据类头名在下方校验器拒收）
    headers: dict[str, str] | None = None
    #: 数组根路径（点分；空 = 响应根即数组）
    root_path: str | None = Field(default=None, max_length=512)
    #: 字段投影（至少一条；列名不重复在服务层校验）
    projection: list[RestProjectionIn] = Field(min_length=1)
    #: 分页声明（缺省 = 单页）
    pagination: RestPaginationIn | None = None
    #: 水位响应路径（可选增量；缺 = 该源无增量能力，如实回退全量）
    watermark_path: str | None = Field(default=None, max_length=512)

    model_config = ConfigDict(extra="forbid")

    @field_validator("url")
    @classmethod
    def _url_scheme(cls, v: str) -> str:
        stripped = v.strip()
        lowered = stripped.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ValueError("url 必须以 http:// 或 https:// 开头")
        return stripped

    @field_validator("headers")
    @classmethod
    def _no_credentials(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if not v:
            return v
        bad = sorted(
            name for name in v if name.strip().lower() in CREDENTIAL_HEADER_NAMES
        )
        if bad:
            raise ValueError(
                "请求头不得携带认证凭据（"
                + "、".join(bad)
                + "）——凭据只存连接器本地配置文件（0600），云端零凭据"
            )
        return v



class ColumnIn(BaseModel):
    """列声明（引擎三型之一；不确定一律 TEXT——保守由调用方保证）。"""

    name: str = Field(min_length=1, max_length=200)
    type: ColumnType = "TEXT"


class DatasetSourceIn(BaseModel):
    """数据集来源（上传 / 连接器白名单 / 声明式建集 / 指标目录），随 kind 互斥取用。"""
    kind: Literal["upload", "connector", "declared", "indicator"]
    upload_id: str | None = None
    connector_id: str | None = None
    #: 连接器本地源名（连接器本地配置 sources[].name 的映射键）；上传类忽略
    source_name: str | None = Field(default=None, max_length=200)
    #: 源内的表名/文件名（连接器拉数对象）；上传类忽略
    source_ref: str | None = Field(default=None, max_length=512)
    schedule_cron: str | None = Field(default=None, max_length=64)
    #: declared 专用：列声明 [{name, type(INTEGER|REAL|TEXT)}]（wire-customer-data-engine 2.3）
    columns: list[ColumnIn] | None = None
    #: declared 专用：主键列（成为行锚；缺省行哈希兜底）
    pk_columns: list[str] | None = None
    #: indicator 专用（wire-indicator-to-dataset）：统一注册目录标识
    #: ``source_db:native_code``（catalog 镜像的 indicator_id）与实体/时间范围
    indicator_id: str | None = Field(default=None, max_length=200)
    entity_type: str | None = Field(default=None, max_length=64)
    entity_id: str | None = Field(default=None, max_length=200)
    start: str | None = Field(default=None, max_length=10)
    end: str | None = Field(default=None, max_length=10)
    #: connector 专用（wire-rest-connector）：REST 源的声明式请求形状。**零凭据**
    #: ——认证凭据只存连接器本地配置文件；本字段的 headers 拒凭据类头名。
    #:
    #: 这里**不**用 ``RestSourceConfig`` 做解析期校验：pydantic 的 422 会把
    #: 提交值原样回显在 ``input``/``ctx`` 里，凭据类头一旦带错就会漏进响应
    #: （违反零凭据）。改为路由层 ``_rest_source_config`` 显式校验并构造**不回显
    #: 输入**的 422 文案。
    source_config: dict[str, Any] | None = None


class DatasetCreateIn(BaseModel):
    """建档：名称 + 来源（载荷严格按契约 §3.3）。"""

    name: str = Field(min_length=1, max_length=200)
    source: DatasetSourceIn


class MaterializeInfo(BaseModel):
    """m:n 桥物化的诚实计数（wire-entity-mn-materialize design D1/D3）：

    ``estimate_rows``＝桥行数（物化前估算口径）、``rows``＝实际展开对数、
    ``orphan_dropped``＝引用失存端点被 INNER 丢弃的桥行数（如实上报不静默）。
    仅 materialize 端点在 m:n 关系时回填；其余数据集视图恒 None。
    """

    estimate_rows: int
    rows: int
    orphan_dropped: int


class DatasetOut(BaseModel):
    """数据集客户视图（仅本企业可见；跨企业一律 404）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    key: str
    name: str
    source_kind: str
    #: 存储分层徽标（wire-free-tier-data-gate）：api=api 层轻量存储（免费档）、
    #: cell=引擎物化（付费档）——与 source_kind（从哪来）正交。
    storage_tier: str = "cell"
    connector_id: str | None = None
    source_name: str | None = None
    source_ref: str | None = None
    columns: list[ColumnIn] = []
    row_count: int = 0
    size_bytes: int = 0
    sync_cron: str | None = None
    sync_status: str = "idle"
    paused_reason: str | None = None
    #: 连接器同步模式与水位（wire-connector-incremental-sync）：snapshot
    #: 默认；incremental 时 watermark_column/watermark_value 有值（水位值
    #: 原样字符串，列清单里既有列可前台做下拉）
    sync_mode: str = "snapshot"
    watermark_column: str | None = None
    watermark_value: str | None = None
    #: REST 源声明式配置（wire-rest-connector；非 rest 源为 null）。**零凭据**：
    #: 该字段的 headers 在登记时已拒凭据类头名。
    source_config: RestSourceConfig | None = None
    #: m:n 桥物化计数（wire-entity-mn-materialize；仅物化端点 m:n 时回填）
    materialization: MaterializeInfo | None = None
    #: 访问模式（wire-dataset-acl）：team（默认，本队全员可见）| restricted
    #: （仅 owner 与显式授权成员；前端据此渲染「受限」徽标与访问面板）
    acl_mode: str = "team"
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_dataset(cls, dataset) -> "DatasetOut":
        return cls(
            id=dataset.id,
            key=dataset.key,
            name=dataset.name,
            source_kind=dataset.source_kind,
            storage_tier=getattr(dataset, "storage_tier", "cell"),
            connector_id=dataset.connector_id,
            source_name=dataset.source_name,
            source_ref=dataset.source_ref,
            columns=[ColumnIn(**c) for c in (dataset.columns_json or [])],
            row_count=dataset.row_count,
            size_bytes=dataset.size_bytes,
            sync_cron=dataset.sync_cron,
            sync_status=dataset.sync_status,
            paused_reason=dataset.paused_reason,
            sync_mode=getattr(dataset, "sync_mode", "snapshot") or "snapshot",
            watermark_column=getattr(dataset, "watermark_column", None),
            watermark_value=getattr(dataset, "watermark_value", None),
            source_config=(
                RestSourceConfig(**dataset.source_config_json)
                if getattr(dataset, "source_config_json", None)
                else None
            ),
            acl_mode=getattr(dataset, "acl_mode", None) or "team",
            created_at=dataset.created_at,
            updated_at=dataset.updated_at,
        )


class UploadOut(BaseModel):
    """上传预览：建档前呈现列名与推断类型 + 样例行。"""

    upload_id: str
    filename: str
    columns: list[ColumnIn]
    row_count: int
    within_limit: bool = True
    sample_rows: list[dict[str, Any]] = []


class DatasetQuotaOut(BaseModel):
    """免费档配额视图（wire-free-tier-data-gate §7；付费档 max 为 null =
    无此限额，控制台隐藏配额条）。"""

    model_config = ConfigDict(from_attributes=True)

    plan_code: str
    #: 免费档口径的已用数据集个数（= api 层数据集数）
    used_datasets: int
    max_datasets: int | None = None
    max_rows_per_dataset: int | None = None


class CorrectionIn(BaseModel):
    """修正三件套：

    - ``{op:"update", row_id, column, value}`` 改值
    - ``{op:"delete", row_id}`` 删行
    - ``{op:"insert", values, artifact_id?}`` 加行

    ``row_id`` = 预览行的 ``__row_id``（行锚，引擎算）。``artifact_id``
    （wire-form-entry-advanced design D1，仅 insert 可带）：表单运行时提交
    携带时服务端按该 form 产物**当前版本**执行全量字段校验+提交闸（统一
    执法点，与公开 submit 同规）；不携带时行为与现状逐字一致。
    """

    op: Literal["update", "delete", "insert"]
    row_id: str | None = None
    column: str | None = None
    value: Any = None
    values: dict[str, Any] | None = None
    artifact_id: int | None = None


class CorrectionOut(BaseModel):
    """修正写入回执（引擎 correction_id）。"""

    correction_id: int


class ResumeIn(BaseModel):
    """漂移人工确认：接受新 schema（``accept_columns`` 覆盖 columns_json）。"""

    accept_columns: list[ColumnIn] = Field(min_length=1)


class SyncSettingsPatchIn(BaseModel):
    """增量同步设置（wire-connector-incremental-sync；仅连接器数据集）。

    - ``sync_mode``：snapshot ↔ incremental 切换；切回 snapshot 清水位
      两字段（watermark_column/watermark_value 置 NULL）。
    - ``watermark_column``：增量模式的水位列（须 ∈ columns_json 既有列）；
      换列会同时清水位值（旧值属于旧列，比较语义失效——下次同步回到
      全量替换重建基线）。
    - ``reset_watermark``：只清水位值（模式与列保留）——下次同步按全量
      替换重建基线并重新捕获水位。
    """

    sync_mode: Literal["snapshot", "incremental"] | None = None
    watermark_column: str | None = Field(default=None, max_length=64)
    reset_watermark: bool = False


class SyncOut(BaseModel):
    """同步历史行（客户可见；失败原因如实呈现）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    dataset_id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    rows: int = 0
    error: str | None = None
    #: 本轮同步模式 snapshot|incremental（wire-connector-incremental-sync）
    mode: str = "snapshot"


class PairingTokenOut(BaseModel):
    """配对令牌：15 分钟、单次；明文只在本响应出现一次。"""

    token: str
    expires_at: datetime


class ConnectorOut(BaseModel):
    """连接器客户视图（状态由心跳推导，不落库）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    version: str | None = None
    status: str
    last_heartbeat_at: datetime | None = None
    created_at: datetime
    revoked_at: datetime | None = None
    dataset_count: int = 0


class ConnectorPatchIn(BaseModel):
    """改名（白名单/调度变更走数据集端点）。"""

    name: str = Field(min_length=1, max_length=200)


class PairIn(BaseModel):
    """连接器换取 key：配对令牌 + 自报名称/版本。"""

    token: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    version: str | None = Field(default=None, max_length=64)


class PairOut(BaseModel):
    """配对回执：connector_key 明文仅此一次（库内只存 sha256）。"""

    connector_key: str
    connector_id: str


class HeartbeatIn(BaseModel):
    """心跳：版本 + 可选同步态（同步态不落库，仅在线判定用）。"""

    version: str | None = Field(default=None, max_length=64)
    sync_state: dict[str, Any] | None = None


class HeartbeatOut(BaseModel):
    """心跳回执：轮询间隔（秒），连接器据此节拍。"""

    poll_interval_seconds: int


class SyncBeginIn(BaseModel):
    """推送阶段一：声明 schema 与行数（漂移/配额判定在这里，先于引擎调用）。"""

    dataset_key: str = Field(min_length=1, max_length=40)
    columns: list[ColumnIn] = Field(min_length=1)
    pk_columns: list[str] = []
    row_count: int | None = Field(default=None, ge=0)


class SyncBeginOut(BaseModel):
    """begin 回执：sync_id 供后续 chunk/commit/abort。"""

    sync_id: str


class ChunkIn(BaseModel):
    """推送阶段二：单块 ≤2000 行（引擎侧同上限）。"""

    index: int = Field(ge=0)
    rows: list[dict[str, Any]] = Field(min_length=1, max_length=2000)


class ChunkOut(BaseModel):
    accepted: int


class CommitIn(BaseModel):
    """推送阶段三请求体：可选水位上报（wire-connector-incremental-sync）。

    ``watermark_value`` 为连接器本轮增量窗口的末值（字符串原样——整数十
    进制/浮点 %v/时间 RFC3339Nano/字符串原样）；空串视为未上报（本轮零
    行不动水位）。非增量数据集携带水位 → 422 fail-closed（防误配）。
    长度校验（Trim 后 ≤256）在路由层做（422 带原因）。
    """

    watermark_value: str | None = None


class CommitOut(BaseModel):
    status: Literal["ok"] = "ok"
    rows: int


class AbortOut(BaseModel):
    aborted: bool = True


class InternalConnectorOut(BaseModel):
    """内部概览：连接器健康（只读视图，无管理操作入口）。"""

    id: str
    status: str
    last_heartbeat_at: datetime | None = None


class InternalOverviewOut(BaseModel):
    """内部后台只读概览：per-cell 存储用量与连接器健康。"""

    customer_id: str
    datasets: int
    storage_bytes: int
    connectors: list[InternalConnectorOut]