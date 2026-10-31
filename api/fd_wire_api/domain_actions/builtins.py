"""平台内置声明动作类型（wire-entity-write-surface 任务组 1）。

**形状**：代码合成定义（非 DB 声明行）——不可停用、不可删除、不可修改、无编辑
入口；内置名保留（客户声明同名 409）。闸 id 命名空间沿 ``domain.{type_id}``，
内置用**负数哨兵 id**（-1/-2，与自增正 id 永不冲突；``resolve_action`` 的
``int()`` 解析原样可达，闸零改动）。

**校验同源纪律**：提议 preflight 以**嵌套 savepoint 干跑**真实创建函数
（entities.service.create_entity / create_relationship），异常即 PreflightError
（非法草案不进 pending、fail-fast），savepoint 回滚零残留；确认执行时再走同一
函数复校（列快照漂移/端点失存/撞名竞态如实 failed、零半写）——两处调用同一
函数，无第二套校验实现（design D2/D3）。

**metadata 效果不进修正层**：实体/关系是客户域元数据（datasets 同族），不是
数据行修正；执行直调 entities service 创建路径（同自理动作「直调 service 不经
HTTP」纪律），结果引用回填实例 ``result_ref``（迁移 0045）。撤销声明=既有删除
通道，动作不提供回滚伪语义。

红线：写操作永不进工具面直调（agent 只 ``wire_action_propose``，owner 控制台
确认）；「永不写回客户源」不变（声明落元数据，不触数据行）；执行零计量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..ai.actions import (
    ActionDef,
    ExecutionError,
    PreflightError,
)
from ..datasets.models import Dataset
from ..entities import service as entities_service
from ..entities.models import Entity

#: 内置类型名（保留名，客户自声明同名 409）
BUILTIN_ENTITY_DECLARE = "entity-declare"
BUILTIN_RELATION_DECLARE = "entity-relation-declare"
BUILTIN_NAMES: frozenset[str] = frozenset({BUILTIN_ENTITY_DECLARE, BUILTIN_RELATION_DECLARE})

#: 闸 id 命名空间的负数哨兵（domain.-1 / domain.-2）
BUILTIN_ENTITY_DECLARE_ID = -1
BUILTIN_RELATION_DECLARE_ID = -2
BUILTIN_BY_ID: dict[int, str] = {
    BUILTIN_ENTITY_DECLARE_ID: BUILTIN_ENTITY_DECLARE,
    BUILTIN_RELATION_DECLARE_ID: BUILTIN_RELATION_DECLARE,
}


# ------------------------------------------------------------------ 伪类型对象


@dataclass
class BuiltinActionType:
    """与 ``DomainActionType`` 行同形接口的合成定义（无 DB 行）。

    只读通道消费（create_instance 取 id/customer_id；list 载荷取展示字段）；
    ``enabled`` 恒 True、编辑/启停端点对负 id 天然 404（无行可寻）。
    """

    id: int
    name: str
    risk: str
    form_schema: dict
    effect: dict
    customer_id: str
    enabled: bool = True
    entity_id: None = None
    dataset_id: None = None
    pk_columns_json: list = field(default_factory=list)


#: 实体声明表单：text 承载引用字段（v0 select 仅字面量选项——动态值域用
#: text，值域校验收敛到 preflight 干跑，design D2）
_ENTITY_FORM = {
    "fields": [
        {"name": "business_name", "label": "业务名", "input": "text", "required": True},
        {"name": "dataset_key", "label": "数据集 key", "input": "text", "required": True},
        {
            "name": "pk_columns",
            "label": "主键列（逗号分隔）",
            "input": "text",
            "required": True,
        },
        {"name": "display_column", "label": "显示列（可空）", "input": "text", "required": False},
        {
            "name": "column_notes",
            "label": "列注记（列=注记;列=注记，可空）",
            "input": "text",
            "required": False,
        },
    ]
}

_RELATION_FORM = {
    "fields": [
        {"name": "source_entity", "label": "源实体业务名", "input": "text", "required": True},
        {"name": "source_column", "label": "源实体列", "input": "text", "required": True},
        {"name": "target_entity", "label": "目标实体业务名", "input": "text", "required": True},
        {"name": "target_column", "label": "目标实体列", "input": "text", "required": True},
        {
            "name": "kind",
            "label": "基数",
            "input": "select",
            "required": True,
            "options": ["n:1", "1:1", "1:n", "m:n"],
        },
        {"name": "junction_dataset_key", "label": "中间数据集 key（仅 m:n）", "input": "text", "required": False},
        {"name": "junction_source_column", "label": "中间集源列（仅 m:n）", "input": "text", "required": False},
        {"name": "junction_target_column", "label": "中间集目标列（仅 m:n）", "input": "text", "required": False},
    ]
}


def builtin_types(customer_id: str) -> list[BuiltinActionType]:
    """本企业内置两类型（list 通道注入用）。"""
    return [
        BuiltinActionType(
            id=BUILTIN_ENTITY_DECLARE_ID,
            name=BUILTIN_ENTITY_DECLARE,
            risk="write",
            form_schema=_ENTITY_FORM,
            effect={"kind": "metadata", "op": "entity.create"},
            customer_id=customer_id,
        ),
        BuiltinActionType(
            id=BUILTIN_RELATION_DECLARE_ID,
            name=BUILTIN_RELATION_DECLARE,
            risk="write",
            form_schema=_RELATION_FORM,
            effect={"kind": "metadata", "op": "relationship.create"},
            customer_id=customer_id,
        ),
    ]


def get_builtin(type_id: int, customer_id: str) -> BuiltinActionType | None:
    for bt in builtin_types(customer_id):
        if bt.id == type_id:
            return bt
    return None


def builtin_payload(bt: BuiltinActionType) -> dict:
    """list 载荷 dict（与 DomainActionTypeOut.model_dump 同键 + ``builtin`` 标记；
    展示字段无实体挂载语义：entity_id/dataset_id/时间戳 null）。"""
    return {
        "id": bt.id,
        "entity_id": None,
        "name": bt.name,
        "risk": bt.risk,
        "form_schema": bt.form_schema,
        "effect": bt.effect,
        "enabled": True,
        "dataset_id": None,
        "pk_columns_json": [],
        "created_at": None,
        "updated_at": None,
        "builtin": True,
    }


# ------------------------------------------------------------------ 表单值解析


def _text(values: dict, name: str, *, required: bool) -> str:
    raw = values.get(name)
    if raw is None or str(raw).strip() == "":
        if required:
            raise PreflightError(f"缺少必填表单字段: {name}")
        return ""
    if isinstance(raw, (dict, list)):
        raise PreflightError(f"表单字段 {name} 必须是标量文本")
    return str(raw).strip()


def _split_csv(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _parse_notes(text: str) -> dict[str, str] | None:
    """``列=注记;列=注记`` → dict；空→None；形态不合法逐条拒。"""
    if not text:
        return None
    notes: dict[str, str] = {}
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        column, sep, note = chunk.partition("=")
        if not sep or not column.strip() or not note.strip():
            raise PreflightError(
                f"列注记格式必须为「列=注记」分号分隔（收到 {chunk!r}）"
            )
        notes[column.strip()] = note.strip()
    return notes or None


def _resolve_dataset(values: dict, name: str, sess, customer_id: str) -> Dataset:
    from .service import InvalidActionType  # noqa: PLC0415 —— 环规避

    key = _text(values, name, required=True)
    dataset = sess.datasets.scalar(
        select(Dataset).where(Dataset.key == key, Dataset.customer_id == customer_id)
    )
    if dataset is None:
        raise InvalidActionType(f"数据集 {key!r} 不存在（或不属于本企业）")
    return dataset


def _resolve_entity(values: dict, name: str, sess, customer_id: str) -> Entity:
    from .service import InvalidActionType  # noqa: PLC0415

    entity_name = _text(values, name, required=True)
    entity = sess.entities.scalar(
        select(Entity).where(
            Entity.customer_id == customer_id, Entity.name == entity_name
        )
    )
    if entity is None:
        raise InvalidActionType(f"实体 {entity_name!r} 不存在（或不属于本企业）")
    return entity


# ------------------------------------------------------------------ 创建干跑/实跑


def _create_declaration(sess, bt: BuiltinActionType, values: dict, customer_id: str) -> str:
    """跑真实创建函数（**不含 commit**）——返回结果引用 id 字符串。

    preflight 以 savepoint 包裹后回滚（干跑）；execute 直调后 commit（实跑）。
    校验器在 add/flush 前失败，干跑零残留；形态问题以 PreflightError 形态上抛
    （调用方各自映射）。
    """
    from .service import InvalidActionType  # noqa: PLC0415

    op = (bt.effect or {}).get("op")
    if op == "entity.create":
        business_name = _text(values, "business_name", required=True)
        dataset = _resolve_dataset(values, "dataset_key", sess, customer_id)
        pk_columns = _split_csv(_text(values, "pk_columns", required=True))
        if not pk_columns:
            raise InvalidActionType("主键列必须是非空逗号分隔列表（至少一列）")
        display = _text(values, "display_column", required=False) or None
        notes = _parse_notes(_text(values, "column_notes", required=False))
        entity = entities_service.create_entity(
            sess.entities,
            sess.datasets,
            customer_id=customer_id,
            name=business_name,
            dataset_id=dataset.id,
            pk_columns=pk_columns,
            display_column=display,
            column_notes=notes,
        )
        return str(entity.id)
    if op == "relationship.create":
        source = _resolve_entity(values, "source_entity", sess, customer_id)
        target = _resolve_entity(values, "target_entity", sess, customer_id)
        from_column = _text(values, "source_column", required=True)
        to_column = _text(values, "target_column", required=True)
        kind = _text(values, "kind", required=True)
        if kind not in entities_service.RELATIONSHIP_KINDS:
            raise InvalidActionType(
                f"关系 kind 必须是 {' 或 '.join(entities_service.RELATIONSHIP_KINDS)}"
                f"（收到 {kind!r}）"
            )
        junction_key = _text(values, "junction_dataset_key", required=False)
        junction_source = _text(values, "junction_source_column", required=False)
        junction_target = _text(values, "junction_target_column", required=False)
        junction_id: str | None = None
        if kind == "m:n":
            if not (junction_key and junction_source and junction_target):
                raise InvalidActionType(
                    "m:n 关系必须同时给出中间数据集 key 与两列"
                )
            junction_id = _resolve_dataset(
                values, "junction_dataset_key", sess, customer_id
            ).id
        elif junction_key or junction_source or junction_target:
            raise InvalidActionType("中间集字段仅 kind=m:n 时使用")
        relationship = entities_service.create_relationship(
            sess.entities,
            sess.datasets,
            customer_id=customer_id,
            source_entity=source,
            to_entity_id=target.id,
            from_column=from_column,
            to_column=to_column,
            kind=kind,
            junction_dataset_id=junction_id,
            junction_source_column=junction_source or None,
            junction_target_column=junction_target or None,
        )
        return str(relationship.id)
    raise InvalidActionType(f"未知 metadata op: {op!r}")


#: 声明校验异常的映射面（entities service 抛 InvalidEntity/EntityConflict/
#: EntityNotFound + 本模块 InvalidActionType——都翻译为闸语义，不外泄原类型）
_VALIDATION_ERRORS = (
    entities_service.InvalidEntity,
    entities_service.EntityConflict,
    entities_service.EntityNotFound,
)


def _dry_run(sess, bt: BuiltinActionType, values: dict, customer_id: str) -> None:
    """preflight 干跑：savepoint 内创建后回滚（非法即拒、合法零残留）。"""
    from .service import InvalidActionType  # noqa: PLC0415

    try:
        with sess.entities.begin_nested():
            _create_declaration(sess, bt, values, customer_id)
            raise _DryRunAbort()
    except _DryRunAbort:
        pass
    except (InvalidActionType, *_VALIDATION_ERRORS) as exc:
        raise PreflightError(str(exc)) from exc
    except SQLAlchemyError as exc:  # 干跑触底 DB（竞态唯一索引等）如实拒
        raise PreflightError(f"声明干跑校验失败：{exc}") from exc


class _DryRunAbort(Exception):
    """合法干跑的显式中止哨兵（让 begin_nested 上下文回滚 savepoint）。"""


# ------------------------------------------------------------------ metadata 执行器


def execute_metadata_effect(
    sess,
    *,
    bt: BuiltinActionType,
    instance,
    customer_id: str,
) -> str:
    """metadata 效果执行：复校（同一创建函数）+ 双提交（entities→ai）。

    失败路径：entities rollback、实例 failed+原因、ai commit、上抛
    ExecutionError——零半写（校验在 add 前失败；竞态 IntegrityError 亦回滚）。
    成功：result_ref=结果 id 字符串、status=executed。
    """
    from . import models as da_models  # noqa: PLC0415
    from .service import InvalidActionType  # noqa: PLC0415

    values = dict((instance.args_json or {}).get("values") or {})
    try:
        result_ref = _create_declaration(sess, bt, values, customer_id)
        sess.entities.commit()
    except (InvalidActionType, PreflightError, *_VALIDATION_ERRORS, SQLAlchemyError) as exc:
        try:
            sess.entities.rollback()
        except Exception:  # noqa: BLE001 —— 回滚尽力，不掩盖主流程失败事实
            pass
        instance.status = da_models.INSTANCE_STATUS_FAILED
        instance.error = str(exc)[:2000]
        sess.ai.commit()
        raise ExecutionError(str(exc)) from exc

    instance.status = da_models.INSTANCE_STATUS_EXECUTED
    instance.result_ref = result_ref[:128]
    instance.error = None
    sess.ai.commit()
    return result_ref


# ------------------------------------------------------------------ 闸 def 构建


def build_builtin_action_def(
    sess, customer_id: str, bt: BuiltinActionType
) -> ActionDef:
    """内置声明动作的闸 def（管线/档位/审计全复用；执行体走 metadata 通道）。"""

    def _impact(args: dict[str, Any]) -> str:
        values = dict(args.get("values") or {})
        try:
            if bt.id == BUILTIN_ENTITY_DECLARE_ID:
                return (
                    f"声明实体「{_text(values, 'business_name', required=True)}」"
                    f"＝数据集 {_text(values, 'dataset_key', required=True)}"
                    f"（主键 {values.get('pk_columns')}）——写入客户域元数据，"
                    f"不动任何数据行"
                )
            return (
                f"声明关系 {_text(values, 'source_entity', required=True)}"
                f".{_text(values, 'source_column', required=True)}"
                f" →({values.get('kind')}) "
                f"{_text(values, 'target_entity', required=True)}"
                f".{_text(values, 'target_column', required=True)}"
                + (
                    f"（中间集 {values.get('junction_dataset_key')}）"
                    if values.get("junction_dataset_key")
                    else ""
                )
                + "——写入客户域元数据，不动任何数据行"
            )
        except PreflightError as exc:
            return f"内置声明动作「{bt.name}」：参数不完整（{exc}）"

    def _preflight(ctx, gate_sess, args: dict[str, Any]) -> None:
        values = args.get("values")
        if not isinstance(values, dict):
            raise PreflightError("表单值必须是对象（{字段名: 值}）")
        _dry_run(gate_sess, bt, values, customer_id)

    def _summarize(args: dict[str, Any]) -> str:
        values = dict(args.get("values") or {})
        parts = [
            f"{k}={str(v)[:60]}"
            for k, v in values.items()
            if not k.startswith("_")
        ]
        return f"builtin={bt.name}; {'; '.join(parts)}"[:2000]

    def _execute(ctx, gate_sess, args: dict[str, Any]) -> str:
        from . import service as domain_service  # noqa: PLC0415 —— 环规避

        instance = None
        proposal_id = args.get("_proposal_id")
        if proposal_id is not None:
            instance = domain_service.instance_by_proposal(
                gate_sess.ai, customer_id=customer_id, proposal_id=int(proposal_id)
            )
        if instance is None:
            raise ExecutionError("动作实例不可恢复（提议参数丢失）——请重新提议")
        result_ref = execute_metadata_effect(
            gate_sess, bt=bt, instance=instance, customer_id=customer_id
        )
        return (
            f"内置声明动作「{bt.name}」已执行：result_ref={result_ref}"
            f"（actor=domain-action:{bt.id}:{instance.id}，落客户域元数据，"
            f"未触数据行）"
        )

    def _on_proposed(ctx, gate_sess, args: dict[str, Any], proposal) -> None:
        from . import service as domain_service  # noqa: PLC0415 —— 环规避

        instance = domain_service.create_instance(
            gate_sess.ai,
            bt,
            proposal_id=proposal.id,
            proposer_sub=ctx.sub,
            channel=str(args.get("_channel") or "console"),
            values=dict(args.get("values") or {}),
            source_ref=args.get("source_ref")
            if isinstance(args.get("source_ref"), str)
            else None,
        )
        args["_instance_id"] = instance.id

    def _on_rejected(ctx, gate_sess, args: dict[str, Any], proposal) -> None:
        from . import service as domain_service  # noqa: PLC0415 —— 环规避

        instance = domain_service.instance_by_proposal(
            gate_sess.ai, customer_id=customer_id, proposal_id=proposal.id
        )
        if (
            instance is not None
            and instance.status == domain_service.INSTANCE_STATUS_AWAITING
        ):
            instance.status = domain_service.INSTANCE_STATUS_REJECTED
            gate_sess.ai.commit()

    return ActionDef(
        id=f"domain.{bt.id}",
        risk=bt.risk,
        impact=_impact,
        preflight=_preflight,
        execute=_execute,
        summarize=_summarize,
        on_proposed=_on_proposed,
        on_rejected=_on_rejected,
    )


__all__ = [
    "BUILTIN_BY_ID",
    "BUILTIN_ENTITY_DECLARE",
    "BUILTIN_ENTITY_DECLARE_ID",
    "BUILTIN_NAMES",
    "BUILTIN_RELATION_DECLARE",
    "BUILTIN_RELATION_DECLARE_ID",
    "BuiltinActionType",
    "build_builtin_action_def",
    "builtin_payload",
    "builtin_types",
    "execute_metadata_effect",
    "get_builtin",
]
