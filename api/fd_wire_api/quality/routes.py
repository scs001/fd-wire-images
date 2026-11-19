"""数据集质量快照端点（wire-asset-catalog 任务组 4，customer 面）。

客户面鉴权与 datasets 切片同款（``Bearer ID token → require_console_customer_id``）：

- ``GET /api/v1/quality/summary?dataset_ids=a,b,c``——列表徽章聚合视图
  （freshness/last_refresh_at/row_count/status；任何登录角色含 viewer 可读）；
- ``GET /api/v1/datasets/{dataset_id}/quality``——详情：最新快照（失败/
  不支持如实呈现，不用旧值冒充）+ 历史趋势 + 新鲜度 + 声明节奏；
- ``POST /api/v1/datasets/{dataset_id}/quality/refresh``——手动触发
  （owner/editor；viewer 对变更类方法统一 403——wire-team-spaces 门禁），
  引擎计算在服务端完成，返回快照行（含 ``failed``/``unsupported`` 的
  如实失败记录）。

口径标注：质量信息是**指示性检查，非质量认证**（spec「呈现如实」），
两个读端点响应都带 ``note`` 字段，控制台据此外显。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..assets.routes import require_asset_editor
from ..console.auth import (
    require_console_customer_actor,
    require_console_customer_id,
)
from ..datasets import acl
from ..datasets.db import get_db
from ..datasets.ingest import (
    EngineClientFactory,
    get_engine_client_factory,
)
from ..datasets.models import Dataset
from ..datasets.routes import require_cell_tier
from . import rules, service
from .models import DatasetQualityRule, DatasetQualityRuleResult
from .service import DISCLAIMER

router = APIRouter(tags=["dataset-quality"])

#: 单次 summary 的 dataset_ids 数量上限（列表页徽章批量拉取的合理上界）
_SUMMARY_MAX_IDS = 200


def _dataset_or_404(db: Session, dataset_id: str, customer_id: str) -> Dataset:
    """数据集属主校验（跨企业/不存在一律 404，不泄露存在性）。"""
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.customer_id != customer_id:
        raise HTTPException(status_code=404, detail=f"数据集 {dataset_id} 不存在")
    return dataset


@router.get("/api/v1/quality/summary")
def get_quality_summary(
    dataset_ids: str = Query(
        ..., description="逗号分隔的数据集 id 列表（列表页徽章批量拉取）"
    ),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """徽章聚合：每数据集 freshness + last_refresh_at + row_count + status。"""
    ids: list[str] = []
    for chunk in dataset_ids.split(","):
        name = chunk.strip()
        if name and name not in ids:
            ids.append(name)
    if not ids:
        raise HTTPException(status_code=422, detail="dataset_ids 不能为空")
    if len(ids) > _SUMMARY_MAX_IDS:
        raise HTTPException(
            status_code=422,
            detail=f"dataset_ids 一次至多 {_SUMMARY_MAX_IDS} 个（本次 {len(ids)}）",
        )
    statuses = service.quality_summary(db, customer_id, ids)
    return {"statuses": statuses, "note": DISCLAIMER}


@router.get("/api/v1/datasets/{dataset_id}/quality")
def get_dataset_quality(
    dataset_id: str,
    principal: tuple = Depends(require_console_customer_actor),
    db: Session = Depends(get_db),
) -> dict:
    """质量详情：latest（含失败/不支持如实态）+ 历史趋势 + 新鲜度 + 节奏。

    wire-dataset-acl：restricted 数据集无 view 授权 → 403（数据面各面同闸）。
    """
    customer, actor_sub, role = principal
    dataset = _dataset_or_404(db, dataset_id, str(customer.name))
    acl.can_access(
        db, customer=customer, actor_sub=actor_sub, role=role, dataset=dataset
    )
    return service.quality_detail(db, dataset)


@router.post("/api/v1/datasets/{dataset_id}/quality/refresh")
def refresh_dataset_quality(
    dataset_id: str,
    principal: tuple = Depends(require_console_customer_actor),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """手动触发一次质量快照（owner/editor；viewer 403 由统一角色门禁拦）。

    返回快照行本身——引擎失败/不支持时行内 ``status``/``error_text``
    如实呈现（不伪造成功）。api 层数据集（免费档）403 档位闸：质量计算
    在 cell 引擎侧，api 层数据不在引擎侧，平台侧直读被红线禁止。

    wire-dataset-acl：手动快照属写面（restricted 需 edit；档位闸先行）。
    """
    customer, actor_sub, role = principal
    customer_id = str(customer.name)
    dataset = _dataset_or_404(db, dataset_id, customer_id)
    require_cell_tier(dataset, "质量快照")
    acl.can_access(
        db,
        customer=customer,
        actor_sub=actor_sub,
        role=role,
        dataset=dataset,
        need=acl.NEED_WRITE,
    )
    snapshot = service.capture_snapshot(
        customer_id,
        dataset,
        service.TRIGGER_MANUAL,
        client=engine_factory(customer_id),
        db=db,
    )
    return service.snapshot_to_dict(snapshot)


# ------------------------------------------------------------------ 质量规则
# wire-quality-rules：客户自定义规则（指标+阈值）+ 求值结果（GX 契约）。
# cell 档专属（与质量快照同款档位闸——api 层数据不在引擎侧，平台侧直读
# 被红线禁止，如实 403 不假执行）。


class RuleCreateIn(BaseModel):
    name: str | None = None
    check: dict


class RulePatchIn(BaseModel):
    name: str | None = None
    check: dict | None = None
    enabled: bool | None = None


def _translate_rule_validation(fn, *args):
    """DSL 校验失败 → 422（字段级原因；绝不触引擎）。"""
    try:
        return fn(*args)
    except rules.RuleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _rule_or_404(db: Session, dataset_id: str, rule_id: int) -> DatasetQualityRule:
    rule = db.get(DatasetQualityRule, rule_id)
    if rule is None or rule.dataset_id != dataset_id:
        raise HTTPException(status_code=404, detail=f"质量规则 {rule_id} 不存在")
    return rule


@router.get("/api/v1/datasets/{dataset_id}/quality-rules")
def list_quality_rules(
    dataset_id: str,
    principal: tuple = Depends(require_console_customer_actor),
    db: Session = Depends(get_db),
) -> dict:
    """规则列表（任意登录角色含 viewer 可读；每数据集上限见 settings）。"""
    customer, actor_sub, role = principal
    dataset = _dataset_or_404(db, dataset_id, str(customer.name))
    require_cell_tier(dataset, "质量规则")
    acl.can_access(
        db, customer=customer, actor_sub=actor_sub, role=role, dataset=dataset
    )
    rows = db.execute(
        select(DatasetQualityRule)
        .where(DatasetQualityRule.dataset_id == dataset.id)
        .order_by(DatasetQualityRule.id)
    ).scalars().all()
    latest: dict[int, DatasetQualityRuleResult] = {}
    if rows:
        result_rows = db.execute(
            select(DatasetQualityRuleResult)
            .where(
                DatasetQualityRuleResult.dataset_id == dataset.id,
                DatasetQualityRuleResult.rule_id.in_([r.id for r in rows]),
            )
            .order_by(DatasetQualityRuleResult.id.desc())
        ).scalars().all()
        for row in result_rows:
            latest.setdefault(row.rule_id, row)
    return {
        "items": [rules.rule_to_dict(r) for r in rows],
        "latest_results": {
            str(rid): rules.result_to_dict(row) for rid, row in latest.items()
        },
        "note": DISCLAIMER,
    }


@router.post("/api/v1/datasets/{dataset_id}/quality-rules", status_code=201)
def create_quality_rule(
    dataset_id: str,
    body: RuleCreateIn,
    principal: tuple = Depends(require_asset_editor),
    db: Session = Depends(get_db),
) -> dict:
    """添加规则（owner/editor；viewer 403 由 require_asset_editor 拦）。

    DSL 封闭枚举校验在先（design D3：校验不过 422 且零引擎调用）。
    """
    customer, sub = principal
    dataset = _dataset_or_404(db, dataset_id, str(customer.name))
    require_cell_tier(dataset, "质量规则")
    acl.can_access(
        db,
        customer=customer,
        actor_sub=sub,
        role=None,
        dataset=dataset,
        need=acl.NEED_WRITE,
    )
    name = _translate_rule_validation(rules.validate_name, body.name)
    check = _translate_rule_validation(
        rules.validate_check, body.check, rules.dataset_columns(dataset)
    )
    max_rules = service.get_settings().quality_rules_max_per_dataset
    count = len(
        db.execute(
            select(DatasetQualityRule.id).where(
                DatasetQualityRule.dataset_id == dataset.id
            )
        ).scalars().all()
    )
    if count >= max_rules:
        raise HTTPException(
            status_code=422,
            detail=f"每数据集质量规则至多 {max_rules} 条（已 {count} 条）",
        )
    rule = DatasetQualityRule(
        dataset_id=dataset.id, name=name, check_json=check,
        enabled=True, updated_by=sub,
    )
    db.add(rule)
    db.commit()
    return rules.rule_to_dict(rule)


@router.patch("/api/v1/datasets/{dataset_id}/quality-rules/{rule_id}")
def patch_quality_rule(
    dataset_id: str,
    rule_id: int,
    body: RulePatchIn,
    principal: tuple = Depends(require_asset_editor),
    db: Session = Depends(get_db),
) -> dict:
    """编辑规则（改名/改检查体/启停）；updated_by 记服务端身份。"""
    customer, sub = principal
    dataset = _dataset_or_404(db, dataset_id, str(customer.name))
    require_cell_tier(dataset, "质量规则")
    acl.can_access(
        db,
        customer=customer,
        actor_sub=sub,
        role=None,
        dataset=dataset,
        need=acl.NEED_WRITE,
    )
    rule = _rule_or_404(db, dataset.id, rule_id)
    if body.name is not None:
        rule.name = _translate_rule_validation(rules.validate_name, body.name)
    if body.check is not None:
        rule.check_json = _translate_rule_validation(
            rules.validate_check, body.check, rules.dataset_columns(dataset)
        )
    if body.enabled is not None:
        rule.enabled = body.enabled
    rule.updated_by = sub
    db.commit()
    return rules.rule_to_dict(rule)


@router.delete(
    "/api/v1/datasets/{dataset_id}/quality-rules/{rule_id}", status_code=204
)
def delete_quality_rule(
    dataset_id: str,
    rule_id: int,
    principal: tuple = Depends(require_asset_editor),
    db: Session = Depends(get_db),
) -> None:
    """删除规则（历史结果保留可查——append-only 纪律不动结果行）。"""
    customer, _sub = principal
    dataset = _dataset_or_404(db, dataset_id, str(customer.name))
    require_cell_tier(dataset, "质量规则")
    acl.can_access(
        db,
        customer=customer,
        actor_sub=_sub,
        role=None,
        dataset=dataset,
        need=acl.NEED_WRITE,
    )
    rule = _rule_or_404(db, dataset.id, rule_id)
    db.delete(rule)
    db.commit()


@router.post("/api/v1/datasets/{dataset_id}/quality-rules/run")
def run_quality_rules(
    dataset_id: str,
    principal: tuple = Depends(require_asset_editor),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """立即求值全部启用规则（逐条隔离——引擎不可达逐条 error 非整批 5xx）。"""
    customer, _sub = principal
    dataset = _dataset_or_404(db, dataset_id, str(customer.name))
    require_cell_tier(dataset, "质量规则")
    acl.can_access(
        db,
        customer=customer,
        actor_sub=_sub,
        role=None,
        dataset=dataset,
        need=acl.NEED_WRITE,
    )
    results = rules.evaluate_dataset_rules(
        db,
        dataset=dataset,
        triggered_by=service.TRIGGER_MANUAL,
        client=engine_factory(str(customer.name)),
    )
    return {
        "results": [rules.result_to_dict(r) for r in results],
        "ran_at": _now_iso(),
    }


def _now_iso() -> str:
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc).isoformat()


@router.get("/api/v1/datasets/{dataset_id}/quality-rules/results")
def list_quality_rule_results(
    dataset_id: str,
    rule_id: int | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    principal: tuple = Depends(require_console_customer_actor),
    db: Session = Depends(get_db),
) -> dict:
    """求值结果历史（按时间倒序；append-only）。"""
    customer, actor_sub, role = principal
    dataset = _dataset_or_404(db, dataset_id, str(customer.name))
    require_cell_tier(dataset, "质量规则")
    acl.can_access(
        db, customer=customer, actor_sub=actor_sub, role=role, dataset=dataset
    )
    stmt = select(DatasetQualityRuleResult).where(
        DatasetQualityRuleResult.dataset_id == dataset.id
    )
    if rule_id is not None:
        stmt = stmt.where(DatasetQualityRuleResult.rule_id == rule_id)
    rows = db.execute(
        stmt.order_by(DatasetQualityRuleResult.id.desc()).limit(limit)
    ).scalars().all()
    return {"items": [rules.result_to_dict(r) for r in rows]}
