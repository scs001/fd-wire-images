"""产物生命周期业务逻辑：版本化保存、状态机迁移、会话草稿导入。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.models import AIDraftArtifact, utc_now_naive
from ..ai.service import ConversationNotFound, get_customer_conversation
from ..datasets.models import Dataset
from .doc_spec import BLANK_DOC_INNER, validate_doc_spec, validate_doc_text
from .evidence import (
    GATE_ENFORCE,
    GATE_OFF,
    InfoStore,
    get_evidence_gate_settings,
    info_store_from_env,
    validate_evidence,
    verify_evidence,
)
from .form_spec import validate_form_spec
from .models import Artifact, ArtifactDraft, ArtifactStatus, ArtifactVersion
from .record_view_spec import validate_record_view_spec

logger = logging.getLogger(__name__)


class ArtifactNotFound(LookupError):
    """产物不存在（或不属于该客户）——路由层转 404（不泄露他企存在性）。"""


class InvalidArtifactSpec(ValueError):
    """spec 未过最小校验（沿用 ai.tools.validate_artifact_args 同口径）——路由层转 422。"""


class ArtifactLocked(Exception):
    """archived 产物拒改——路由层转 409（提示先恢复）。"""


class InvalidStatusTransition(Exception):
    """非法状态迁移——路由层转 409（说明合法路径）。"""


def utc_now_iso() -> str:
    """状态留痕时间戳：UTC ISO-8601（与 demands.status_history 同口径）。"""
    return datetime.now(timezone.utc).isoformat()


def validate_spec(spec: object) -> str | None:
    """spec 最小校验（沿用 ai.tools.validate_artifact_args 的既有口径）：
    非空 title 字符串 + 非空 spec 对象；返回错误文案（None=合法）。"""
    if not isinstance(spec, dict):
        return "spec 必须是 JSON 对象"
    title = spec.get("title")
    if not isinstance(title, str) or not title.strip():
        return "spec 缺少非空 title"
    inner = spec.get("spec")
    if not isinstance(inner, dict) or not inner:
        return "spec.spec 必须是非空 JSON 对象"
    return None


#: form 产物 kind 短名（spec 校验按 kind 分派追加 form 结构层的依据）
FORM_KIND = "form"

#: alert_rule 产物 kind 短名（保存时追加规则契约层的依据）
ALERT_KIND = "alert_rule"

#: record-view 产物 kind 短名（wire-record-view design D1：同 form 走 service
#: 层常量路线——models.ARTIFACT_KINDS 校验面与 AI 门均不收录）
RECORD_VIEW_KIND = "record-view"

#: doc 产物 kind 短名（wire-rich-text-docs design D1：同 form/record-view 走
#: service 层常量路线——正文型产物不是 AI 五类工具产物，models.ARTIFACT_KINDS
#: 校验面与 AI 门均不收录；结构校验在那层之上按 kind 分派追加 doc_spec）
DOC_KIND = "doc"


class InvalidAlertArtifactSpec(InvalidArtifactSpec):
    """alert_rule 规则契约未过校验（alerts.spec_validate）——路由层转 400
    （告警规则是机读调试面，契约文案如实定位字段；与 form 的 422 面分置）。"""


def validate_kind_spec(kind: str, spec: object) -> str | None:
    """按 kind 分派 spec 校验（wire-form-builder design D1）：

    通用层先行（validate_spec：title 非空 + spec.spec 非空——既有口径
    零漂移）；kind=form 再追加 form 结构层（form_spec.validate_form_spec，
    design D1 第①层）；kind=record-view 追加 record-view 结构层
    （record_view_spec.validate_record_view_spec，wire-record-view 任务
    1.1）；kind=doc 追加正文契约层（doc_spec.validate_doc_spec——
    format 恒 markdown + text 字符串 + 体积上限，wire-rich-text-docs
    任务 1.2；草稿层 PUT 复用同一校验器，两路口径一致）。
    dashboard 及其余 kind 语义不变（仅通用层）。
    """
    error = validate_spec(spec)
    if error is not None:
        return error
    if kind == FORM_KIND and isinstance(spec, dict):
        return validate_form_spec(spec.get("spec"))
    if kind == RECORD_VIEW_KIND and isinstance(spec, dict):
        return validate_record_view_spec(spec.get("spec"))
    if kind == DOC_KIND and isinstance(spec, dict):
        return validate_doc_spec(spec.get("spec"))
    # 证据块是信封级可选扩展：结构校验在这里，存在性/逐字核对在发布层。
    return validate_evidence(spec)


def _validate_alert_spec(spec: object) -> None:
    """alert_rule 保存路径的规则契约层（wire-alert-evaluation）。

    对信封内层 ``spec.spec`` 跑 ``alerts.spec_validate.validate_alert_spec``
    （query 过 parse_panel_query / 聚合与条件枚举 / 区间 / webhook 前缀）；
    不过抛 :class:`InvalidAlertArtifactSpec`（路由层 400 detail=文案）。
    只在**新版本保存**时裁决——存量不合形 spec 不回溯（评估器对存量记
    error 事件受冷却约束，绝不崩溃）。
    """
    if not isinstance(spec, dict):  # pragma: no cover - 前置校验已保证
        return
    from ..alerts.spec_validate import validate_alert_spec

    error = validate_alert_spec(spec.get("spec"))
    if error is not None:
        raise InvalidAlertArtifactSpec(error)


def _require_owned_dataset(
    datasets_db: Session | None,
    dataset_id: str,
    *,
    customer_id: str,
    where: str | None = None,
) -> None:
    """单条数据集 id 的属主校验：本企业现存，否则 422（零半写）。

    ``where`` 给定时附在错误文案后定位新键引用位（如
    ``fields[2].optionsFromDataset.datasetId``——wire-form-entry-advanced）；
    主 datasetId 沿用既有文案（不带定位，零漂移）。
    """
    dataset = (
        datasets_db.get(Dataset, dataset_id) if datasets_db is not None else None
    )
    if dataset is None or dataset.customer_id != customer_id:
        suffix = f"（{where}）" if where else ""
        raise InvalidArtifactSpec(f"数据集 {dataset_id} 不存在{suffix}")


def _require_form_dataset(
    spec: object, *, datasets_db: Session | None, customer_id: str
) -> None:
    """form spec 的 datasetId 属主校验（design D1 第②层，零引擎调用）。

    datasetId 为 null（构建中间态——可保存不可提交）直接放行；非空时必须
    在 datasets 库会话中命中**本企业**现存数据集，不存在或他企一律
    InvalidArtifactSpec（路由层 422，不泄露他企存在性）。

    新键引用数据集同口径校验（wire-form-entry-advanced 任务 1.3）：
    ``fields[].optionsFromDataset.datasetId`` 与 ``constraints[].probe.
    datasetId``——本企业现存否则 422 定位引用位，零半写。fillFrom 的
    引用源数据集即其指向 select 自身的 optionsFromDataset.datasetId
    （保存校验保证引用成立），已被前一项覆盖，不重复查库。

    ``datasets_db`` 必须是 datasets 库会话（生产 wire_platform/wire_datasets
    分库——查 datasets 表不能走 ai 库会话，照 query_contract 先例由路由
    注入）；缺席时 fail-closed 拒存（不允许跳过属主校验的写路径）。
    列名是否仍在数据集中不在此裁决（design D1 第③层：提交时如实 422）。
    """
    if not isinstance(spec, dict):  # pragma: no cover - 前置校验已保证
        return
    inner = spec.get("spec")
    dataset_id = inner.get("datasetId") if isinstance(inner, dict) else None
    if dataset_id is not None:
        _require_owned_dataset(datasets_db, dataset_id, customer_id=customer_id)
    if not isinstance(inner, dict):
        return
    fields = inner.get("fields")
    for index, field in enumerate(fields if isinstance(fields, list) else []):
        if not isinstance(field, dict):
            continue
        source = field.get("optionsFromDataset")
        if isinstance(source, dict) and isinstance(source.get("datasetId"), str):
            _require_owned_dataset(
                datasets_db,
                source["datasetId"],
                customer_id=customer_id,
                where=f"fields[{index}].optionsFromDataset.datasetId",
            )
    constraints = inner.get("constraints")
    for index, constraint in enumerate(
        constraints if isinstance(constraints, list) else []
    ):
        if not isinstance(constraint, dict):
            continue
        probe = constraint.get("probe")
        if isinstance(probe, dict) and isinstance(probe.get("datasetId"), str):
            _require_owned_dataset(
                datasets_db,
                probe["datasetId"],
                customer_id=customer_id,
                where=f"constraints[{index}].probe.datasetId",
            )


def operator_label(customer_id: str) -> str:
    """版本/留痕的操作者标注（控制台会话写入口径）。"""
    return f"console:{customer_id}"


def _dataset_column_names(dataset: Dataset | None) -> set[str]:
    """数据集列快照列名集合（columns_json [{name, type}]——漂移基线口径，
    与 entity-layer 主键列校验同源）。"""
    if dataset is None:
        return set()
    return {
        column.get("name")
        for column in (dataset.columns_json or [])
        if isinstance(column, dict)
    }


def _require_column_in_snapshot(
    columns: set[str], column: object, *, where: str, dataset_id: str
) -> None:
    """列 ∈ 数据集列快照校验（wire-record-view 保存时刻口径）：keyColumn、
    headerFields.column、sections.dateColumn 与 filters 的 ``{fromField}``
    都按此裁决（422 定位字段；同 entity-layer 主键列口径）。"""
    if column not in columns:
        raise InvalidArtifactSpec(
            f"{where} 不在数据集 {dataset_id} 列快照内: {column!r}"
        )


def _require_record_view_datasets(
    spec: object, *, datasets_db: Session | None, customer_id: str
) -> None:
    """record-view spec 的属主与列快照校验（wire-record-view 任务 1.2，
    design「保存时刻列快照」——零引擎调用）。

    datasetId 为 null（构建中间态——可保存不可分享）直接放行（结构校验已
    保证该态下 keyColumn/headerFields/sections 全空）；非空时：

    - 主 datasetId：本企业现存（``_require_owned_dataset`` 同 form 主键
      口径，文案零漂移）；
    - ``keyColumn`` ∈ 主集列快照；
    - ``headerFields[].column`` ∈ 主集列快照（去重已在结构层拦）；
    - 每个 ``sections[].datasetId``：本企业现存（422 定位引用位，可与主集
      相同——同集复用已取行不重复查库）；
    - 每个 ``sections[].dateColumn`` ∈ 该 section 数据集列快照；
    - 每个 ``sections[].filters[].value.fromField`` ∈ 主集列快照（运行时
      以主记录行该列值代入）。

    section 的**过滤列**（filters.column）不在此裁决（design 定案：section
    数据集列漂移的真相在取数时以单段 error 如实暴露，不静默空段）。
    ``datasets_db`` 缺席时 fail-closed 拒存（同 ``_require_form_dataset``）。
    """
    if not isinstance(spec, dict):  # pragma: no cover - 前置校验已保证
        return
    inner = spec.get("spec")
    if not isinstance(inner, dict):
        return
    dataset_id = inner.get("datasetId")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        return  # 未绑数据集中间态：结构校验已保证其余键全空
    _require_owned_dataset(datasets_db, dataset_id, customer_id=customer_id)
    dataset = (
        datasets_db.get(Dataset, dataset_id) if datasets_db is not None else None
    )
    main_columns = _dataset_column_names(dataset)
    _require_column_in_snapshot(
        main_columns,
        inner.get("keyColumn"),
        where="spec.spec.keyColumn",
        dataset_id=dataset_id,
    )
    for index, field in enumerate(
        inner.get("headerFields") if isinstance(inner.get("headerFields"), list) else []
    ):
        if isinstance(field, dict):
            _require_column_in_snapshot(
                main_columns,
                field.get("column"),
                where=f"headerFields[{index}].column",
                dataset_id=dataset_id,
            )
    # section 数据集列快照缓存（同集 section 复用——每数据集至多查一次）
    section_columns: dict[str, set[str]] = {dataset_id: main_columns}
    sections = inner.get("sections")
    for index, section in enumerate(sections if isinstance(sections, list) else []):
        if not isinstance(section, dict):
            continue
        where = f"sections[{index}]"
        sec_dataset_id = section.get("datasetId")
        if isinstance(sec_dataset_id, str) and sec_dataset_id.strip():
            if sec_dataset_id not in section_columns:
                _require_owned_dataset(
                    datasets_db, sec_dataset_id, customer_id=customer_id, where=f"{where}.datasetId"
                )
                sec_dataset = (
                    datasets_db.get(Dataset, sec_dataset_id)
                    if datasets_db is not None
                    else None
                )
                section_columns[sec_dataset_id] = _dataset_column_names(sec_dataset)
            _require_column_in_snapshot(
                section_columns[sec_dataset_id],
                section.get("dateColumn"),
                where=f"{where}.dateColumn",
                dataset_id=sec_dataset_id,
            )
        filters = section.get("filters")
        for f_index, flt in enumerate(filters if isinstance(filters, list) else []):
            if not isinstance(flt, dict):
                continue
            value = flt.get("value")
            if isinstance(value, dict) and isinstance(value.get("fromField"), str):
                _require_column_in_snapshot(
                    main_columns,
                    value["fromField"],
                    where=f"{where}.filters[{f_index}].value.fromField",
                    dataset_id=dataset_id,
                )


def get_customer_artifact(db: Session, *, artifact_id: int, customer_id: str) -> Artifact:
    """客户侧产物：不存在或非本企业一律 404。"""
    artifact = db.get(Artifact, artifact_id)
    if artifact is None or artifact.customer_id != customer_id:
        raise ArtifactNotFound(f"artifact {artifact_id} 不存在")
    return artifact


def list_artifacts(
    db: Session,
    *,
    customer_id: str,
    kind: str | None = None,
    status: str | None = None,
) -> list[Artifact]:
    """本企业产物列表（按更新时间倒序；kind/status 可选过滤）。"""
    stmt = select(Artifact).where(Artifact.customer_id == customer_id)
    if kind is not None:
        stmt = stmt.where(Artifact.kind == kind)
    if status is not None:
        stmt = stmt.where(Artifact.status == status)
    stmt = stmt.order_by(Artifact.updated_at.desc(), Artifact.id.desc())
    return list(db.scalars(stmt).all())


def current_spec(db: Session, artifact: Artifact) -> dict:
    """当前 spec：版本表中 version=产物计数的最新行。"""
    row = db.scalar(
        select(ArtifactVersion).where(
            ArtifactVersion.artifact_id == artifact.id,
            ArtifactVersion.version == artifact.version,
        )
    )
    return dict(row.spec) if row is not None else {}


def list_versions(db: Session, artifact: Artifact) -> list[ArtifactVersion]:
    """版本历史（新版本在前）。"""
    stmt = (
        select(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == artifact.id)
        .order_by(ArtifactVersion.version.desc())
    )
    return list(db.scalars(stmt).all())


def get_version(db: Session, artifact: Artifact, version: int) -> ArtifactVersion:
    """取指定版本（append-only：任何版本只读）。"""
    row = db.scalar(
        select(ArtifactVersion).where(
            ArtifactVersion.artifact_id == artifact.id,
            ArtifactVersion.version == version,
        )
    )
    if row is None:
        raise ArtifactNotFound(f"artifact {artifact.id} 无版本 {version}")
    return row


def _require_kind_layers(
    kind: str,
    spec: object,
    *,
    datasets_db: Session | None,
    customer_id: str,
) -> None:
    """kind 专属校验层（spec 更新与带内容创建共用的单一真源）。

    在版本行落库前执行——任何拒绝零半写。form/record-view 的 datasetId
    属主层需要 datasets 库会话；alert_rule 规则契约层失败抛
    InvalidAlertArtifactSpec（路由层 400，须先于父类捕获）。
    """
    if kind == FORM_KIND:
        _require_form_dataset(spec, datasets_db=datasets_db, customer_id=customer_id)
    if kind == RECORD_VIEW_KIND:
        _require_record_view_datasets(
            spec, datasets_db=datasets_db, customer_id=customer_id
        )
    if kind == ALERT_KIND:
        _validate_alert_spec(spec)


def update_spec(
    db: Session,
    artifact: Artifact,
    *,
    spec: object,
    customer_id: str,
    datasets_db: Session | None = None,
    source_ref: str | None = None,
) -> Artifact:
    """spec 更新：校验（kind 分派）→ 追加新版本行（append-only）并递增计数。

    archived 拒改（先恢复）；title 随 spec.title 投影同步。

    form 产物在通用校验之上追加两层（wire-form-builder design D1，全部在
    版本行落库前——任何拒绝**零半写**）：结构层（validate_form_spec）+
    datasetId 属主层（``datasets_db``=datasets 库会话，datasetId 非空时验
    存在且属本企业，见 _require_form_dataset；wire-form-entry-advanced
    起 optionsFromDataset/probe 引用数据集同口径校验）。record-view 产物
    同构两层：结构层（record_view_spec）+ 属主与保存时刻列快照层
    （_require_record_view_datasets——keyColumn/headerFields.column/
    sections.dateColumn/filters 的 {fromField} 按 columns_json 校验，
    wire-record-view 任务 1.2）。alert_rule 产物
    追加规则契约层（_validate_alert_spec，InvalidAlertArtifactSpec→路由
    层 400）。doc 产物只走 kind 分派里的正文契约层（doc_spec，无数据集
    依赖——不触 ``datasets_db``；wire-rich-text-docs 任务 1.2）。dashboard
    及其余 kind 仅走通用层（语义零漂移，``datasets_db``
    不参与）。
    """
    if artifact.status == ArtifactStatus.archived.value:
        raise ArtifactLocked(
            f"产物 {artifact.id} 已归档（archived），不可编辑——请先恢复（restore）为草稿再修改。"
        )
    error = validate_kind_spec(artifact.kind, spec)
    if error is not None:
        raise InvalidArtifactSpec(error)
    _require_kind_layers(
        artifact.kind,
        spec,
        datasets_db=datasets_db,
        customer_id=artifact.customer_id,
    )
    spec = dict(spec)  # type: ignore[arg-type]
    artifact.version += 1
    db.add(
        ArtifactVersion(
            artifact_id=artifact.id,
            version=artifact.version,
            spec=spec,
            operator=operator_label(customer_id),
            source_ref=source_ref,
        )
    )
    artifact.title = str(spec["title"]).strip()[:200]
    artifact.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    db.refresh(artifact)
    return artifact


# ------------------------------------------------------------------ 状态机

#: 合法迁移表：当前态 → 允许动作（draft→published→archived 单向；
#: archived→draft 恢复是唯一回退边）。
_TRANSITIONS: dict[str, dict[str, ArtifactStatus]] = {
    "draft": {"publish": ArtifactStatus.published, "archive": ArtifactStatus.archived},
    "published": {"archive": ArtifactStatus.archived},
    "archived": {"restore": ArtifactStatus.draft},
}

#: 动作说明（非法迁移 409 时说明合法路径用）。
_ACTION_HINTS: dict[str, str] = {
    "publish": "合法路径：draft --publish--> published（已发布或已归档的产物请先恢复为草稿）",
    "archive": "合法路径：draft/published --archive--> archived",
    "restore": "合法路径：archived --restore--> draft（仅归档产物可恢复）",
}


def _transition(
    db: Session, artifact: Artifact, *, action: str, customer_id: str
) -> Artifact:
    target = _TRANSITIONS.get(artifact.status, {}).get(action)
    if target is None:
        raise InvalidStatusTransition(
            f"产物 {artifact.id} 当前状态 {artifact.status} 不允许 {action}——"
            + _ACTION_HINTS[action]
        )
    by = operator_label(customer_id)
    note = {
        "publish": "发布（draft → published）",
        "archive": "归档（→ archived，数据保留不删除）",
        "restore": "恢复为草稿（archived → draft）",
    }[action]
    artifact.status = target.value
    artifact.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    history = list(artifact.status_history or [])
    history.append(
        {"status": target.value, "at": utc_now_iso(), "by": by, "note": note}
    )
    artifact.status_history = history
    db.commit()
    db.refresh(artifact)
    return artifact


def current_version_time(db: Session, artifact: Artifact) -> str | None:
    """当前版本建立时间（spec delta：as_of 不得晚于"产物该版本建立时间"）。"""
    created_at = db.scalar(
        select(ArtifactVersion.created_at).where(
            ArtifactVersion.artifact_id == artifact.id,
            ArtifactVersion.version == artifact.version,
        )
    )
    if created_at is not None:
        return created_at.isoformat()
    return artifact.updated_at.isoformat() if artifact.updated_at else None


def publish_artifact(
    db: Session,
    artifact: Artifact,
    *,
    customer_id: str,
    info_store: InfoStore | None = None,
    gate_mode: str | None = None,
) -> Artifact:
    """发布：draft → published（published 是稳定引用位）。

    证据发布门（wire-info-stream-program 4.2/4.3）：带 evidence 的产物在
    发布前逐条核对 info-stream（存在性 + 逐字片段 + as_of 不晚于本版本），
    按 ``EVIDENCE_GATE_MODE`` 裁决：

    - ``off``：完全跳过（回滚位——移除校验即恢复原行为）；
    - ``warn``（默认，灰度）：失败只记结构化告警日志（前缀 ``evidence_gate``，
      JSON 含产物 id/失败项/原因，可查），**不阻断发布**；
    - ``enforce``：InvalidArtifactSpec（路由层 422）并指名全部缺失项。

    不带 evidence 的产物三种模式下行为均与变更前一致（零影响）。
    ``info_store``/``gate_mode`` 供测试注入；缺省走 env 装配
    （``FD_INFO_URL`` / ``EVIDENCE_GATE_MODE``）。
    """
    mode = gate_mode or get_evidence_gate_settings().mode
    if mode != GATE_OFF:
        failures = verify_evidence(
            current_spec(db, artifact),
            info_store if info_store is not None else info_store_from_env(),
            version_time=current_version_time(db, artifact),
        )
        if failures:
            detail = "；".join(failure.message() for failure in failures)
            if mode == GATE_ENFORCE:
                raise InvalidArtifactSpec(f"证据校验未通过（{len(failures)} 项）：{detail}")
            for failure in failures:
                logger.warning(
                    "evidence_gate warn %s",
                    json.dumps(
                        {
                            "artifact_id": artifact.id,
                            "customer_id": artifact.customer_id,
                            "version": artifact.version,
                            "index": failure.index,
                            "field": failure.field,
                            "source": failure.source,
                            "stable_id": failure.stable_id,
                            "reason": failure.reason,
                        },
                        ensure_ascii=False,
                    ),
                )
    return _transition(db, artifact, action="publish", customer_id=customer_id)


def archive_artifact(db: Session, artifact: Artifact, *, customer_id: str) -> Artifact:
    """归档：draft/published → archived（不删除，可恢复）。"""
    return _transition(db, artifact, action="archive", customer_id=customer_id)


def restore_artifact(db: Session, artifact: Artifact, *, customer_id: str) -> Artifact:
    """恢复：archived → draft（唯一回退边）。"""
    return _transition(db, artifact, action="restore", customer_id=customer_id)


# ------------------------------------------------------------- 会话草稿导入


def import_from_draft(
    db: Session, *, customer_id: str, conversation_id: int, draft_id: int
) -> Artifact:
    """把会话草稿固化为 draft 产物（version=1，保留来源会话指针）。

    会话不存在/非本人、草稿不存在或不属于该会话一律 404（不泄露他企）。
    """
    try:
        conversation = get_customer_conversation(
            db, conversation_id=conversation_id, customer_id=customer_id
        )
    except ConversationNotFound as exc:
        raise ArtifactNotFound(str(exc)) from exc
    draft = db.get(AIDraftArtifact, draft_id)
    if draft is None or draft.conversation_id != conversation.id:
        raise ArtifactNotFound(
            f"conversation {conversation_id} 无草稿 {draft_id}"
        )
    error = validate_spec(draft.spec)
    if error is not None:  # pragma: no cover - 存量草稿均过工具侧校验，防御位
        raise InvalidArtifactSpec(f"草稿 {draft_id} 的 spec 不合法：{error}")
    kind = draft.kind
    title = str(draft.spec.get("title", "")).strip()[:200] or f"{kind} 产物"
    artifact = Artifact(
        customer_id=customer_id,
        kind=kind,
        title=title,
        status=ArtifactStatus.draft.value,
        version=1,
        origin_conversation_id=conversation.id,
        status_history=[
            {
                "status": ArtifactStatus.draft.value,
                "at": utc_now_iso(),
                "by": operator_label(customer_id),
                "note": f"从会话 {conversation.id} 草稿导入",
            }
        ],
    )
    db.add(artifact)
    db.flush()
    db.add(
        ArtifactVersion(
            artifact_id=artifact.id,
            version=1,
            spec=dict(draft.spec),
            operator=operator_label(customer_id),
        )
    )
    db.commit()
    db.refresh(artifact)
    return artifact


# --------------------------------------------------------------- 空白创建

#: 空白创建的 kind 白名单：dashboard（最小空板）/ form（最小空表单）/
#: alert_rule（规则模板壳，wire-alert-evaluation——alert 此前唯一出生路径
#: 是 AI 草稿转正，而 AI 构建要求 cell，免费档无门）/ record-view（最小空
#: 单记录页，wire-record-view——公开读面产物同样要求 console 出生路径）/
#: doc（空正文最小文档，wire-rich-text-docs——正文型产物无条件可建：人写
#: 文档不依赖任何 AI 构建物）。其余三类（复合指标/研报/定时）结构强，
#: 仍只能由 AI 构建草稿转正产生。
BLANK_CREATE_KINDS: tuple[str, ...] = (
    "dashboard",
    "form",
    "alert_rule",
    "record-view",
    "doc",
)


def _blank_spec(kind: str, title: str) -> dict:
    """空白创建的最小 spec（kind 分派）：dashboard=空面板；form=零字段、
    datasetId=None（构建中间态——可保存不可提交，进构建器再绑定数据集）；
    alert_rule=规则模板壳（占位 dataset_id；出生版本不过规则契约层，首次
    PUT 真实 spec 才校验——评估器对未修占位的 published 规则记 error 事件）；
    record-view=零头部零时间段、datasetId=None 的最小空页（wire-record-view
    契约：未绑数据集的中间态可保存、不可生成分享）；doc=空正文最小文档
    （wire-rich-text-docs：``{format:"markdown", text:""}``，出生即可进
    分栏编辑器写作）。"""
    if kind == FORM_KIND:
        return {"title": title, "spec": {"title": title, "datasetId": None, "fields": []}}
    if kind == DOC_KIND:
        # 浅拷 BLANK_DOC_INNER：内造 spec 会随产物落库并被后续 PUT/草稿路径
        # 读写，绝不能把模块常量本体交出去（调用方 mutate 即污染全进程）。
        return {"title": title, "spec": dict(BLANK_DOC_INNER)}
    if kind == RECORD_VIEW_KIND:
        return {
            "title": title,
            "spec": {
                "datasetId": None,
                "keyColumn": None,
                "headerFields": [],
                "sections": [],
            },
        }
    if kind == ALERT_KIND:
        return {
            "title": title,
            "spec": {
                "query": {"dataset_id": "<数据集id>", "columns": ["<列名>"]},
                "aggregate": {"func": "count"},
                "condition": {"op": "gt", "value": 0},
                "intervalMinutes": 30,
            },
        }
    return {"title": title, "spec": {"title": title, "panels": []}}


def create_blank_artifact(
    db: Session,
    *,
    customer_id: str,
    kind: str,
    title: str,
    spec: dict | None = None,
    datasets_db: Session | None = None,
    source_ref: str | None = None,
) -> Artifact:
    """空白/带内容创建（console 直接创建 draft 产物）。

    产物此前唯一出生路径是会话草稿转正（import_from_draft），而草稿只能由
    AI 构建产生、AI 构建要求 cell——免费档（无 cell）造不出任何看板/表单。
    本函数补第二条出生路径：按 kind 构造最小空 spec（dashboard=空面板、
    form=零字段未绑数据集），走与 from-draft 完全相同的建档手法（version=1
    版本行 + operator 审计 + 初始状态留痕 + title 投影），仅来源会话指针
    为 None。

    ``spec`` 给定时（add-artifact-create-endpoint）创建「带内容 draft 产物」
    （v1 = 给定 spec）：与 spec 更新同一校验栈（kind 分派 + kind 专属层，
    全部落库前零半写），留痕文案如实区分两种出生方式。缺省 None = 既有
    空白创建，行为逐字节不变。

    约束：kind 白名单 BLANK_CREATE_KINDS（dashboard/form/alert_rule/
    record-view/doc）；title strip 后须非空——违者 InvalidArtifactSpec
    （路由层 422），且零半写（拒收不落任何产物/版本行）。
    """
    if kind not in BLANK_CREATE_KINDS:
        raise InvalidArtifactSpec(
            "空白创建仅支持 dashboard、form、alert_rule、record-view、doc"
            "（其他 kind 由 AI 构建产生）"
        )
    if not title.strip():
        raise InvalidArtifactSpec("title 必须是非空字符串")
    title = title.strip()
    note = "空白创建（console 直接新建）"
    if spec is not None:
        spec = dict(spec)
        error = validate_kind_spec(kind, spec)
        if error is not None:
            raise InvalidArtifactSpec(error)
        # kind 专属层（数据集属主/告警契约）与 update_spec 同一函数——
        # 单一真源，任何拒绝发生在落库前
        _require_kind_layers(
            kind, spec, datasets_db=datasets_db, customer_id=customer_id
        )
        note = "带内容创建（console 直接新建）"
    else:
        spec = _blank_spec(kind, title)
    # kind 分派校验（内造最小 spec 恒过——防御位，保证出生版本永远合法）
    error = validate_kind_spec(kind, spec)
    if error is not None:  # pragma: no cover - 内造 spec 恒过校验
        raise InvalidArtifactSpec(error)
    artifact = Artifact(
        customer_id=customer_id,
        kind=kind,
        # title 投影同 update_spec 口径（strip + 截 200）
        title=title[:200],
        status=ArtifactStatus.draft.value,
        version=1,
        # 直接创建无来源会话（区别于 from-draft 保留会话指针）
        origin_conversation_id=None,
        status_history=[
            {
                "status": ArtifactStatus.draft.value,
                "at": utc_now_iso(),
                "by": operator_label(customer_id),
                "note": note,
            }
        ],
    )
    db.add(artifact)
    db.flush()
    db.add(
        ArtifactVersion(
            artifact_id=artifact.id,
            version=1,
            spec=spec,
            operator=operator_label(customer_id),
            source_ref=source_ref,
        )
    )
    db.commit()
    db.refresh(artifact)
    return artifact


# ------------------------------------------------- 带内容创建 + slug 幂等


def _spec_slug(spec: object) -> str | None:
    """取 artifact spec 信封里的幂等键（``spec.spec.showcase_slug``）。

    ``showcase_slug`` 是 spec 的自由扩展位（import_showcase 既有约定），
    不在数据库列里。非 dict / 空 / 非字符串一律 None（视为无幂等键）。
    """
    if not isinstance(spec, dict):
        return None
    inner = spec.get("spec")
    if not isinstance(inner, dict):
        return None
    slug = inner.get("showcase_slug")
    return slug if isinstance(slug, str) and slug else None


def find_slug_hit(db: Session, *, customer_id: str, slug: str) -> Artifact | None:
    """同租户内按 ``showcase_slug`` 查既有产物（幂等查重）。

    只在请求方租户域内查（跨租户同 slug 互不命中）；遍历该租户产物并读
    各自最新版本行比对（平台租户产物量级几十条，代价可忽略；超规模再落
    列建索引）。查重域不挑 kind——slug 是发布面概念，同租户同 slug 的
    任何 kind 都视为同一产物的重放。
    """
    for artifact in list_artifacts(db, customer_id=customer_id):
        if _spec_slug(current_spec(db, artifact)) == slug:
            return artifact
    return None


def _specs_equal(a: dict, b: dict) -> bool:
    """规范化 JSON 语义相等（键序无关）——避免键序差异制造假版本。"""
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(
        b, sort_keys=True, ensure_ascii=False
    )


def create_or_replay_artifact(
    db: Session,
    *,
    customer_id: str,
    kind: str,
    title: str,
    spec: dict,
    datasets_db: Session | None = None,
) -> tuple[Artifact, bool]:
    """带内容创建 + ``showcase_slug`` 幂等（本 change 核心）。

    返回 ``(产物, 是否新建)``。slug 命中同租户既有产物时**绝不建重**：
    内容一致零操作（原样返回，不追加冗余版本——「重跑 = 确保最新内容在线」
    的单调语义）；有变化走 update_spec 同款追加版本（append-only；
    archived 拒改 ArtifactLocked，与 PUT 同口径，路由层 409）。未命中或
    无幂等键走普通创建。
    """
    slug = _spec_slug(spec)
    if slug:
        hit = find_slug_hit(db, customer_id=customer_id, slug=slug)
        if hit is not None:
            if _specs_equal(current_spec(db, hit), spec):
                return hit, False
            update_spec(
                db,
                hit,
                spec=spec,
                customer_id=customer_id,
                datasets_db=datasets_db,
            )
            return hit, False
    artifact = create_blank_artifact(
        db,
        customer_id=customer_id,
        kind=kind,
        title=title,
        spec=spec,
        datasets_db=datasets_db,
    )
    return artifact, True


# ------------------------------------------------------------- 草稿层（doc）

#: doc 草稿层是「工作区」不是「里程碑」（wire-rich-text-docs design D3）：
#: 自动保存高频覆盖写这一行，**绝不**产生版本行；版本链只在显式保存
#: （update_spec）时追加。草稿层 SHALL NOT 参与状态机/分享钉版/市场判定
#: （读它的入口只有编辑器读路径一处）。


def get_doc_draft(db: Session, artifact: Artifact) -> ArtifactDraft | None:
    """读 doc 草稿层（无草稿行为 None——编辑器回退当前版本 spec.text）。"""
    return db.scalar(
        select(ArtifactDraft).where(ArtifactDraft.artifact_id == artifact.id)
    )


def save_doc_draft(
    db: Session, *, artifact: Artifact, text: object, customer_id: str
) -> ArtifactDraft:
    """自动保存：UPSERT 草稿层一行（覆盖写，不产生版本行——design D3）。

    正文口径与版本保存**完全一致**（doc_spec.validate_doc_text，wire-rich-
    text-docs 任务 2.2）：同一个上限、同一段文案——不留下「草稿能存但版本
    存不了」的错位（否则自动保存悄悄超限，点「保存版本」才炸）。

    拒绝面：kind≠doc 一律 422（草稿层只有正文型产物用得上；其余 kind 的
    中间态在各自构建器内，不入库）；archived 拒写（与 update_spec 同口径，
    路由层 409——归档是只读位）。任何拒绝**零半写**（不改草稿行、不动
    产物行）。
    """
    if artifact.kind != DOC_KIND:
        raise InvalidArtifactSpec("仅 doc 产物支持草稿层")
    if artifact.status == ArtifactStatus.archived.value:
        raise ArtifactLocked("已归档产物不可写入草稿")
    error = validate_doc_text(text)
    if error is not None:
        raise InvalidArtifactSpec(error)
    row = get_doc_draft(db, artifact)
    if row is None:
        row = ArtifactDraft(
            artifact_id=artifact.id,
            text=text,
            updated_at=utc_now_naive(),
            updated_by=operator_label(customer_id),
        )
        db.add(row)
    else:
        row.text = text
        row.updated_at = utc_now_naive()
        row.updated_by = operator_label(customer_id)
    db.commit()
    db.refresh(row)
    return row
