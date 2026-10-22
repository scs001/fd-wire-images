"""副驾内部读工具（wire-copilot-action-gate design D1/D2）：直调 service 层。

单一副驾循环的工具面 = 本模块读工具 + ``propose_action`` + cell 工具（有
引擎时动态注入）。读不过闸（risk=read 直通）——但全部是**自有数据读取**
（本团队数据集/产物/需求单），任意档位可用、不依赖数据处理引擎；这与
既有客户面 REST 的可见性一致，不引入新的越权面。

与 cell 工具的分工：cell 工具做指标探索（引擎物化层数据，需引擎）；本
模块读的是 wire 自库的客户资产（datasets/artifacts/demands 切片）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session


def _tool(name: str, description: str, params: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": params, "required": required},
        },
    }


#: 读工具 schema（模型可见面；无引擎依赖）
READ_TOOL_SCHEMAS: list[dict] = [
    _tool(
        "dataset_list",
        "列出本团队的全部数据集（id/名称/来源/存储层/行数）。回答「我有哪些数据」"
        "或为构建选数据集时先用它。",
        {},
        [],
    ),
    _tool(
        "dataset_schema",
        "读一个数据集的列结构（列名与类型）。回答「这表有什么字段」、为表单/"
        "看板选列时用。",
        {"dataset_id": {"type": "string", "description": "数据集 id（dataset_list 的 id）"}},
        ["dataset_id"],
    ),
    _tool(
        "artifact_list",
        "列出本团队的全部产物（id/标题/类型/状态/版本）。回答「我有哪些看板/表单」时用。",
        {
            "kind": {
                "type": "string",
                "description": "可选过滤：dashboard/form/record_view/composite_indicator/"
                "research_report/alert_rule/refresh_schedule/doc",
            }
        },
        [],
    ),
    _tool(
        "artifact_get",
        "读一个产物的完整规格（spec，含看板面板/表单字段）。要基于既有产物修改"
        "或解释其结构时用。",
        {"artifact_id": {"type": "integer", "description": "产物 id（artifact_list 的 id）"}},
        ["artifact_id"],
    ),
    _tool(
        "demands_list",
        "列出本团队已提交的数据需求单（编号/状态/摘要）。回答「我的需求进展」时用。",
        {},
        [],
    ),
]

_READ_TOOL_NAMES = frozenset(item["function"]["name"] for item in READ_TOOL_SCHEMAS)


def is_read_tool(name: str) -> bool:
    return name in _READ_TOOL_NAMES


class ReadToolExecutor:
    """读工具执行器：datasets / artifacts（ai 库）/ demands 三切片会话直调。

    会话由路由层注入（各库各取各的），测试可整体覆写依赖。
    """

    def __init__(self, *, ai_db: Session, datasets_db: Session, demands_db: Session) -> None:
        self._ai = ai_db
        self._datasets = datasets_db
        self._demands = demands_db

    def execute(self, tool_name: str, args: dict[str, Any], *, customer_id: str) -> dict:
        """执行一次读；未知名/参数错 → 如实错误 dict（回灌模型，不抛断对话）。"""
        try:
            if tool_name == "dataset_list":
                return self._dataset_list(customer_id)
            if tool_name == "dataset_schema":
                return self._dataset_schema(customer_id, args)
            if tool_name == "artifact_list":
                return self._artifact_list(customer_id, args)
            if tool_name == "artifact_get":
                return self._artifact_get(customer_id, args)
            if tool_name == "demands_list":
                return self._demands_list(customer_id)
            return {"ok": False, "error": f"未知读工具: {tool_name!r}"}
        except Exception as exc:  # noqa: BLE001 - 读失败如实回灌，不中断对话
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    # ── 各工具实现 ─────────────────────────────────────────────────────────

    def _dataset_list(self, customer_id: str) -> dict:
        from ..datasets.models import Dataset

        rows = list(
            self._datasets.scalars(
                select(Dataset)
                .where(Dataset.customer_id == customer_id)
                .order_by(Dataset.created_at.desc(), Dataset.id)
            ).all()
        )
        return {
            "ok": True,
            "count": len(rows),
            "datasets": [
                {
                    "id": row.id,
                    "name": row.name,
                    "key": row.key,
                    "source_kind": row.source_kind,
                    "storage_tier": getattr(row, "storage_tier", "cell"),
                    "row_count": row.row_count,
                }
                for row in rows
            ],
        }

    def _dataset_schema(self, customer_id: str, args: dict[str, Any]) -> dict:
        from ..datasets.models import Dataset

        dataset_id = args.get("dataset_id")
        dataset = (
            self._datasets.get(Dataset, dataset_id)
            if isinstance(dataset_id, str)
            else None
        )
        if dataset is None or dataset.customer_id != customer_id:
            return {"ok": False, "error": f"数据集 {dataset_id} 不存在或不属于当前团队。"}
        columns = [
            {"name": col.get("name"), "type": col.get("type")}
            for col in (dataset.columns_json or [])
            if isinstance(col, dict)
        ]
        return {
            "ok": True,
            "dataset_id": dataset.id,
            "name": dataset.name,
            "row_count": dataset.row_count,
            "columns": columns,
        }

    def _artifact_list(self, customer_id: str, args: dict[str, Any]) -> dict:
        from ..artifacts.service import list_artifacts

        rows = list_artifacts(
            self._ai,
            customer_id=customer_id,
            kind=args.get("kind") if isinstance(args.get("kind"), str) else None,
        )
        return {
            "ok": True,
            "count": len(rows),
            "artifacts": [
                {
                    "id": row.id,
                    "title": row.title,
                    "kind": row.kind,
                    "status": row.status,
                    "version": row.version,
                }
                for row in rows
            ],
        }

    def _artifact_get(self, customer_id: str, args: dict[str, Any]) -> dict:
        from ..artifacts.service import ArtifactNotFound, current_spec, get_customer_artifact

        artifact_id = args.get("artifact_id")
        if not isinstance(artifact_id, int):
            return {"ok": False, "error": "artifact_get 必须带整数 artifact_id。"}
        try:
            artifact = get_customer_artifact(
                self._ai, artifact_id=artifact_id, customer_id=customer_id
            )
        except ArtifactNotFound:
            return {"ok": False, "error": f"产物 {artifact_id} 不存在或不属于当前团队。"}
        return {
            "ok": True,
            "id": artifact.id,
            "title": artifact.title,
            "kind": artifact.kind,
            "status": artifact.status,
            "version": artifact.version,
            "spec": current_spec(self._ai, artifact),
        }

    def _demands_list(self, customer_id: str) -> dict:
        from ..demands.service import list_demands

        rows = list_demands(self._demands, customer_id=customer_id)
        return {
            "ok": True,
            "count": len(rows),
            "demands": [
                {
                    "id": row.id,
                    "code": row.code,
                    "status": row.status,
                    "title": getattr(row, "title", None) or (row.description or "")[:60],
                }
                for row in rows
            ],
        }
