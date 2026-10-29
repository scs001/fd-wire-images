"""客户域动作服务（wire-domain-actions 任务组 2/3）。

三层职责：

1. **类型声明**（本模块 CRUD）：挂实体声明动作类型——name 实体内唯一、
   risk ∈ {write, destructive}（v0：publish/cost 不是域动作语义，422 带合法
   清单）、表单字段 schema（复用 form-apps 字段子集：text|number|date|select，
   select 仅字面量选项）、效果 spec（v0 仅 insert/update，v0 只写实体自己的
   绑定数据集且该集须 cell 档——免费档录入需求由既有表单面覆盖）。
   ``dataset_id``/``pk_columns_json`` 为**保存时刻快照**（效果执行零实体库
   会话依赖；漂移在提议/执行处如实拒绝）。
2. **效果执行器**（闸 def.execute 的实现）：效果翻译为引擎业务写通道
   ``customer_dataset_write_batch``（strict anchors——业务写必须命中锚，
   悬空是连接器世界概念不是业务写结果）；通道归属 ``agent`` +
   ``actor="domain-action:{type_id}:{instance_id}"``（ADR-0006：业务写并入
   通道归属，永不写回客户源）。update 锚 = 实体主键值（引擎锚按主键列
   匹配——v0 单列主键，声明时校验）。
3. **闸 def 构建器**（:func:`build_domain_action_def`）：按 type_id 动态构建
   ``ActionDef``——闸注册表的第二个来源（ai.actions.resolve_action）。
   preflight = 启用态 + 表单值校验 + 列漂移；execute = 效果执行 + 实例
   台账回写（执行事实源是实例行的 ``args_json``，非进程内暂存——重启安全）。

红线：永不写回客户源（效果只落引擎覆盖层）；写操作不进任何工具面直调；
域动作提议/实例不镜像至壹座拍板面（paas 评审 R2 共同护栏）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.actions import (
    ActionDef,
    ActionNotRegistered,
    ExecutionError,
    PreflightError,
)
from ..ai.models import utc_now_naive
from ..datasets.models import Dataset
from .models import (
    CHANNEL_CONSOLE,
    DOMAIN_RISKS,
    EFFECT_KINDS,
    FIELD_INPUTS,
    INSTANCE_STATUS_AWAITING,
    INSTANCE_STATUS_DISABLED,
    INSTANCE_STATUS_EXECUTED,
    INSTANCE_STATUS_FAILED,
    INSTANCE_STATUS_REJECTED,
    DomainActionInstance,
    DomainActionType,
    DomainActionTypeEvent,
)

#: 闸动作 id 前缀（域动作在闸注册表的动态命名空间）
DOMAIN_ACTION_PREFIX = "domain."


class ActionTypeNotFound(LookupError):
    """动作类型不存在/不属本企业——路由层转 404（不泄露他企存在性）。"""


class InvalidActionType(ValueError):
    """类型声明校验失败——路由层转 422（逐条文案，零半写）。"""


class ActionTypeConflict(ValueError):
    """动作名实体内唯一冲突——路由层转 409。"""


# ------------------------------------------------------------------ 校验


def _validate_action_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise InvalidActionType("动作名必须是非空字符串")
    stripped = name.strip()
    if len(stripped) > 100:
        raise InvalidActionType("动作名超过 100 字上限")
    return stripped


def _validate_risk(risk: Any) -> str:
    if risk not in DOMAIN_RISKS:
        raise InvalidActionType(
            f"域动作风险档只接受 {' / '.join(DOMAIN_RISKS)}"
            f"（publish/cost 不是域动作语义；收到 {risk!r}）"
        )
    return risk


def _validate_form_schema(schema: Any) -> dict:
    """表单字段 schema：``{"fields": [...]}`` 信封（裸数组宽容归一）；
    name 唯一非空、input 枚举、select 须带非空字面量选项。"""
    if isinstance(schema, dict) and isinstance(schema.get("fields"), list):
        field_list = schema["fields"]
    elif isinstance(schema, list):
        field_list = schema  # 裸数组宽容归一
    else:
        raise InvalidActionType("表单字段 schema 必须是 {fields: [...]} 信封（或字段数组）")
    if not field_list:
        raise InvalidActionType("表单字段 schema 必须是非空数组")
    seen: set[str] = set()
    fields: list[dict] = []
    for field in field_list:
        if not isinstance(field, dict):
            raise InvalidActionType("表单字段必须是对象")
        name = field.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InvalidActionType("表单字段 name 必须是非空字符串")
        if name in seen:
            raise InvalidActionType(f"表单字段名重复: {name}")
        seen.add(name)
        input_kind = field.get("input")
        if input_kind not in FIELD_INPUTS:
            raise InvalidActionType(
                f"字段 {name} 的 input 必须是 {' / '.join(FIELD_INPUTS)}"
                f"（收到 {input_kind!r}）"
            )
        normalized: dict[str, Any] = {
            "name": name.strip(),
            "input": input_kind,
            "required": field.get("required") is True,
        }
        label = field.get("label")
        if isinstance(label, str) and label.strip():
            normalized["label"] = label.strip()[:100]
        if input_kind == "select":
            options = field.get("options")
            if (
                not isinstance(options, list)
                or not options
                or not all(isinstance(o, str) and o.strip() for o in options)
            ):
                raise InvalidActionType(
                    f"字段 {name} 为 select 时必须带非空字面量选项数组"
                )
            normalized["options"] = [o.strip() for o in options]
        fields.append(normalized)
    return {"fields": fields}


def _validate_effect(
    effect: Any, fields: dict | list, dataset: Dataset, pk_columns: list[str]
) -> dict:
    """效果 spec：kind 枚举；value_mapping 列 ∈ 数据集列快照、映射值恰为
    {field} 或 {literal} 之一；update 须单列主键实体（锚=主键值）且主键列
    不进 value_mapping（定位列不可改写）。"""
    if not isinstance(effect, dict):
        raise InvalidActionType("效果必须是对象")
    kind = effect.get("kind")
    if kind not in EFFECT_KINDS:
        raise InvalidActionType(
            f"效果 kind 必须是 {' / '.join(EFFECT_KINDS)}（收到 {kind!r}）"
        )
    mapping = effect.get("value_mapping")
    if not isinstance(mapping, dict) or not mapping:
        raise InvalidActionType("效果必须带非空 value_mapping（目标列 → 表单字段/字面量）")
    columns = {
        column.get("name")
        for column in (dataset.columns_json or [])
        if isinstance(column, dict)
    }
    field_list = fields.get("fields") if isinstance(fields, dict) else fields
    field_names = {field["name"] for field in field_list}
    normalized: dict[str, dict] = {}
    for column, bound in mapping.items():
        if column not in columns:
            raise InvalidActionType(
                f"效果映射列 {column} 不在数据集 {dataset.id} 列快照中"
            )
        if kind == "update" and column in pk_columns:
            raise InvalidActionType(
                f"效果映射列 {column} 是主键列——update 的定位列不可改写"
            )
        if (
            not isinstance(bound, dict)
            or sorted(bound.keys()) not in (["field"], ["literal"])
            or len(bound) != 1
        ):
            raise InvalidActionType(
                f"列 {column} 的映射必须是 {{field: 表单字段名}} 或 "
                "{literal: 字面量} 之一"
            )
        if "field" in bound:
            if bound["field"] not in field_names:
                raise InvalidActionType(
                    f"列 {column} 映射的表单字段 {bound['field']!r} 不在表单 schema 中"
                )
            normalized[column] = {"field": bound["field"]}
        else:
            literal = bound["literal"]
            if isinstance(literal, (dict, list)):
                raise InvalidActionType(f"列 {column} 的字面量必须是标量")
            normalized[column] = {"literal": literal}
    if kind == "update":
        if len(pk_columns) != 1:
            raise InvalidActionType(
                "update 效果须含实体主键列以定位目标行——"
                "v0 仅支持单列主键实体（组合键留 v1）"
            )
        if pk_columns[0] not in field_names:
            raise InvalidActionType(
                f"update 效果须含实体主键列以定位目标行：表单必须包含主键字段 "
                f"{pk_columns[0]!r}"
            )
    return {"kind": kind, "value_mapping": normalized}


# ------------------------------------------------------------------ 类型 CRUD


def _type_snapshot(type_row: DomainActionType) -> dict:
    return {
        "name": type_row.name,
        "risk": type_row.risk,
        "form_schema": type_row.form_schema,
        "effect": type_row.effect,
        "enabled": type_row.enabled,
    }


def _type_event(
    db: Session,
    type_row: DomainActionType,
    *,
    actor_sub: str,
    before: dict | None,
    after: dict | None,
) -> None:
    db.add(
        DomainActionTypeEvent(
            type_id=type_row.id,
            actor_sub=actor_sub[:64],
            before_json=before,
            after_json=after,
        )
    )


def get_owned_type(db: Session, *, type_id: int, customer_id: str) -> DomainActionType:
    type_row = db.get(DomainActionType, type_id)
    if type_row is None or type_row.customer_id != customer_id:
        raise ActionTypeNotFound(f"动作类型 {type_id} 不存在")
    return type_row


def create_type(
    db: Session,
    entities_db: Session,
    datasets_db: Session,
    *,
    customer_id: str,
    entity_id: int,
    name: str,
    risk: str,
    form_schema: Any,
    effect: Any,
    actor_sub: str,
) -> DomainActionType:
    """声明动作类型：实体属主 + 绑定集一致 + cell 档 + 全链校验后落行。"""
    from ..entities.models import Entity  # noqa: PLC0415 —— 跨切片惰性

    name = _validate_action_name(name)
    risk = _validate_risk(risk)
    entity = entities_db.get(Entity, entity_id)
    if entity is None or entity.customer_id != customer_id:
        raise ActionTypeNotFound(f"实体 {entity_id} 不存在")
    dataset = datasets_db.get(Dataset, entity.dataset_id)
    if dataset is None or dataset.customer_id != customer_id:
        raise InvalidActionType(
            f"实体绑定的数据集 {entity.dataset_id} 不存在（漂移）"
        )
    if dataset.storage_tier != "cell":
        raise InvalidActionType(
            "域动作 v0 仅支持 cell 档数据集（免费档录入需求走表单面）"
        )
    pk_columns = [c for c in (entity.pk_columns or [])]
    fields = _validate_form_schema(form_schema)
    normalized_effect = _validate_effect(effect, fields, dataset, pk_columns)
    dup = db.scalar(
        select(DomainActionType).where(
            DomainActionType.customer_id == customer_id,
            DomainActionType.entity_id == entity_id,
            DomainActionType.name == name,
        )
    )
    if dup is not None:
        raise ActionTypeConflict(
            f"动作名「{name}」在该实体上已存在（冲突类型 id={dup.id}）"
        )
    type_row = DomainActionType(
        customer_id=customer_id,
        entity_id=entity_id,
        name=name,
        risk=risk,
        form_schema=fields,
        effect=normalized_effect,
        enabled=True,
        dataset_id=entity.dataset_id,
        pk_columns_json=pk_columns,
        created_by=actor_sub[:64],
    )
    db.add(type_row)
    db.flush()
    _type_event(db, type_row, actor_sub=actor_sub, before=None, after=_type_snapshot(type_row))
    db.flush()
    return type_row


def update_type(
    db: Session,
    datasets_db: Session,
    type_row: DomainActionType,
    *,
    name: str,
    risk: str,
    form_schema: Any,
    effect: Any,
    actor_sub: str,
) -> DomainActionType:
    """整量更新（重校验；快照不随实体漂移追改——重校验按当前快照列）。"""
    name = _validate_action_name(name)
    risk = _validate_risk(risk)
    dataset = datasets_db.get(Dataset, type_row.dataset_id)
    if dataset is None:
        raise InvalidActionType(f"绑定数据集 {type_row.dataset_id} 不存在（漂移）")
    fields = _validate_form_schema(form_schema)
    normalized_effect = _validate_effect(
        effect, fields, dataset, list(type_row.pk_columns_json or [])
    )
    dup = db.scalar(
        select(DomainActionType).where(
            DomainActionType.customer_id == type_row.customer_id,
            DomainActionType.entity_id == type_row.entity_id,
            DomainActionType.name == name,
            DomainActionType.id != type_row.id,
        )
    )
    if dup is not None:
        raise ActionTypeConflict(
            f"动作名「{name}」在该实体上已存在（冲突类型 id={dup.id}）"
        )
    before = _type_snapshot(type_row)
    type_row.name = name
    type_row.risk = risk
    type_row.form_schema = fields
    type_row.effect = normalized_effect
    _type_event(
        db, type_row, actor_sub=actor_sub, before=before, after=_type_snapshot(type_row)
    )
    db.flush()
    return type_row


def set_enabled(
    db: Session, type_row: DomainActionType, *, enabled: bool, actor_sub: str
) -> DomainActionType:
    """启停（停用 = 未注册语义——闸按「动作未注册」拒绝；pending 实例在
    确认时如实失败，见执行器启用态复检）。"""
    before = _type_snapshot(type_row)
    type_row.enabled = enabled
    if not enabled:
        # 停用即冻结在途实例（backstop：执行器还有启用态复检双保险）
        pending = (
            db.execute(
                select(DomainActionInstance).where(
                    DomainActionInstance.type_id == type_row.id,
                    DomainActionInstance.status == INSTANCE_STATUS_AWAITING,
                )
            )
            .scalars()
            .all()
        )
        for instance in pending:
            instance.status = INSTANCE_STATUS_DISABLED
    _type_event(
        db, type_row, actor_sub=actor_sub, before=before, after=_type_snapshot(type_row)
    )
    db.flush()
    return type_row


def disable_for_entity(db: Session, *, customer_id: str, entity_id: int) -> int:
    """实体删除级联：该实体全部类型置停用（历史实例保留可查）。

    由 entities.delete_entity 调用（同事务；返回停用数留痕）。"""
    rows = (
        db.execute(
            select(DomainActionType).where(
                DomainActionType.customer_id == customer_id,
                DomainActionType.entity_id == entity_id,
                DomainActionType.enabled.is_(True),
            )
        )
        .scalars()
        .all()
    )
    for type_row in rows:
        set_enabled(db, type_row, enabled=False, actor_sub="migration:entity-deleted")
    return len(rows)


def list_types_for_entity(
    db: Session, *, customer_id: str, entity_id: int
) -> list[DomainActionType]:
    return list(
        db.scalars(
            select(DomainActionType)
            .where(
                DomainActionType.customer_id == customer_id,
                DomainActionType.entity_id == entity_id,
            )
            .order_by(DomainActionType.id)
        ).all()
    )


def list_enabled_for_customer(db: Session, *, customer_id: str) -> list[DomainActionType]:
    return list(
        db.scalars(
            select(DomainActionType)
            .where(
                DomainActionType.customer_id == customer_id,
                DomainActionType.enabled.is_(True),
            )
            .order_by(DomainActionType.id)
        ).all()
    )


# ------------------------------------------------------------------ 实例台账


def create_instance(
    db: Session,
    type_row: DomainActionType,
    *,
    proposal_id: int,
    proposer_sub: str,
    channel: str,
    values: dict,
    source_ref: str | None,
) -> DomainActionInstance:
    instance = DomainActionInstance(
        type_id=type_row.id,
        customer_id=type_row.customer_id,
        proposal_id=proposal_id,
        proposer_sub=proposer_sub[:64],
        channel=channel,
        source_ref=(source_ref or None),
        args_json={"values": values},
        status=INSTANCE_STATUS_AWAITING,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def instance_by_proposal(
    db: Session, *, customer_id: str, proposal_id: int
) -> DomainActionInstance | None:
    return db.scalar(
        select(DomainActionInstance).where(
            DomainActionInstance.customer_id == customer_id,
            DomainActionInstance.proposal_id == proposal_id,
        )
    )


def list_instances(
    db: Session,
    *,
    customer_id: str,
    type_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[DomainActionInstance]:
    stmt = (
        select(DomainActionInstance)
        .where(DomainActionInstance.customer_id == customer_id)
        .order_by(DomainActionInstance.id.desc())
        .limit(limit)
    )
    if type_id is not None:
        stmt = stmt.where(DomainActionInstance.type_id == type_id)
    if status is not None:
        stmt = stmt.where(DomainActionInstance.status == status)
    return list(db.scalars(stmt).all())


# ------------------------------------------------------------------ 效果执行


def _resolve_mapped_values(
    type_row: DomainActionType,
    dataset: Dataset,
    values: dict,
) -> dict[str, Any]:
    """效果映射 → 实际写入行（field 取表单值；literal 原样）。"""
    mapping = (type_row.effect or {}).get("value_mapping") or {}
    resolved: dict[str, Any] = {}
    for column, bound in mapping.items():
        if "field" in bound:
            if bound["field"] not in values:
                raise ExecutionError(
                    f"表单值缺失字段 {bound['field']!r}（列 {column}）"
                )
            resolved[column] = values[bound["field"]]
        else:
            resolved[column] = bound["literal"]
    # 列漂移如实拒绝（快照后数据集列可能已变）
    columns = {
        column.get("name")
        for column in (dataset.columns_json or [])
        if isinstance(column, dict)
    }
    unknown = [column for column in resolved if column not in columns]
    if unknown:
        raise ExecutionError(
            f"效果映射列已不在数据集列快照中: {', '.join(unknown)}（列漂移）"
        )
    return resolved


def execute_effect(
    sess,
    *,
    type_row: DomainActionType,
    instance: DomainActionInstance,
    customer_id: str,
) -> int:
    """效果执行（闸 def.execute 的实现体）：效果 → 引擎业务写通道
    （strict anchors）；通道归属 agent + domain-action:{type}:{instance}；
    成败都回写实例台账（执行事实源 = 实例 args_json，重启安全）。

    返回 correction_id（write_batch 单 op 语义下即该笔修正 id）。
    """
    from ..datasets.ingest import TOOL_WRITE_BATCH, get_engine_client_factory  # noqa: PLC0415

    if not type_row.enabled:
        instance.status = INSTANCE_STATUS_DISABLED
        instance.error = "动作类型已停用"
        sess.ai.commit()
        raise ExecutionError("动作类型已停用（类型停用后的 pending 确认如实拒绝）")
    dataset = sess.datasets.get(Dataset, type_row.dataset_id)
    if dataset is None or dataset.customer_id != customer_id:
        instance.status = INSTANCE_STATUS_FAILED
        instance.error = f"绑定数据集 {type_row.dataset_id} 不存在（漂移）"
        sess.ai.commit()
        raise ExecutionError(instance.error)
    if dataset.storage_tier != "cell":
        instance.status = INSTANCE_STATUS_FAILED
        instance.error = "域动作 v0 仅支持 cell 档数据集"
        sess.ai.commit()
        raise ExecutionError(instance.error)

    values = dict((instance.args_json or {}).get("values") or {})
    resolved = _resolve_mapped_values(type_row, dataset, values)
    kind = (type_row.effect or {}).get("kind")
    op: dict[str, Any]
    if kind == "update":
        pk_columns = list(type_row.pk_columns_json or [])
        anchor = values.get(pk_columns[0]) if pk_columns else None
        if anchor is None or anchor == "":
            instance.status = INSTANCE_STATUS_FAILED
            instance.error = f"update 缺主键定位值（主键列 {pk_columns[0] if pk_columns else ''}）"
            sess.ai.commit()
            raise ExecutionError(instance.error)
        op = {"op": "update", "anchor": str(anchor), "values": resolved}
    else:
        op = {"op": "insert", "values": resolved}

    actor = f"domain-action:{type_row.id}:{instance.id}"
    try:
        result = get_engine_client_factory()(customer_id).call(
            TOOL_WRITE_BATCH,
            {
                "dataset_key": dataset.key,
                "ops": [op],
                "actor": actor,
                "channel": "agent",
                "actor_id": customer_id,
                "actor_label": actor,
            },
        )
    except Exception as exc:  # noqa: BLE001 —— 失败落台账再上抛（闸转 failed）
        instance.status = INSTANCE_STATUS_FAILED
        instance.error = str(exc)[:2000]
        sess.ai.commit()
        raise ExecutionError(str(exc)) from exc

    correction_id = result.get("correction_id")
    if correction_id is None and isinstance(result.get("results"), list) and result["results"]:
        correction_id = result["results"][0].get("correction_id")
    instance.status = INSTANCE_STATUS_EXECUTED
    instance.correction_id = int(correction_id) if correction_id is not None else None
    sess.ai.commit()
    return instance.correction_id or 0


# ------------------------------------------------------------------ 闸 def 构建


def build_domain_action_def(sess, customer_id: str, type_id: int) -> ActionDef:
    """按类型行构建闸 ActionDef（闸注册表的动态来源；未注册/停用/他企一律
    ActionNotRegistered——闸「未注册动作被拒绝」语义，不泄露他企存在性）。"""
    type_row = sess.ai.get(DomainActionType, type_id)
    if (
        type_row is None
        or type_row.customer_id != customer_id
        or not type_row.enabled
    ):
        raise ActionNotRegistered(f"动作 domain.{type_id} 未注册")

    def _impact(args: dict[str, Any]) -> str:
        values = dict(args.get("values") or {})
        shown = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(values.items())[:6])
        return (
            f"执行域动作「{type_row.name}」："
            f"{(type_row.effect or {}).get('kind')} 到数据集 {type_row.dataset_id}"
            f"（{shown}）"
        )

    def _preflight(ctx, gate_sess, args: dict[str, Any]) -> None:
        values = args.get("values")
        if not isinstance(values, dict):
            raise PreflightError("表单值必须是对象（{字段名: 值}）")
        fields = (type_row.form_schema or {}).get("fields") or []
        for field in fields:
            if field.get("required") and (
                field["name"] not in values or values[field["name"]] in (None, "")
            ):
                raise PreflightError(f"缺少必填表单字段: {field['name']}")
        dataset = gate_sess.datasets.get(Dataset, type_row.dataset_id)
        if dataset is None or dataset.customer_id != customer_id:
            raise PreflightError(
                f"绑定数据集 {type_row.dataset_id} 不存在（漂移）——请更新动作类型"
            )
        _resolve_mapped_values(type_row, dataset, values)  # 列漂移如实拒绝

    def _summarize(args: dict[str, Any]) -> str:
        values = dict(args.get("values") or {})
        parts = [
            f"{k}={str(v)[:60]}"
            for k, v in values.items()
            if not k.startswith("_")
        ]
        return f"type={type_row.id}; {'; '.join(parts)}"[:2000]

    def _execute(ctx, gate_sess, args: dict[str, Any]) -> str:
        instance = None
        proposal_id = args.get("_proposal_id")
        if proposal_id is not None:
            instance = instance_by_proposal(
                gate_sess.ai, customer_id=customer_id, proposal_id=int(proposal_id)
            )
        if instance is None:
            # 进程内 args 丢失（重启）的恢复路：类型快照 + 闸提议摘要不可用
            # → 如实失败（fail-closed，重新提议即可）
            raise ExecutionError(
                "动作实例不可恢复（提议参数丢失）——请重新提议"
            )
        correction_id = execute_effect(
            gate_sess, type_row=type_row, instance=instance, customer_id=customer_id
        )
        return (
            f"域动作「{type_row.name}」已执行：correction_id={correction_id}"
            f"（actor=domain-action:{type_row.id}:{instance.id}）"
        )

    def _on_proposed(ctx, gate_sess, args: dict[str, Any], proposal) -> None:
        instance = create_instance(
            gate_sess.ai,
            type_row,
            proposal_id=proposal.id,
            proposer_sub=ctx.sub,
            channel=str(args.get("_channel") or CHANNEL_CONSOLE),
            values=dict(args.get("values") or {}),
            source_ref=args.get("source_ref") if isinstance(args.get("source_ref"), str) else None,
        )
        # 实例 id 注入 args（随闸的进程内暂存进执行体；重启恢复走 _proposal_id）
        args["_instance_id"] = instance.id

    def _on_rejected(ctx, gate_sess, args: dict[str, Any], proposal) -> None:
        instance = instance_by_proposal(
            gate_sess.ai, customer_id=customer_id, proposal_id=proposal.id
        )
        if instance is not None and instance.status == INSTANCE_STATUS_AWAITING:
            instance.status = INSTANCE_STATUS_REJECTED
            gate_sess.ai.commit()

    return ActionDef(
        id=f"{DOMAIN_ACTION_PREFIX}{type_row.id}",
        risk=type_row.risk,
        impact=_impact,
        preflight=_preflight,
        execute=_execute,
        summarize=_summarize,
        on_proposed=_on_proposed,
        on_rejected=_on_rejected,
    )


__all__ = [
    "DOMAIN_ACTION_PREFIX",
    "ActionTypeConflict",
    "ActionTypeNotFound",
    "InvalidActionType",
    "build_domain_action_def",
    "create_instance",
    "create_type",
    "disable_for_entity",
    "execute_effect",
    "get_owned_type",
    "instance_by_proposal",
    "list_enabled_for_customer",
    "list_instances",
    "list_types_for_entity",
    "set_enabled",
    "update_type",
]
