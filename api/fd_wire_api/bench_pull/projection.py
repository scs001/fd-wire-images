"""知验台本体快照只读投影（projection）。

spec：投影面 SHALL NOT 提供任何写路径——本模块纯读（SELECT only），不写
wire 自有 entities 表、不做本地编辑。payload 解包纪律（design D6「宽容读
+ 核心键严格」）：

- **未知键忽略**：对端载荷演进出的新键不报错、原样随条目透传（快照冻结，
  投影不丢数据）；
- **核心键严格**：条目缺核心键（实体 ``external_key``/``kind``/``body``；
  关系经 :func:`rebuild_identity` 解不出的 from/to/kind）即
  :class:`ValueError`——形状漂移宁可红不可静默丢身份；
- **空域如实**：从未拉取过的域返回空集（spec「空域如实」），不报错。

键约定 ``{from_key}->{kind}:{to_key}`` 在本仓本地复述（模块常量），来源
law-bench sha-43f7063——对端 docstring 是契约正本，此处只是消费端副本，
漂移由契约测试（D5）兜底。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .store import BenchOntologySnapshot

#: 关系键约定（本地复述；正本 = law-bench sha-43f7063 的 distribution 面
#: docstring：关系 external_key 恒为 ``{from_key}->{kind}:{to_key}``）。
RELATIONSHIP_KEY_TEMPLATE = "{from_key}->{kind}:{to_key}"


def rebuild_identity(rel: dict) -> dict:
    """一条关系载荷 → ``{from_key, kind, to_key, external_key}``。

    解析顺序：显式 ``attrs.from_key``/``attrs.to_key`` 优先，缺省回落结构化
    端点 ``attrs.from``/``attrs.to`` 对象的 ``external_key``；``kind`` 取条
    目顶层字段（回落 ``attrs.kind``）。核心键（from_key/kind/to_key）解不出
    即 :class:`ValueError`（D6 核心键严格）；``external_key`` 按键约定重建。
    """
    if not isinstance(rel, dict):
        raise ValueError(f"关系条目不是对象：{type(rel).__name__}")
    attrs = rel.get("attrs") if isinstance(rel.get("attrs"), dict) else {}

    kind = rel.get("kind") or attrs.get("kind")

    def _endpoint_key(endpoint: object) -> str | None:
        if isinstance(endpoint, dict):
            candidate = endpoint.get("external_key") or endpoint.get("key")
            if candidate:
                return str(candidate)
        return None

    from_key = attrs.get("from_key") or _endpoint_key(attrs.get("from"))
    to_key = attrs.get("to_key") or _endpoint_key(attrs.get("to"))

    missing = [
        name
        for name, value in (
            ("from_key", from_key),
            ("kind", kind),
            ("to_key", to_key),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"关系条目缺少核心键：{', '.join(missing)}")

    from_key = str(from_key)
    kind = str(kind)
    to_key = str(to_key)
    return {
        "from_key": from_key,
        "kind": kind,
        "to_key": to_key,
        "external_key": RELATIONSHIP_KEY_TEMPLATE.format(
            from_key=from_key, kind=kind, to_key=to_key
        ),
    }


# ---------------------------------------------------------------- 版本定位


def _resolve_snapshot(
    conn: Session, *, domain_key: str, version: int | None
) -> dict | None:
    """version=None → 最新快照；指定版本 → 该版本快照；无 → None（空域如实）。"""
    from .store import get_latest_snapshot, get_snapshot

    if version is None:
        return get_latest_snapshot(conn, domain_key=domain_key)
    return get_snapshot(conn, domain_key=domain_key, version=version)


def _payload_items(
    conn: Session,
    *,
    domain_key: str,
    version: int | None,
    array_key: str,
) -> tuple[int, list]:
    """定位快照并取 ``entities``/``relationships`` 数组（缺省空列表）。"""
    snapshot = _resolve_snapshot(conn, domain_key=domain_key, version=version)
    if snapshot is None:
        return 0, []
    payload = snapshot["payload"] or {}
    items = payload.get(array_key)
    if not isinstance(items, list):
        raise ValueError(f"快照 {domain_key} v{snapshot['version']} 载荷 {array_key} 不是数组")
    return int(snapshot["version"]), items


# ---------------------------------------------------------------- 投影查询


def list_domains_with_snapshots(conn: Session) -> list[dict]:
    """有快照的域清单：domain_key + 最新 version/digest（无快照域不出现）。"""
    domain_keys = conn.scalars(
        select(BenchOntologySnapshot.domain_key).distinct()
    ).all()
    domains: list[dict] = []
    from .store import get_latest_snapshot

    for domain_key in sorted(domain_keys):
        latest = get_latest_snapshot(conn, domain_key=domain_key)
        domains.append(
            {
                "domain_key": domain_key,
                "version": latest["version"] if latest else None,
                "digest": latest["digest"] if latest else None,
            }
        )
    return domains


def _count_items(payload: dict | None, key: str) -> int:
    """载荷里某个条目数组的长度；缺失/非数组一律 0（元数据面不因坏载荷 500）。"""
    items = (payload or {}).get(key)
    return len(items) if isinstance(items, list) else 0


def snapshot_overview(conn: Session) -> list[dict]:
    """登录态读面的载荷：每域最新版本的**元数据**，不含快照正文。

    只读且只出元数据是刻意的（design D3）：快照正文整块含共享层内容，一旦
    可读就等于把平台发布物免费复制进浏览器；而用户要回答的问题是「拉到哪
    一版、拉的是什么规模、什么时候拉的」，这四项都在元数据里。
    """
    from .store import get_latest_snapshot

    domain_keys = conn.scalars(
        select(BenchOntologySnapshot.domain_key).distinct()
    ).all()
    overview: list[dict] = []
    for domain_key in sorted(domain_keys):
        latest = get_latest_snapshot(conn, domain_key=domain_key)
        if latest is None:
            continue
        payload = latest["payload"] or {}
        overview.append(
            {
                "domain_key": domain_key,
                "version": latest["version"],
                "digest": latest["digest"],
                "entity_count": _count_items(payload, "entities"),
                "relationship_count": _count_items(payload, "relationships"),
                "pulled_at": _isoformat(latest["pulled_at"]),
            }
        )
    return overview


def _isoformat(value):
    """时间字段统一成 ISO-8601 文本（驱动可能给 datetime，也可能给文本）。"""
    return value.isoformat() if hasattr(value, "isoformat") else value


def list_versions(conn: Session, *, domain_key: str) -> list[dict]:
    """某域版本时间线：version + digest + pulled_at（version 倒序）。"""
    from .store import list_snapshots

    return [
        {
            "version": row["version"],
            "digest": row["digest"],
            "pulled_at": row["pulled_at"],
        }
        for row in list_snapshots(conn, domain_key=domain_key)
    ]


def list_entities(
    conn: Session,
    *,
    domain_key: str,
    version: int | None = None,
    kind: str | None = None,
) -> list[dict]:
    """列实体条目（version=None=最新；kind 过滤；未知域 → []）。

    核心键（external_key/kind/body）缺失即 :class:`ValueError`；未知键忽略
    （条目原样透传）。
    """
    _snapshot_version, items = _payload_items(
        conn, domain_key=domain_key, version=version, array_key="entities"
    )
    entities: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"实体条目 #{index} 不是对象：{type(item).__name__}")
        missing = [
            key for key in ("external_key", "kind", "body") if key not in item
        ]
        if missing:
            external = item.get("external_key")
            raise ValueError(
                f"实体条目 {external or f'#{index}'} 缺少核心键：{', '.join(missing)}"
            )
        if kind is not None and item.get("kind") != kind:
            continue
        entities.append(item)
    return entities


def list_relationships(
    conn: Session,
    *,
    domain_key: str,
    version: int | None = None,
) -> list[dict]:
    """列关系条目，每条并上重建身份四键（from_key/kind/to_key/external_key）。

    身份重建失败（核心键缺失）即 :class:`ValueError`；重建键覆盖条目自带
    同名键——按键约定重建的才是两端身份的权威形态。
    """
    _snapshot_version, items = _payload_items(
        conn, domain_key=domain_key, version=version, array_key="relationships"
    )
    relationships: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"关系条目 #{index} 不是对象：{type(item).__name__}")
        identity = rebuild_identity(item)
        relationships.append({**item, **identity})
    return relationships


__all__ = [
    "RELATIONSHIP_KEY_TEMPLATE",
    "list_domains_with_snapshots",
    "list_entities",
    "list_relationships",
    "list_versions",
    "rebuild_identity",
]
