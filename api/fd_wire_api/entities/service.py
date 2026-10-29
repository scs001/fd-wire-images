"""实体语义层服务（wire-entity-layer）：校验、CRUD 组装、级联删除与
图谱紧凑文本块（AI 构建上下文注入的唯一同源）。

校验纪律（design D2）：

- 实体：name 非空 strip ≤100；dataset 存在且属本企业（datasets 库会话查
  ``Dataset`` 行——生产 wire_platform/wire_datasets 分库，绝不能用元数据
  库会话查）；pk_columns 非空、无重复、逐列 ∈ dataset.columns_json
  （列名级报错）；重复业务名 / 重复绑定数据集 → 冲突（路由转 409，
  detail 带冲突方），区别于 422 结构错。
- 关系：两端实体同 customer；from_column ∈ 源实体数据集列；
  **to_column ∈ 目标实体 pk_columns（纪律：关系指向身份）**；kind 枚举
  ``n:1``|``1:1``；四元组唯一 → 409；自环（同一实体）允许。
- 删除实体：同事务先删两端关系再删实体，返回删除的关系计数。

所有写操作只 flush 不 commit——提交归请求级会话依赖（``db.get_db``）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from ..datasets.models import Dataset
from .models import Entity, EntityRelationship

#: 关系基数枚举（v2：多对一 / 一对一 / 一对多反向声明 / 多对多经中间集）
RELATIONSHIP_KINDS: tuple[str, ...] = ("n:1", "1:1", "1:n", "m:n")

#: 实体业务名上限
MAX_NAME_LENGTH = 100

#: 列注记上限（wire-entity-v2）：每条 ≤50 字、每实体 ≤50 列
MAX_COLUMN_NOTE_LENGTH = 50
MAX_COLUMN_NOTES = 50

#: AI 上下文注入的有界（design D4）：超限截断并如实标注
PROMPT_MAX_ENTITIES = 50
PROMPT_MAX_RELATIONSHIPS = 100


class EntityNotFound(LookupError):
    """实体/关系不存在（或不属本企业）——路由层转 404（不泄露他企存在性）。"""


class InvalidEntity(ValueError):
    """实体/关系声明校验失败——路由层转 422（逐条文案，零半写）。"""


class EntityConflict(ValueError):
    """唯一性冲突（重名/重复绑定/四元组重复）——路由层转 409（detail 带冲突方）。"""


# ------------------------------------------------------------------ 实体校验


def _validate_name(name: Any) -> str:
    """业务名：非空字符串，strip 后非空且 ≤100。"""
    if not isinstance(name, str):
        raise InvalidEntity("实体名必须是非空字符串")
    stripped = name.strip()
    if not stripped:
        raise InvalidEntity("实体名必须是非空字符串（去除首尾空白后非空）")
    if len(stripped) > MAX_NAME_LENGTH:
        raise InvalidEntity(
            f"实体名长度超过 {MAX_NAME_LENGTH} 上限（当前 {len(stripped)} 字符）"
        )
    return stripped


def _owned_dataset_or_none(
    datasets_db: Session, dataset_id: str, customer_id: str
) -> Dataset | None:
    """本企业数据集行（datasets 库会话——生产分库，跨客户与不存在同 None，
    不泄露属主差异）；drift（行已删）同样 None。"""
    dataset = datasets_db.get(Dataset, dataset_id)
    if dataset is None or dataset.customer_id != customer_id:
        return None
    return dataset


def _dataset_column_names(dataset: Dataset) -> set[str]:
    return {
        column.get("name")
        for column in (dataset.columns_json or [])
        if isinstance(column, dict)
    }


def _validate_pk_columns(pk_columns: Any, dataset: Dataset) -> list[str]:
    """主键列：非空字符串数组、无重复、逐列 ∈ dataset.columns_json。"""
    if not isinstance(pk_columns, list) or not pk_columns:
        raise InvalidEntity("主键列必须是非空字符串数组（至少一列）")
    seen: set[str] = set()
    for column in pk_columns:
        if not isinstance(column, str) or not column:
            raise InvalidEntity(
                f"主键列必须是非空字符串（收到 {column!r}）"
            )
        if column in seen:
            raise InvalidEntity(f"主键列重复: {column}")
        seen.add(column)
    known = _dataset_column_names(dataset)
    unknown = [column for column in pk_columns if column not in known]
    if unknown:
        raise InvalidEntity(
            f"主键列不存在于数据集 {dataset.id} 的列快照: {', '.join(unknown)}"
        )
    return list(pk_columns)


def _validate_display_column(display_column: Any, dataset: Dataset) -> str | None:
    """显示列（v2 属性元数据）：可空；非空时须 ∈ 数据集列快照。"""
    if display_column is None:
        return None
    if not isinstance(display_column, str) or not display_column.strip():
        return None
    if display_column not in _dataset_column_names(dataset):
        raise InvalidEntity(
            f"显示列 {display_column} 不在数据集 {dataset.id} 的列快照中"
        )
    return display_column


def _validate_column_notes(column_notes: Any, dataset: Dataset) -> dict | None:
    """列注记（v2 属性元数据）：{column: 注记}，列 ∈ 快照、注记 ≤50 字、≤50 列。"""
    if column_notes is None:
        return None
    if not isinstance(column_notes, dict):
        raise InvalidEntity("列注记必须是对象（{列名: 注记}）")
    known = _dataset_column_names(dataset)
    notes: dict[str, str] = {}
    for column, note in column_notes.items():
        if column not in known:
            raise InvalidEntity(f"列注记的列 {column} 不在数据集 {dataset.id} 列快照中")
        if not isinstance(note, str) or not note.strip():
            raise InvalidEntity(f"列 {column} 的注记必须是非空字符串")
        if len(note.strip()) > MAX_COLUMN_NOTE_LENGTH:
            raise InvalidEntity(
                f"列 {column} 的注记超过 {MAX_COLUMN_NOTE_LENGTH} 字上限"
            )
        notes[column] = note.strip()
    if len(notes) > MAX_COLUMN_NOTES:
        raise InvalidEntity(f"列注记最多 {MAX_COLUMN_NOTES} 列")
    return notes or None


def _check_name_conflict(
    db: Session, customer_id: str, name: str, *, exclude_id: int | None = None
) -> None:
    stmt = select(Entity).where(
        Entity.customer_id == customer_id, Entity.name == name
    )
    dup = db.scalar(stmt)
    if dup is not None and dup.id != exclude_id:
        raise EntityConflict(
            f"实体名「{name}」已被使用（冲突实体：{dup.id}「{dup.name}」，"
            f"绑定数据集 {dup.dataset_id}）"
        )


def _check_dataset_binding_conflict(
    db: Session, customer_id: str, dataset_id: str, *, exclude_id: int | None = None
) -> None:
    stmt = select(Entity).where(
        Entity.customer_id == customer_id, Entity.dataset_id == dataset_id
    )
    bound = db.scalar(stmt)
    if bound is not None and bound.id != exclude_id:
        raise EntityConflict(
            f"数据集 {dataset_id} 已绑定实体（冲突实体：{bound.id}「{bound.name}」"
            "——每数据集至多一个实体声明）"
        )


# ------------------------------------------------------------------ 实体 CRUD


def get_owned_entity(
    db: Session, *, entity_id: int, customer_id: str
) -> Entity:
    """客户侧实体：不存在或不属本企业一律 EntityNotFound（404 语义）。"""
    entity = db.get(Entity, entity_id)
    if entity is None or entity.customer_id != customer_id:
        raise EntityNotFound(f"实体 {entity_id} 不存在")
    return entity


def create_entity(
    db: Session,
    datasets_db: Session,
    *,
    customer_id: str,
    name: str,
    dataset_id: str,
    pk_columns: list,
    description: str | None = None,
    display_column: str | None = None,
    column_notes: dict | None = None,
) -> Entity:
    """声明实体：name/dataset 属主/pk ⊆ 列快照 全过 + 双唯一检查后落行。"""
    name = _validate_name(name)
    dataset = _owned_dataset_or_none(datasets_db, dataset_id, customer_id)
    if dataset is None:
        raise InvalidEntity(f"数据集 {dataset_id} 不存在")
    columns = _validate_pk_columns(pk_columns, dataset)
    display = _validate_display_column(display_column, dataset)
    notes = _validate_column_notes(column_notes, dataset)
    _check_name_conflict(db, customer_id, name)
    _check_dataset_binding_conflict(db, customer_id, dataset_id)
    entity = Entity(
        customer_id=customer_id,
        name=name,
        dataset_id=dataset_id,
        pk_columns=columns,
        description=(description.strip() or None)
        if isinstance(description, str)
        else None,
        display_column=display,
        column_notes=notes,
    )
    db.add(entity)
    db.flush()
    return entity


def update_entity(
    db: Session,
    datasets_db: Session,
    entity: Entity,
    *,
    name: str,
    dataset_id: str,
    pk_columns: list,
    description: str | None = None,
    display_column: str | None = None,
    column_notes: dict | None = None,
) -> Entity:
    """整量更新（重校验——与创建同一条校验链，排除自身后的双唯一检查）。"""
    name = _validate_name(name)
    dataset = _owned_dataset_or_none(datasets_db, dataset_id, entity.customer_id)
    if dataset is None:
        raise InvalidEntity(f"数据集 {dataset_id} 不存在")
    columns = _validate_pk_columns(pk_columns, dataset)
    display = _validate_display_column(display_column, dataset)
    notes = _validate_column_notes(column_notes, dataset)
    _check_name_conflict(db, entity.customer_id, name, exclude_id=entity.id)
    _check_dataset_binding_conflict(
        db, entity.customer_id, dataset_id, exclude_id=entity.id
    )
    entity.name = name
    entity.dataset_id = dataset_id
    entity.pk_columns = columns
    entity.description = (
        (description.strip() or None) if isinstance(description, str) else None
    )
    entity.display_column = display
    entity.column_notes = notes
    db.flush()
    return entity


def delete_entity(
    db: Session, entity: Entity, *, domain_db: Session | None = None
) -> int:
    """删实体 = 同事务先删两端关系再删实体；返回删除的关系数（留痕）。

    域动作类型级联停用（wire-domain-actions：enabled=False，历史实例保留
    可查）——``domain_db``（ai 元数据会话）由路由注入；None 时跳过（跨库
    拓扑由调用方负责，直调兼容）。"""
    if domain_db is not None:
        from ..domain_actions import service as domain_service  # noqa: PLC0415

        domain_service.disable_for_entity(
            domain_db, customer_id=entity.customer_id, entity_id=entity.id
        )
    result = db.execute(
        delete(EntityRelationship).where(
            EntityRelationship.customer_id == entity.customer_id,
            or_(
                EntityRelationship.from_entity_id == entity.id,
                EntityRelationship.to_entity_id == entity.id,
            ),
        )
    )
    removed = int(result.rowcount or 0)
    db.delete(entity)
    db.flush()
    return removed


def list_entities(db: Session, *, customer_id: str) -> list[Entity]:
    """本企业实体列表（创建序稳定）。"""
    stmt = (
        select(Entity)
        .where(Entity.customer_id == customer_id)
        .order_by(Entity.id)
    )
    return list(db.scalars(stmt).all())


# ------------------------------------------------------------------ 关系


def _quad_conflict(
    db: Session,
    *,
    from_entity_id: int,
    from_column: str,
    to_entity_id: int,
    to_column: str,
) -> None:
    dup = db.scalar(
        select(EntityRelationship).where(
            EntityRelationship.from_entity_id == from_entity_id,
            EntityRelationship.from_column == from_column,
            EntityRelationship.to_entity_id == to_entity_id,
            EntityRelationship.to_column == to_column,
        )
    )
    if dup is not None:
        raise EntityConflict(
            f"关系已存在（冲突关系 id={dup.id}：四元组 源实体/源列/目标实体/"
            f"目标列 唯一——{from_entity_id}.{from_column} →({dup.kind}) "
            f"{to_entity_id}.{to_column}）"
        )


def create_relationship(
    db: Session,
    datasets_db: Session,
    *,
    customer_id: str,
    source_entity: Entity,
    to_entity_id: int,
    from_column: str,
    to_column: str,
    kind: str,
    label: str | None = None,
    junction_dataset_id: str | None = None,
    junction_source_column: str | None = None,
    junction_target_column: str | None = None,
) -> EntityRelationship:
    """声明关系（v2 四种基数）。

    - ``n:1`` / ``1:1``（v0 语义零漂移）：from_column ∈ 源集列、
      to_column ∈ 目标实体主键（关系指向身份）；
    - ``1:n``：n:1 的**反向声明**——声明源列须 ∈ 源实体主键（「一」侧身份）、
      声明目标列 ∈ 目标集列（「多」侧携带外键列）；**归一化存储**为反向
      n:1（from/to 对调、kind="n:1"、declared_kind="1:n"），正反向查重
      按归一化后四元组（同四元组重复/反向重复一律 409）；
    - ``m:n``：经**中间数据集**声明——from_column/to_column 仍为两端主键列
      （身份纪律），另须中间集三键：中间集属本企业、
      junction_source_column/junction_target_column ∈ 中间集列快照
      （语义指向两端主键，值域不校验——列快照存在性口径）；
      v0 物化端点不支持（resolve 层如实拒绝）。
    """
    if kind not in RELATIONSHIP_KINDS:
        raise InvalidEntity(
            f"关系 kind 必须是 {' 或 '.join(RELATIONSHIP_KINDS)}（收到 {kind!r}）"
        )
    target = db.get(Entity, to_entity_id)
    if target is None or target.customer_id != customer_id:
        raise EntityNotFound(f"目标实体 {to_entity_id} 不存在")
    if not isinstance(from_column, str) or not from_column:
        raise InvalidEntity("关系源列必须是非空字符串")
    if not isinstance(to_column, str) or not to_column:
        raise InvalidEntity("关系目标列必须是非空字符串")

    if kind == "m:n":
        # 两端列 = 各自主键列（身份纪律）；中间集三键逐项校验
        if from_column not in (source_entity.pk_columns or []):
            raise InvalidEntity(
                f"m:n 关系的源列必须是源实体「{source_entity.name}」的主键列："
                f"{', '.join(source_entity.pk_columns or [])}（收到 {from_column}）"
            )
        if to_column not in (target.pk_columns or []):
            raise InvalidEntity(
                f"m:n 关系的目标列必须是目标实体「{target.name}」的主键列："
                f"{', '.join(target.pk_columns or [])}（收到 {to_column}）"
            )
        if not isinstance(junction_dataset_id, str) or not junction_dataset_id:
            raise InvalidEntity("m:n 关系必须指定中间数据集（junction_dataset_id）")
        junction = _owned_dataset_or_none(datasets_db, junction_dataset_id, customer_id)
        if junction is None:
            raise InvalidEntity(f"中间数据集 {junction_dataset_id} 不存在")
        junction_columns = _dataset_column_names(junction)
        for name, column in (
            ("junction_source_column", junction_source_column),
            ("junction_target_column", junction_target_column),
        ):
            if not isinstance(column, str) or not column:
                raise InvalidEntity(f"m:n 关系必须指定 {name}")
            if column not in junction_columns:
                raise InvalidEntity(
                    f"{name} {column} 不在中间数据集 {junction_dataset_id} 列快照中"
                )
        _quad_conflict(
            db,
            from_entity_id=source_entity.id,
            from_column=from_column,
            to_entity_id=to_entity_id,
            to_column=to_column,
        )
        relationship = EntityRelationship(
            customer_id=customer_id,
            from_entity_id=source_entity.id,
            from_column=from_column,
            to_entity_id=to_entity_id,
            to_column=to_column,
            kind="m:n",
            declared_kind="m:n",
            label=(label.strip() or None) if isinstance(label, str) else None,
            junction_dataset_id=junction_dataset_id,
            junction_source_column=junction_source_column,
            junction_target_column=junction_target_column,
        )
        db.add(relationship)
        db.flush()
        return relationship

    if kind == "1:n":
        # 反向声明校验（镜像 n:1）：声明源列 ∈ 源主键（「一」侧身份）、
        # 声明目标列 ∈ 目标集列（「多」侧外键列）；随后归一化对调存储。
        if from_column not in (source_entity.pk_columns or []):
            raise InvalidEntity(
                f"1:n 声明的源列必须是源实体「{source_entity.name}」的主键列"
                f"（「一」侧身份纪律）：{', '.join(source_entity.pk_columns or [])}"
                f"（收到 {from_column}）"
            )
        declared_target_dataset = _owned_dataset_or_none(
            datasets_db, target.dataset_id, customer_id
        )
        if declared_target_dataset is None:
            raise InvalidEntity(
                f"目标实体绑定的数据集 {target.dataset_id} 不存在（目标列无法校验）"
            )
        if to_column not in _dataset_column_names(declared_target_dataset):
            raise InvalidEntity(
                f"1:n 声明的目标列 {to_column} 不在目标实体「{target.name}」的数据集 "
                f"{target.dataset_id} 列中（「多」侧外键列）"
            )
        # 归一化：存储为 反向 n:1（from/to 对调）
        stored_from, stored_from_column = target, to_column
        stored_to, stored_to_column = source_entity, from_column
        stored_kind = "n:1"
        declared = "1:n"
    else:
        # v0 语义零漂移：源列 ∈ 源集列、目标列 ∈ 目标主键
        dataset = _owned_dataset_or_none(
            datasets_db, source_entity.dataset_id, customer_id
        )
        if dataset is None:
            raise InvalidEntity(
                f"源实体绑定的数据集 {source_entity.dataset_id} 不存在（源列无法校验）"
            )
        if from_column not in _dataset_column_names(dataset):
            raise InvalidEntity(
                f"关系源列 {from_column} 不在源实体「{source_entity.name}」的数据集 "
                f"{source_entity.dataset_id} 列中"
            )
        if to_column not in (target.pk_columns or []):
            raise InvalidEntity(
                f"关系目标必须是目标实体的主键列：「{target.name}」的主键为 "
                f"{', '.join(target.pk_columns or [])}（收到 {to_column}）"
            )
        stored_from, stored_from_column = source_entity, from_column
        stored_to, stored_to_column = target, to_column
        stored_kind = kind
        declared = None

    _quad_conflict(
        db,
        from_entity_id=stored_from.id,
        from_column=stored_from_column,
        to_entity_id=stored_to.id,
        to_column=stored_to_column,
    )
    relationship = EntityRelationship(
        customer_id=customer_id,
        from_entity_id=stored_from.id,
        from_column=stored_from_column,
        to_entity_id=stored_to.id,
        to_column=stored_to_column,
        kind=stored_kind,
        declared_kind=declared,
        label=(label.strip() or None) if isinstance(label, str) else None,
    )
    db.add(relationship)
    db.flush()
    return relationship


def declared_view(rel: EntityRelationship) -> dict[str, Any]:
    """按**声明方向**呈现关系（1:n 归一化存储的反向翻转；图谱/遍历/UI 唯一
    同源——禁止各处自行翻转，tasks 2.1 对拍项）。含 m:n 中间集三键。"""
    if rel.declared_kind == "1:n":
        view = {
            "from_entity_id": rel.to_entity_id,
            "from_column": rel.to_column,
            "to_entity_id": rel.from_entity_id,
            "to_column": rel.from_column,
            "kind": "1:n",
            "label": rel.label,
            "id": rel.id,
        }
    else:
        view = {
            "from_entity_id": rel.from_entity_id,
            "from_column": rel.from_column,
            "to_entity_id": rel.to_entity_id,
            "to_column": rel.to_column,
            "kind": rel.kind,
            "label": rel.label,
            "id": rel.id,
        }
    view["junction_dataset_id"] = rel.junction_dataset_id
    view["junction_source_column"] = rel.junction_source_column
    view["junction_target_column"] = rel.junction_target_column
    return view


def delete_relationship(
    db: Session, *, customer_id: str, entity_id: int, rel_id: int
) -> None:
    """删关系：须属本企业且以 ``entity_id`` 实体为端点，否则一律 404。"""
    rel = db.get(EntityRelationship, rel_id)
    if (
        rel is None
        or rel.customer_id != customer_id
        or (rel.from_entity_id != entity_id and rel.to_entity_id != entity_id)
    ):
        raise EntityNotFound(f"关系 {rel_id} 不存在")
    db.delete(rel)
    db.flush()


def list_relationships(db: Session, *, customer_id: str) -> list[EntityRelationship]:
    """本企业全部关系（创建序稳定）。"""
    stmt = (
        select(EntityRelationship)
        .where(EntityRelationship.customer_id == customer_id)
        .order_by(EntityRelationship.id)
    )
    return list(db.scalars(stmt).all())


# ------------------------------------------------------------------ 关系物化解析


def resolve_relationship_materialization(
    db: Session,
    datasets_db: Session,
    *,
    customer_id: str,
    entity_id: int,
    rel_id: int,
) -> tuple[EntityRelationship, Entity, Entity, Dataset, Dataset]:
    """物化解析（design D2，wire-entity-link）：

    - 关系存在、属本企业、且以 ``entity_id`` 实体为端点（端点参数只是定位
      入口——**以关系的 from 实体为左、to 实体为右**）→ 否则 EntityNotFound；
    - 两侧实体的 dataset 行（datasets 库会话）存在且属本企业 → 否则
      EntityNotFound（失存/drift 如实 404）；
    - from_column ∈ 左集列快照、to_column ∈ 右集列快照（声明时校验过，
      此处防漂移窗口）→ 否则 InvalidEntity（指名列名）。

    档位闸（403）与配额（429）是 HTTP 语义，留在路由层。
    """
    rel = db.get(EntityRelationship, rel_id)
    if (
        rel is None
        or rel.customer_id != customer_id
        or (rel.from_entity_id != entity_id and rel.to_entity_id != entity_id)
    ):
        raise EntityNotFound(f"关系 {rel_id} 不存在")
    if rel.kind == "m:n":
        # wire-entity-v2：m:n v0 不物化（声明可、遍历可、物化不支持——如实 422）
        raise InvalidEntity(
            "m:n 关系暂不支持物化连接（v0 物化仅支持 n:1/1:1；"
            "m:n 可经对象遍历与中间数据集查询使用）"
        )
    left_entity = db.get(Entity, rel.from_entity_id)
    right_entity = db.get(Entity, rel.to_entity_id)
    if left_entity is None or right_entity is None:  # pragma: no cover - 级联删除保证
        raise EntityNotFound(f"关系 {rel_id} 不存在")
    left_dataset = _owned_dataset_or_none(
        datasets_db, left_entity.dataset_id, customer_id
    )
    if left_dataset is None:
        raise EntityNotFound(
            f"实体「{left_entity.name}」绑定的数据集 {left_entity.dataset_id} 不存在"
        )
    right_dataset = _owned_dataset_or_none(
        datasets_db, right_entity.dataset_id, customer_id
    )
    if right_dataset is None:
        raise EntityNotFound(
            f"实体「{right_entity.name}」绑定的数据集 {right_entity.dataset_id} 不存在"
        )
    if rel.from_column not in _dataset_column_names(left_dataset):
        raise InvalidEntity(
            f"关系源列 {rel.from_column} 已不在实体「{left_entity.name}」的数据集 "
            f"{left_dataset.id} 当前列中（列漂移）——请更新或删除该关系后重试"
        )
    if rel.to_column not in _dataset_column_names(right_dataset):
        raise InvalidEntity(
            f"关系目标列 {rel.to_column} 已不在实体「{right_entity.name}」的数据集 "
            f"{right_dataset.id} 当前列中（列漂移）——请更新或删除该关系后重试"
        )
    return rel, left_entity, right_entity, left_dataset, right_dataset


# ------------------------------------------------------------------ 图谱


def _dataset_rows_by_id(
    datasets_db: Session, entities: list[Entity]
) -> dict[str, Dataset]:
    """批量取数据集摘要行（datasets 库会话；缺行不报错——drift 如实呈现）。"""
    ids = {entity.dataset_id for entity in entities}
    if not ids:
        return {}
    rows = datasets_db.scalars(
        select(Dataset).where(Dataset.id.in_(ids))
    ).all()
    return {row.id: row for row in rows}


def dataset_summary(dataset: Dataset | None, dataset_id: str) -> dict[str, Any]:
    """数据集摘要（列表项与图谱节点同形；数据集行缺失时除 id 外如实置 None）。"""
    if dataset is None:
        return {
            "id": dataset_id,
            "key": None,
            "name": None,
            "row_count": None,
            "storage_tier": None,
        }
    return {
        "id": dataset.id,
        "key": dataset.key,
        "name": dataset.name,
        "row_count": dataset.row_count,
        "storage_tier": dataset.storage_tier,
    }


def entity_graph(
    db: Session, datasets_db: Session, *, customer_id: str
) -> dict[str, list[dict[str, Any]]]:
    """同源图谱：nodes（实体 + 数据集摘要 + 主键）+ edges（关系四元组）。
    无实体 → 空集（200），不 404。"""
    entities = list_entities(db, customer_id=customer_id)
    datasets = _dataset_rows_by_id(datasets_db, entities)
    nodes = [
        {
            "id": entity.id,
            "name": entity.name,
            "description": entity.description,
            "pk_columns": list(entity.pk_columns or []),
            "display_column": entity.display_column,
            "column_notes": dict(entity.column_notes or {}),
            "dataset": dataset_summary(
                datasets.get(entity.dataset_id), entity.dataset_id
            ),
        }
        for entity in entities
    ]
    edges = [
        {
            **declared_view(rel),
            # m:n 附加中间集三键（声明方向即存储方向）
            "junction_dataset_id": rel.junction_dataset_id,
            "junction_source_column": rel.junction_source_column,
            "junction_target_column": rel.junction_target_column,
        }
        for rel in list_relationships(db, customer_id=customer_id)
    ]
    return {"nodes": nodes, "edges": edges}


def entity_graph_prompt_block(
    db: Session, datasets_db: Session, customer_id: str
) -> str | None:
    """图谱 → AI 构建上下文紧凑文本块；空图谱 → None（调用方零注入零漂移）。

    有界（design D4）：≤50 实体 / ≤100 关系，超限截断并如实标注
    「完整见控制台实体页」——不撑爆上下文。
    """
    entities = list_entities(db, customer_id=customer_id)
    if not entities:
        return None
    relationships = list_relationships(db, customer_id=customer_id)
    datasets = _dataset_rows_by_id(datasets_db, entities)
    by_id = {entity.id: entity for entity in entities}

    def _entity_label(entity_id: int) -> str:
        entity = by_id.get(entity_id)
        return entity.name if entity is not None else f"#{entity_id}"

    lines: list[str] = ["【本企业实体图谱】（业务语义；列名以此为准）"]
    shown_entities = entities[:PROMPT_MAX_ENTITIES]
    for entity in shown_entities:
        dataset = datasets.get(entity.dataset_id)
        ds_label = (
            (dataset.name or dataset.key) if dataset is not None else entity.dataset_id
        )
        line = (
            f"实体「{entity.name}」= 数据集 {ds_label}"
            f"（主键: {', '.join(entity.pk_columns or [])}）"
        )
        if entity.display_column:
            line += f"，显示列: {entity.display_column}"
        notes = dict(entity.column_notes or {})
        if notes:
            noted = "、".join(f"{col}({note})" for col, note in notes.items())
            line += f"，列注记: {noted}"
        lines.append(line)
    shown_relationships = relationships[:PROMPT_MAX_RELATIONSHIPS]
    for rel in shown_relationships:
        view = declared_view(rel)
        from_name = _entity_label(view["from_entity_id"])
        to_name = _entity_label(view["to_entity_id"])
        line = (
            f"{from_name}.{view['from_column']}"
            f" →({view['kind']}) {to_name}.{view['to_column']}"
        )
        if view["kind"] == "m:n" and rel.junction_dataset_id:
            line += (
                f" 经中间数据集 {rel.junction_dataset_id}"
                f"（{rel.junction_source_column}/{rel.junction_target_column}）"
            )
        lines.append(line)
    truncated: list[str] = []
    if len(entities) > PROMPT_MAX_ENTITIES:
        truncated.append(f"实体仅列前 {PROMPT_MAX_ENTITIES}")
    if len(relationships) > PROMPT_MAX_RELATIONSHIPS:
        truncated.append(f"关系仅列前 {PROMPT_MAX_RELATIONSHIPS}")
    if truncated:
        lines.append(f"（{'、'.join(truncated)}，完整见控制台实体页）")
    return "\n".join(lines)


__all__ = [
    "EntityConflict",
    "EntityNotFound",
    "InvalidEntity",
    "MAX_COLUMN_NOTE_LENGTH",
    "MAX_COLUMN_NOTES",
    "MAX_NAME_LENGTH",
    "PROMPT_MAX_ENTITIES",
    "PROMPT_MAX_RELATIONSHIPS",
    "RELATIONSHIP_KINDS",
    "create_entity",
    "create_relationship",
    "dataset_summary",
    "declared_view",
    "delete_entity",
    "delete_relationship",
    "entity_graph",
    "entity_graph_prompt_block",
    "get_owned_entity",
    "list_entities",
    "list_relationships",
    "resolve_relationship_materialization",
    "update_entity",
]
