"""本体版本核心（wire-ontology-versioning 任务 2.1 / 4.1）：规范 manifest、
digest、事件折叠、diff、回滚 plan 与回滚执行。

**规范形态**（design D4）：``{"entities": [...], "relationships": [...]}``——

- 实体条按 ``id`` 排序（缺失 id 时按业务名）；关系条按四元组排序；
- 键序固定（``json.dumps(sort_keys=True)`` 递归生效），null 与缺省不混用
  （每个已知键恒出现，值可为 null）；
- 日期 ISO8601 秒精度、浮点按 JSON 序列化默认（json 默认浮点表现）；
- **不含** ``created_at``/``updated_at``：时间戳不是本体语义，纳入会让
  每次触碰都变 digest（回滚后无法与目标版本对齐）。

digest = sha256(规范 manifest 的 UTF-8 JSON)，对外作为导出物完整性校验值。

**身份纪律**：实体条含自增 ``id``、关系条含两端实体 ``id``——digest 因此
覆盖「同一对象」的身份。回滚到某版按 manifest 恢复时**沿用原 id**（create
显式带 id；被占用则退回自增并在结果里如实报 ``identity_changed``），这样
「回滚后新快照 digest 与目标版本一致」（spec 场景）在正常路径上成立。

**事件折叠**（完整性自检，design D10）：从基线 manifest（若有最早快照，取
其 manifest——那是「启用时刻的当前态」，design D6）出发按 ``(seq, id)`` 顺序
应用事件（create/update 用 after、delete 移除），得到「事件说应该是什么」；
与当前态 manifest 比对即得逃逸写差异条目。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from fd_ontology_kernel import manifest as kernel_manifest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    Entity,
    EntityRelationship,
    OntologyEvent,
    OntologySnapshot,
    utc_now_naive,
)

#: 导出/快照 manifest 的格式版本（schema_version 单一真源）
#: 2 = add-entity-bench-contract 0050：实体/关系条增 tier/sensitivity 两键
#: （键恒出现、null 显式；消费方读旧导出按缺失→null 归一）
SCHEMA_VERSION = 2

#: 变更操作枚举
OP_CREATE = "create"
OP_UPDATE = "update"
OP_DELETE = "delete"
OPS: tuple[str, ...] = (OP_CREATE, OP_UPDATE, OP_DELETE)

#: 目标种类
KIND_ENTITY = "entity"
KIND_RELATIONSHIP = "relationship"
KINDS: tuple[str, ...] = (KIND_ENTITY, KIND_RELATIONSHIP)

#: 写面通道（via）
VIA_ACTION_GATE = "action_gate"
VIA_CONSOLE = "console"
VIA_ROLLBACK = "rollback"
VIAS: tuple[str, ...] = (VIA_ACTION_GATE, VIA_CONSOLE, VIA_ROLLBACK)

#: 快照触发（trigger）
TRIGGER_DAILY = "daily"
TRIGGER_MANUAL = "manual"
TRIGGERS: tuple[str, ...] = (TRIGGER_DAILY, TRIGGER_MANUAL)

#: 实体条字段（manifest 中的完整键集；id 不参与「字段级差异」）
ENTITY_FIELDS: tuple[str, ...] = (
    "name",
    "dataset_id",
    "pk_columns",
    "description",
    "display_column",
    "column_notes",
    # 知识档位（add-entity-bench-contract 0050）：键恒出现（null 显式）
    "tier",
    "sensitivity",
)

#: 关系条字段（四元组是关系身份，参与差异的只有其余语义字段）
RELATIONSHIP_FIELDS: tuple[str, ...] = (
    "kind",
    "declared_kind",
    "label",
    "junction_dataset_id",
    "junction_source_column",
    "junction_target_column",
    # 知识档位（add-entity-bench-contract 0050）：键恒出现（null 显式）
    "tier",
    "sensitivity",
)


# ------------------------------------------------------------------ 规范化


def _iso(value: date | datetime) -> str:
    """日期/时间 → ISO8601（秒精度；naive 视为 UTC，不写时区后缀）。"""
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    return value.isoformat()


#: 条目归一（``_as_text`` / ``_as_int`` / ``_as_text_list`` / ``_as_notes``
#: 这组私有助手）已整体交给内核的 ``normalize_entity_v1`` /
#: ``normalize_relationship_v1``（establish-ontology-kernel 6.2）。


def entity_payload(entity: Entity) -> dict[str, Any]:
    """实体行 → manifest 实体条（键集固定；不含时间戳）。"""
    return {
        "id": entity.id,
        "name": entity.name,
        "dataset_id": entity.dataset_id,
        "pk_columns": list(entity.pk_columns or []),
        "description": entity.description,
        "display_column": entity.display_column,
        "column_notes": dict(entity.column_notes or {}) or None,
        "tier": entity.tier,
        "sensitivity": entity.sensitivity,
    }


def relationship_payload(rel: EntityRelationship) -> dict[str, Any]:
    """关系行 → manifest 关系条（**存储方向**；declared_kind 保留声明方向）。"""
    return {
        "id": rel.id,
        "from_entity_id": rel.from_entity_id,
        "from_column": rel.from_column,
        "to_entity_id": rel.to_entity_id,
        "to_column": rel.to_column,
        "kind": rel.kind,
        "declared_kind": rel.declared_kind,
        "label": rel.label,
        "junction_dataset_id": rel.junction_dataset_id,
        "junction_source_column": rel.junction_source_column,
        "junction_target_column": rel.junction_target_column,
        "tier": rel.tier,
        "sensitivity": rel.sensitivity,
    }


def normalize_entity(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """任意实体条 → 规范实体条（已知键恒出现、类型归一）。

    跨键集边界（0050 前快照无 tier/sensitivity 键）按缺失→null 归一
    （design D5）——diff/回滚 plan 在新旧快照间不因键缺席产生假差异。

    归一规则由内核对形状 v1 持有（establish-ontology-kernel 6.2）：本函数
    保留签名，实现交给内核，避免两个实现悄悄分叉。
    """
    return kernel_manifest.normalize_entity_v1(raw)


def normalize_relationship(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """任意关系条 → 规范关系条（已知键恒出现、类型归一；跨键集边界缺失键
    按 null 归一——同 normalize_entity 注记；归一规则同由内核持有）。"""
    return kernel_manifest.normalize_relationship_v1(raw)


def _entity_sort_key(item: Mapping[str, Any]) -> tuple:
    entity_id = item.get("id")
    if entity_id is not None:
        return (0, int(entity_id), "")
    return (1, 0, str(item.get("name") or ""))


def _relationship_sort_key(item: Mapping[str, Any]) -> tuple:
    def _num(value: Any) -> int:
        return int(value) if isinstance(value, int) else -1

    return (
        _num(item.get("from_entity_id")),
        str(item.get("from_column") or ""),
        _num(item.get("to_entity_id")),
        str(item.get("to_column") or ""),
        _num(item.get("id")),
    )


def canonical_manifest(
    entities: Iterable[Mapping[str, Any]],
    relationships: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """两组条目 → 规范 manifest（排序 + 归一；幂等）。

    形状 v1 的归一与排序键由内核持有（establish-ontology-kernel 6.2）；
    本函数是它在 fd-wire 这一侧的名字。
    """
    return kernel_manifest.canonical_payload(
        shape=kernel_manifest.SHAPE_ID_ADDRESSED,
        payload={"entities": list(entities), "relationships": list(relationships)},
    )


def empty_manifest() -> dict[str, list[dict[str, Any]]]:
    return {"entities": [], "relationships": []}


def manifest_from_rows(
    entities: Sequence[Entity], relationships: Sequence[EntityRelationship]
) -> dict[str, list[dict[str, Any]]]:
    return canonical_manifest(
        (entity_payload(entity) for entity in entities),
        (relationship_payload(rel) for rel in relationships),
    )


def manifest_is_empty(manifest: Mapping[str, Any]) -> bool:
    return not (manifest.get("entities") or manifest.get("relationships"))


# ------------------------------------------------------------------ digest


def canonical_json(manifest: Mapping[str, Any]) -> str:
    """规范 JSON 文本（键序与条序固定；digest 与稳定性测试的共同真源）。

    配方（``sort_keys`` + 紧凑分隔符 + ``ensure_ascii=False``）由内核持有：
    改配方会作废已发布的每一个 digest，所以只允许有一处定义。
    """
    return kernel_manifest.canonical_json(manifest)


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """manifest → sha256（十六进制）。空 manifest 也有稳定 digest。

    入参先归一再取 hash（内核 ``digest_for`` 的唯一顺序）——条序/键序/类型
    形态不影响 digest（同一内容恒同 digest，design D4）。
    """
    return kernel_manifest.digest_for(shape=kernel_manifest.SHAPE_ID_ADDRESSED, payload=manifest or {})


# ------------------------------------------------------------------ 当前态


def build_manifest(db: Session, *, customer_id: str) -> dict[str, list[dict[str, Any]]]:
    """读当前态（本体两表）→ 规范 manifest（纯读，不改任何状态）。"""
    entities = list(
        db.scalars(
            select(Entity)
            .where(Entity.customer_id == customer_id)
            .order_by(Entity.id)
        ).all()
    )
    relationships = list(
        db.scalars(
            select(EntityRelationship)
            .where(EntityRelationship.customer_id == customer_id)
            .order_by(EntityRelationship.id)
        ).all()
    )
    return manifest_from_rows(entities, relationships)


# ------------------------------------------------------------------ 事件流


def next_event_seq(db: Session, *, customer_id: str) -> int:
    """客户内下一个顺序号（max+1；同事务内取值——本体变更量级小，无争用）。"""
    current = db.scalar(
        select(func.max(OntologyEvent.seq)).where(
            OntologyEvent.customer_id == customer_id
        )
    )
    return int(current or 0) + 1


def _entity_names(
    db: Session, *, customer_id: str, ids: Iterable[int]
) -> dict[int, str]:
    """id → 实体名（一批取回；缺失的 id 不出现在结果里）。"""
    wanted = {int(i) for i in ids if i is not None}
    if not wanted:
        return {}
    rows = db.execute(
        select(Entity.id, Entity.name).where(
            Entity.customer_id == customer_id, Entity.id.in_(wanted)
        )
    ).all()
    return {int(row[0]): str(row[1]) for row in rows}


def target_key_for(
    db: Session,
    *,
    customer_id: str,
    target_kind: str,
    payload: Mapping[str, Any] | None,
) -> str | None:
    """事件信封的语义键（establish-ontology-kernel 6.3 / design D6）。

    - 实体条：``<实体名>``——客户内唯一（``uq_entities_customer_name``），
      所以它就是「这个实体」的可移植身份；
    - 关系条：``<from_entity_name>-><kind>:<to_entity_name>``——按**存储方向**
      写（声明方向在 ``declared_kind`` 里，不是身份的一部分）。

    拿不到名字（实体行已在同一次 flush 里删掉、或 id 缺失）时返回 ``None``：
    宁可留空，也不写一个反解不出来、看起来却像身份的字符串。
    """
    data = dict(payload or {})
    if target_kind == KIND_ENTITY:
        name = data.get("name")
        return str(name) if name else None
    if target_kind == KIND_RELATIONSHIP:
        from_id = data.get("from_entity_id")
        to_id = data.get("to_entity_id")
        kind = data.get("kind")
        if from_id is None or to_id is None or not kind:
            return None
        names = _entity_names(db, customer_id=customer_id, ids=(from_id, to_id))
        from_name = names.get(int(from_id))
        to_name = names.get(int(to_id))
        if not from_name or not to_name:
            return None
        return f"{from_name}->{kind}:{to_name}"
    return None


def record_event(
    db: Session,
    *,
    customer_id: str,
    op: str,
    target_kind: str,
    target_id: int,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    actor: str | None = None,
    via: str = VIA_CONSOLE,
    source_ref: str | None = None,
    source_instance_id: int | None = None,
) -> OntologyEvent:
    """追加一条本体变更事件（**只 append，无改删接口**）。

    与本体写入同会话同事务（design D1）：本函数 flush 失败会让整个事务
    失败——调用方（``entities.service``）不吞异常，故不存在「本体变了但
    没有事件」的窗口。

    ``target_key``（0054）在这里就地推导：语义键必须写当时的名字，事后反解
    只能得到今天的名字（establish-ontology-kernel 6.3）。
    """
    if op not in OPS:  # pragma: no cover - 内部调用点全是常量
        raise ValueError(f"未知事件 op: {op!r}")
    if target_kind not in KINDS:  # pragma: no cover
        raise ValueError(f"未知事件 target_kind: {target_kind!r}")
    if via not in VIAS:  # pragma: no cover
        raise ValueError(f"未知事件 via: {via!r}")
    target_key = target_key_for(
        db,
        customer_id=customer_id,
        target_kind=target_kind,
        payload=after if after is not None else before,
    )
    event = OntologyEvent(
        customer_id=customer_id,
        seq=next_event_seq(db, customer_id=customer_id),
        op=op,
        target_kind=target_kind,
        target_id=int(target_id),
        target_key=target_key,
        before_json=dict(before) if before is not None else None,
        after_json=dict(after) if after is not None else None,
        actor=actor[:200] if isinstance(actor, str) and actor else None,
        via=via,
        source_ref=source_ref[:256]
        if isinstance(source_ref, str) and source_ref
        else None,
        source_instance_id=source_instance_id,
    )
    db.add(event)
    db.flush()
    return event


def list_events(
    db: Session,
    *,
    customer_id: str,
    after_seq: int | None = None,
    after_created_at: datetime | None = None,
    order: str = "asc",
) -> list[OntologyEvent]:
    """本企业事件（默认按 ``(seq, id)`` 升序；可只取某点之后）。"""
    stmt = select(OntologyEvent).where(OntologyEvent.customer_id == customer_id)
    if after_seq is not None:
        stmt = stmt.where(OntologyEvent.seq > after_seq)
    if after_created_at is not None:
        stmt = stmt.where(OntologyEvent.created_at > after_created_at)
    stmt = stmt.order_by(OntologyEvent.seq, OntologyEvent.id)
    if order == "desc":
        stmt = stmt.order_by(OntologyEvent.seq.desc(), OntologyEvent.id.desc())
    return list(db.scalars(stmt).all())


def count_events_since(
    db: Session, *, customer_id: str, after_created_at: datetime | None
) -> int:
    """某时刻之后的事件数（「未发布变更」计数；None=全部）。"""
    stmt = select(func.count(OntologyEvent.id)).where(
        OntologyEvent.customer_id == customer_id
    )
    if after_created_at is not None:
        stmt = stmt.where(OntologyEvent.created_at > after_created_at)
    return int(db.scalar(stmt) or 0)


def latest_event_at(db: Session, *, customer_id: str) -> datetime | None:
    return db.scalar(
        select(func.max(OntologyEvent.created_at)).where(
            OntologyEvent.customer_id == customer_id
        )
    )


# ------------------------------------------------------------------ 快照读


def list_snapshots(
    db: Session, *, customer_id: str, order: str = "desc"
) -> list[OntologySnapshot]:
    """本企业全部快照（时间线倒序缺省）。"""
    stmt = select(OntologySnapshot).where(
        OntologySnapshot.customer_id == customer_id
    )
    if order == "desc":
        stmt = stmt.order_by(
            OntologySnapshot.version.desc(), OntologySnapshot.id.desc()
        )
    else:
        stmt = stmt.order_by(OntologySnapshot.version, OntologySnapshot.id)
    return list(db.scalars(stmt).all())


def latest_snapshot(db: Session, *, customer_id: str) -> OntologySnapshot | None:
    """本企业最新版本（无快照 → None）。"""
    return db.scalar(
        select(OntologySnapshot)
        .where(OntologySnapshot.customer_id == customer_id)
        .order_by(OntologySnapshot.version.desc(), OntologySnapshot.id.desc())
        .limit(1)
    )


def earliest_snapshot(db: Session, *, customer_id: str) -> OntologySnapshot | None:
    """本企业最早版本（完整性自检的基线，design D6）。"""
    return db.scalar(
        select(OntologySnapshot)
        .where(OntologySnapshot.customer_id == customer_id)
        .order_by(OntologySnapshot.version, OntologySnapshot.id)
        .limit(1)
    )


def get_snapshot(
    db: Session, *, customer_id: str, version: int
) -> OntologySnapshot | None:
    """指定版本（跨企业/不存在同 None——路由层一律 404，不泄露）。"""
    return db.scalar(
        select(OntologySnapshot).where(
            OntologySnapshot.customer_id == customer_id,
            OntologySnapshot.version == version,
        )
    )


def snapshot_manifest(snapshot: OntologySnapshot) -> dict[str, list[dict[str, Any]]]:
    """快照行 → 规范 manifest（存储形态再归一，防手工改库）。"""
    raw = snapshot.manifest_json or empty_manifest()
    return canonical_manifest(
        raw.get("entities") or [], raw.get("relationships") or []
    )


def snapshot_summary(snapshot: OntologySnapshot) -> dict[str, Any]:
    """时间线条目（不含全量 manifest——列表面只给规模计数）。"""
    manifest = snapshot_manifest(snapshot)
    return {
        "version": snapshot.version,
        # 同上：manifest 形状版本进时间线条目，消费方不必先下全量导出
        "manifest_schema_version": kernel_manifest.SHAPE_ID_ADDRESSED,
        "digest": snapshot.digest,
        "trigger": snapshot.trigger,
        "label": snapshot.label,
        "created_by": snapshot.created_by,
        "created_at": snapshot.created_at,
        "entity_count": len(manifest["entities"]),
        "relationship_count": len(manifest["relationships"]),
    }


# ------------------------------------------------------------------ diff


def _changed_fields(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    fields: Sequence[str],
) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    for field in fields:
        old = before.get(field)
        new = after.get(field)
        if old != new:
            changes[field] = {"before": old, "after": new}
    return changes


def _entity_identity(item: Mapping[str, Any]) -> tuple:
    """实体匹配键：有 id 用 id，否则用业务名（实体名客户内唯一）。"""
    if item.get("id") is not None:
        return ("id", int(item["id"]))
    return ("name", str(item.get("name") or ""))


def _relationship_identity(item: Mapping[str, Any]) -> tuple:
    """关系匹配键：四元组（客户内唯一）；缺失两端 id 时退回关系 id。"""
    if item.get("from_entity_id") is None or item.get("to_entity_id") is None:
        return ("id", item.get("id"))
    return (
        "quad",
        item.get("from_entity_id"),
        str(item.get("from_column") or ""),
        item.get("to_entity_id"),
        str(item.get("to_column") or ""),
    )


def _diff_entities(
    base: Sequence[Mapping[str, Any]], target: Sequence[Mapping[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    base_items = [normalize_entity(item) for item in base]
    target_items = [normalize_entity(item) for item in target]
    base_by_key = {_entity_identity(item): item for item in base_items}
    target_by_key = {_entity_identity(item): item for item in target_items}

    added = [
        item for key, item in target_by_key.items() if key not in base_by_key
    ]
    removed = [
        item for key, item in base_by_key.items() if key not in target_by_key
    ]
    changed: list[dict[str, Any]] = []
    for key, target_item in target_by_key.items():
        base_item = base_by_key.get(key)
        if base_item is None:
            continue
        fields = _changed_fields(base_item, target_item, ENTITY_FIELDS)
        if fields:
            changed.append(
                {
                    "id": target_item.get("id"),
                    "name": target_item.get("name"),
                    "before": base_item,
                    "after": target_item,
                    "fields": fields,
                }
            )
    return {
        "added": sorted(added, key=_entity_sort_key),
        "removed": sorted(removed, key=_entity_sort_key),
        "changed": sorted(
            changed,
            key=lambda item: (
                item.get("id") is None,
                item.get("id") or 0,
                str(item.get("name") or ""),
            ),
        ),
    }


def _diff_relationships(
    base: Sequence[Mapping[str, Any]], target: Sequence[Mapping[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    base_items = [normalize_relationship(item) for item in base]
    target_items = [normalize_relationship(item) for item in target]
    base_by_key = {_relationship_identity(item): item for item in base_items}
    target_by_key = {_relationship_identity(item): item for item in target_items}

    added = [
        item for key, item in target_by_key.items() if key not in base_by_key
    ]
    removed = [
        item for key, item in base_by_key.items() if key not in target_by_key
    ]
    changed: list[dict[str, Any]] = []
    for key, target_item in target_by_key.items():
        base_item = base_by_key.get(key)
        if base_item is None:
            continue
        fields = _changed_fields(base_item, target_item, RELATIONSHIP_FIELDS)
        if fields:
            changed.append(
                {
                    "id": target_item.get("id"),
                    "before": base_item,
                    "after": target_item,
                    "fields": fields,
                }
            )
    return {
        "added": sorted(added, key=_relationship_sort_key),
        "removed": sorted(removed, key=_relationship_sort_key),
        "changed": sorted(changed, key=lambda item: _relationship_sort_key(item["after"])),
    }


def _attach_provenance(
    item: dict[str, Any],
    *,
    kind: str,
    provenance: Mapping[tuple[str, int], str | None] | None,
) -> dict[str, Any]:
    """给差异条目附舰队血缘（无血缘 → 键缺席，不写空串；spec「改动条目可携带出处」）。"""
    if provenance is None:
        return item
    entry = dict(item)
    target = entry.get("after") if isinstance(entry.get("after"), dict) else entry
    target_id = target.get("id") if isinstance(target, dict) else None
    if target_id is None:
        return entry
    ref = provenance.get((kind, int(target_id)))
    if ref:
        entry["source_ref"] = ref
    return entry


def diff_manifests(
    base: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    provenance: Mapping[tuple[str, int], str | None] | None = None,
) -> dict[str, Any]:
    """两 manifest 对比 → 实体/关系三组（增/删/字段级改）。

    输入两侧都先规范归一，故「manifest ↔ 当前态」两种来源可自由互比
    （任务 4.1）；实体按 id（缺失回退业务名）、关系按四元组匹配。
    ``provenance`` 给定时每条差异附 ``source_ref``（最近一次 create/update
    事件的舰队血缘；无血缘的条目键缺席）。
    """
    entities = _diff_entities(
        base.get("entities") or [], target.get("entities") or []
    )
    relationships = _diff_relationships(
        base.get("relationships") or [], target.get("relationships") or []
    )
    if provenance is not None:
        entities = {
            group: [
                _attach_provenance(item, kind=KIND_ENTITY, provenance=provenance)
                for item in items
            ]
            for group, items in entities.items()
        }
        relationships = {
            group: [
                _attach_provenance(
                    item, kind=KIND_RELATIONSHIP, provenance=provenance
                )
                for item in items
            ]
            for group, items in relationships.items()
        }
    entity_counts = {
        "added": len(entities["added"]),
        "removed": len(entities["removed"]),
        "changed": len(entities["changed"]),
    }
    relationship_counts = {
        "added": len(relationships["added"]),
        "removed": len(relationships["removed"]),
        "changed": len(relationships["changed"]),
    }
    return {
        "entities": entities,
        "relationships": relationships,
        "counts": {
            "entities": {
                **entity_counts,
                "total": sum(entity_counts.values()),
            },
            "relationships": {
                **relationship_counts,
                "total": sum(relationship_counts.values()),
            },
        },
        "has_changes": bool(
            sum(entity_counts.values()) + sum(relationship_counts.values())
        ),
    }


# ------------------------------------------------------------------ 回滚 plan


def rollback_plan(
    current: Mapping[str, Any], target: Mapping[str, Any]
) -> dict[str, Any]:
    """当前态 vs 目标版本 → 可预览的执行计划（design D3）。

    关系没有 update 通道（四元组即身份），故关系的字段级改在 plan 里拆成
    「删旧 + 建新」——仍经 service 逐条执行、逐条落事件，历史只增不减。
    """
    diff = diff_manifests(current, target)
    entity_diff = diff["entities"]
    relationship_diff = diff["relationships"]
    return {
        "create_entities": [
            normalize_entity(item) for item in entity_diff["added"]
        ],
        "update_entities": [
            {
                "id": item["after"].get("id"),
                "name": item["after"].get("name"),
                "fields": item["fields"],
                "before": item["before"],
                "after": item["after"],
            }
            for item in entity_diff["changed"]
        ],
        "delete_entities": [
            normalize_entity(item) for item in entity_diff["removed"]
        ],
        "create_relationships": [
            normalize_relationship(item)
            for item in relationship_diff["added"]
            + [item["after"] for item in relationship_diff["changed"]]
        ],
        "delete_relationships": [
            normalize_relationship(item)
            for item in relationship_diff["removed"]
            + [item["before"] for item in relationship_diff["changed"]]
        ],
        "summary": {
            "entities": {
                "create": len(entity_diff["added"]),
                "update": len(entity_diff["changed"]),
                "delete": len(entity_diff["removed"]),
            },
            "relationships": {
                "create": len(relationship_diff["added"])
                + len(relationship_diff["changed"]),
                "delete": len(relationship_diff["removed"])
                + len(relationship_diff["changed"]),
            },
        },
        "diff": diff,
    }


def plan_is_empty(plan: Mapping[str, Any]) -> bool:
    summary = plan.get("summary") or {}
    for bucket in summary.values():
        if any(int(count or 0) for count in bucket.values()):
            return False
    return True


# ------------------------------------------------------------------ 回滚执行


class RollbackError(RuntimeError):
    """回滚中途失败（已执行部分留事件；调用方如实上报）。"""

    def __init__(self, message: str, *, executed: list[dict[str, Any]]):
        super().__init__(message)
        self.executed = executed


def execute_rollback(
    db: Session,
    datasets_db: Session,
    *,
    customer_id: str,
    plan: Mapping[str, Any],
    actor: str | None = None,
    domain_db: Session | None = None,
) -> dict[str, Any]:
    """按 plan 逐条经 service 执行（append-only；每条落 ``via=rollback`` 事件）。

    顺序：删关系 → 删实体 → 改实体 → 建实体 → 建关系（先删后建避免四元组/
    业务名唯一约束的中间态冲突）。任一校验失败**如实停**：已执行条目已落
    事件（可再回滚修正），异常以 :class:`RollbackError` 上抛并带 executed。

    身份纪律：建实体/关系时**沿用 manifest 的 id**（digest 可对齐）；目标 id
    已被占用的极端情形退回自增，并在结果 ``identity_changed`` 里如实记录。

    ``domain_db``（ai 元数据会话）随删实体一并注入——域动作类型级联停用与
    控制台删除同口径（跨库拓扑由调用方负责，production 两库分置）。
    """
    from . import service as entities_service  # noqa: PLC0415 —— 环规避

    executed: list[dict[str, Any]] = []
    identity_changed: list[dict[str, Any]] = []

    def _entity_by_id(entity_id: int | None) -> Entity | None:
        if entity_id is None:
            return None
        row = db.get(Entity, int(entity_id))
        if row is None or row.customer_id != customer_id:
            return None
        return row

    def _relationship_by_id(rel_id: int | None) -> EntityRelationship | None:
        if rel_id is None:
            return None
        row = db.get(EntityRelationship, int(rel_id))
        if row is None or row.customer_id != customer_id:
            return None
        return row

    def _step(entry: Mapping[str, Any], fn) -> None:
        try:
            fn()
            executed.append(dict(entry))
        except Exception as exc:  # noqa: BLE001 —— 如实停：已执行部分留事件
            raise RollbackError(
                f"回滚在第 {len(executed) + 1} 步失败（{entry.get('action')} "
                f"{entry.get('target')}）：{exc}",
                executed=executed,
            ) from exc

    # 1. 删关系（含「字段级改」的删除半）
    for item in plan.get("delete_relationships") or []:
        payload = normalize_relationship(item)
        rel_id = payload.get("id")
        target = _relationship_by_id(rel_id)
        if target is None:
            continue  # 已不在（幂等：plan 生成后被并发删掉）

        def _delete(target=target) -> None:
            # 端点参数只是定位入口（relation 两端任一即可）；事件由 service 落
            entities_service.delete_relationship(
                db,
                customer_id=customer_id,
                entity_id=target.from_entity_id,
                rel_id=target.id,
                actor=actor,
                via=VIA_ROLLBACK,
            )

        _step(
            {"action": "delete_relationship", "target": rel_id},
            _delete,
        )

    # 2. 删实体（级联两端关系，逐条留事件）
    for item in plan.get("delete_entities") or []:
        payload = normalize_entity(item)

        def _delete_entity(payload=payload) -> None:
            entity = _entity_by_id(payload.get("id"))
            if entity is None:
                return  # 已不在
            entities_service.delete_entity(
                db, entity, domain_db=domain_db, actor=actor, via=VIA_ROLLBACK
            )

        _step(
            {"action": "delete_entity", "target": payload.get("id")},
            _delete_entity,
        )

    # 3. 改实体（目标态字段整量写回；校验走既有同一链）
    for item in plan.get("update_entities") or []:
        after = normalize_entity(item.get("after"))

        def _update(after=after) -> None:
            entity = _entity_by_id(after.get("id"))
            if entity is None:
                raise entities_service.EntityNotFound(
                    f"实体 {after.get('id')} 不存在（回滚目标行失存）"
                )
            entities_service.update_entity(
                db,
                datasets_db,
                entity,
                name=after["name"],
                dataset_id=after["dataset_id"],
                pk_columns=list(after["pk_columns"]),
                description=after["description"],
                display_column=after["display_column"],
                column_notes=after["column_notes"],
                tier=after["tier"],
                sensitivity=after["sensitivity"],
                actor=actor,
                via=VIA_ROLLBACK,
            )

        _step(
            {"action": "update_entity", "target": after.get("id")},
            _update,
        )

    # 4. 建实体（沿 manifest id；被占用则退回自增并留痕）
    for item in plan.get("create_entities") or []:
        payload = normalize_entity(item)
        desired_id = payload.get("id")

        def _create(payload=payload, desired_id=desired_id) -> None:
            restore_id = desired_id
            if restore_id is not None and _entity_by_id(restore_id) is not None:
                restore_id = None
            entity = entities_service.create_entity(
                db,
                datasets_db,
                customer_id=customer_id,
                name=payload["name"],
                dataset_id=payload["dataset_id"],
                pk_columns=list(payload["pk_columns"]),
                description=payload["description"],
                display_column=payload["display_column"],
                column_notes=payload["column_notes"],
                tier=payload["tier"],
                sensitivity=payload["sensitivity"],
                actor=actor,
                via=VIA_ROLLBACK,
                entity_id=restore_id,
            )
            if desired_id is not None and entity.id != desired_id:
                identity_changed.append(
                    {
                        "kind": KIND_ENTITY,
                        "version_id": desired_id,
                        "restored_id": entity.id,
                        "reason": "目标 id 已被占用，退回自增",
                    }
                )

        _step({"action": "create_entity", "target": payload.get("name")}, _create)

    # 5. 建关系（沿 manifest id；需两端实体已在位）
    for item in plan.get("create_relationships") or []:
        payload = normalize_relationship(item)
        desired_id = payload.get("id")

        def _create_rel(payload=payload, desired_id=desired_id) -> None:
            source = _entity_by_id(payload.get("from_entity_id"))
            if source is None:
                raise entities_service.EntityNotFound(
                    f"源实体 {payload.get('from_entity_id')} 不存在"
                )
            restore_id = desired_id
            if restore_id is not None and _relationship_by_id(restore_id) is not None:
                restore_id = None
            rel = entities_service.create_relationship(
                db,
                datasets_db,
                customer_id=customer_id,
                source_entity=source,
                to_entity_id=int(payload["to_entity_id"]),
                from_column=payload["from_column"],
                to_column=payload["to_column"],
                kind=payload["kind"],
                label=payload["label"],
                junction_dataset_id=payload["junction_dataset_id"],
                junction_source_column=payload["junction_source_column"],
                junction_target_column=payload["junction_target_column"],
                tier=payload["tier"],
                sensitivity=payload["sensitivity"],
                actor=actor,
                via=VIA_ROLLBACK,
                relationship_id=restore_id,
            )
            if desired_id is not None and rel.id != desired_id:
                identity_changed.append(
                    {
                        "kind": KIND_RELATIONSHIP,
                        "version_id": desired_id,
                        "restored_id": rel.id,
                        "reason": "目标 id 已被占用，退回自增",
                    }
                )

        _step(
            {"action": "create_relationship", "target": payload.get("id")},
            _create_rel,
        )

    db.flush()
    return {"executed": executed, "identity_changed": identity_changed}


# ------------------------------------------------------------------ 折叠 / 完整性


def fold_events(
    baseline: Mapping[str, Any] | None, events: Iterable[OntologyEvent]
) -> dict[str, list[dict[str, Any]]]:
    """基线 manifest + 事件序列 → 「事件说应该是什么」（规范 manifest）。"""
    entity_state: dict[int, dict[str, Any]] = {}
    relationship_state: dict[int, dict[str, Any]] = {}
    buckets: dict[str, dict[int, dict[str, Any]]] = {
        KIND_ENTITY: entity_state,
        KIND_RELATIONSHIP: relationship_state,
    }
    if baseline:
        for item in baseline.get("entities") or []:
            normalized = normalize_entity(item)
            if normalized.get("id") is not None:
                entity_state[int(normalized["id"])] = normalized
        for item in baseline.get("relationships") or []:
            normalized = normalize_relationship(item)
            if normalized.get("id") is not None:
                relationship_state[int(normalized["id"])] = normalized

    for event in events:
        bucket = buckets.get(event.target_kind)
        if bucket is None:  # pragma: no cover - 库内枚举恒受 service 约束
            continue
        target_id = int(event.target_id)
        if event.op == OP_DELETE:
            bucket.pop(target_id, None)
        elif event.after_json is not None:
            if event.target_kind == KIND_ENTITY:
                payload = normalize_entity(event.after_json)
            else:
                payload = normalize_relationship(event.after_json)
            payload["id"] = target_id
            bucket[target_id] = payload

    return canonical_manifest(entity_state.values(), relationship_state.values())


def integrity_report(db: Session, *, customer_id: str) -> dict[str, Any]:
    """完整性自检：事件折叠 vs 当前态（design D10；检测绕过 service 的直改）。

    基线取**最早快照**（若有）——那是「启用时刻的当前态」（design D6），
    只折叠其 ``created_at`` 之后的事件，避免把启用前存量当变更双算；
    无快照则从空基线折叠全部事件（机制启用后未打过版的客户）。
    """
    baseline_snapshot = earliest_snapshot(db, customer_id=customer_id)
    baseline = (
        snapshot_manifest(baseline_snapshot) if baseline_snapshot is not None else None
    )
    events = list_events(
        db,
        customer_id=customer_id,
        after_created_at=baseline_snapshot.created_at
        if baseline_snapshot is not None
        else None,
    )
    folded = fold_events(baseline, events)
    current = build_manifest(db, customer_id=customer_id)
    diff = diff_manifests(folded, current)
    return {
        "customer_id": customer_id,
        "consistent": not diff["has_changes"],
        "baseline": {
            "kind": "snapshot" if baseline_snapshot is not None else "empty",
            "version": baseline_snapshot.version if baseline_snapshot is not None else None,
            "digest": manifest_digest(baseline) if baseline is not None else None,
            "events_folded": len(events),
        },
        "current": {
            "digest": manifest_digest(current),
            "entity_count": len(current["entities"]),
            "relationship_count": len(current["relationships"]),
        },
        "folded_digest": manifest_digest(folded),
        "differences": diff,
        "escape_hint": (
            None
            if not diff["has_changes"]
            else "当前态与事件流不一致：可能存在绕过 entities/service.py 的直改"
            "（裸 SQL）；事件流无法凭自身解释这些条目，详见 differences"
        ),
    }


# ------------------------------------------------------------------ 出处 / 导出


def provenance_map(db: Session, *, customer_id: str) -> dict[tuple[str, int], str | None]:
    """每条实体/关系最近一次 create/update 事件的 source_ref（无则 None）。

    「最近一次」按 ``(seq, id)`` 取，故回滚/控制台直改会把舰队血缘覆盖为
    null——导出物如实呈现「最后一次变更不是经闸来的」。
    """
    stmt = (
        select(OntologyEvent)
        .where(
            OntologyEvent.customer_id == customer_id,
            OntologyEvent.op.in_((OP_CREATE, OP_UPDATE)),
        )
        .order_by(OntologyEvent.seq, OntologyEvent.id)
    )
    result: dict[tuple[str, int], str | None] = {}
    for event in db.scalars(stmt).all():
        result[(event.target_kind, int(event.target_id))] = event.source_ref
    return result


def build_export_document(
    manifest: Mapping[str, Any],
    *,
    customer_id: str,
    version: int | str,
    provenance: Mapping[tuple[str, int], str | None] | None = None,
    exported_at: datetime | None = None,
) -> dict[str, Any]:
    """导出文档（design D7）：schema_version/exported_at/customer_id/version/
    digest/entities/relationships（可选 provenance）。

    digest 只覆盖内容（不含 exported_at/version）——同一内容在任何时刻
    导出、或与快照记录对比，digest 恒一致。
    """
    normalized = canonical_manifest(
        manifest.get("entities") or [], manifest.get("relationships") or []
    )
    entities = []
    for item in normalized["entities"]:
        entry = dict(item)
        if provenance is not None:
            ref = provenance.get((KIND_ENTITY, int(item["id"])))
            if ref:  # 无经闸血缘 → 该键缺席（不写空串）
                entry["source_ref"] = ref
        entities.append(entry)
    relationships = []
    for item in normalized["relationships"]:
        entry = dict(item)
        if provenance is not None:
            ref = provenance.get((KIND_RELATIONSHIP, int(item["id"])))
            if ref:
                entry["source_ref"] = ref
        relationships.append(entry)
    return {
        "schema_version": SCHEMA_VERSION,
        # 内核 manifest 形状版本（establish-ontology-kernel 6.5）：与
        # ``schema_version``（导出文档自身版本）是两个轴，消费方按这个字段决定
        # 怎么解释 entities/relationships。老消费者忽略未知键即照旧解析。
        "manifest_schema_version": kernel_manifest.SHAPE_ID_ADDRESSED,
        "exported_at": _iso(exported_at or utc_now_naive()),
        "customer_id": customer_id,
        "version": version,
        "digest": manifest_digest(normalized),
        "entities": entities,
        "relationships": relationships,
    }


__all__ = [
    "ENTITY_FIELDS",
    "KINDS",
    "KIND_ENTITY",
    "KIND_RELATIONSHIP",
    "OPS",
    "OP_CREATE",
    "OP_DELETE",
    "OP_UPDATE",
    "RELATIONSHIP_FIELDS",
    "SCHEMA_VERSION",
    "TRIGGERS",
    "TRIGGER_DAILY",
    "TRIGGER_MANUAL",
    "VIAS",
    "VIA_ACTION_GATE",
    "VIA_CONSOLE",
    "VIA_ROLLBACK",
    "RollbackError",
    "build_export_document",
    "build_manifest",
    "canonical_json",
    "canonical_manifest",
    "count_events_since",
    "diff_manifests",
    "earliest_snapshot",
    "empty_manifest",
    "entity_payload",
    "execute_rollback",
    "fold_events",
    "get_snapshot",
    "integrity_report",
    "latest_event_at",
    "latest_snapshot",
    "list_events",
    "list_snapshots",
    "manifest_digest",
    "manifest_from_rows",
    "manifest_is_empty",
    "next_event_seq",
    "normalize_entity",
    "normalize_relationship",
    "plan_is_empty",
    "provenance_map",
    "record_event",
    "relationship_payload",
    "rollback_plan",
    "snapshot_manifest",
    "snapshot_summary",
]
