"""五类产物工具的 schema 与「产物 spec 写入」假语义执行。

v1 工具语义：LLM 只拿到五类产物的 **spec 写入** 工具——执行=把 spec
暂存进 ``ai_draft_artifacts``（status=draft）。真正的 cell 物化
（落库/建刷新/渲染）是任务 5.3/5.5 的活；这里绝不产生半成品——
入参不合法即拒绝暂存并把错误如实回灌给 LLM。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .models import ARTIFACT_STATUS_DRAFT, AIDraftArtifact

#: 产物工具名 → 暂存 kind 短名（五类产物：仪表盘/组合指标/研究报告/告警/定时刷新）
ARTIFACT_TOOL_KINDS: dict[str, str] = {
    "dashboard_spec_create": "dashboard",
    "composite_indicator_create": "composite_indicator",
    "research_report_create": "research_report",
    "alert_rule_create": "alert_rule",
    "refresh_schedule_create": "refresh_schedule",
}


def _artifact_tool_schema(name: str, kind_cn: str, spec_desc: str) -> dict:
    """五类产物工具共用一个「写入产物规格」参数面（假语义，spec 自由 JSON）。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": (
                f"写入一份{kind_cn}产物规格：spec 暂存为草稿（draft），"
                "经客户确认后由平台物化到本企业 cell。spec 不完整时不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": f"{kind_cn}产物名称（客户可见）",
                    },
                    "spec": {
                        "type": "object",
                        "description": spec_desc,
                    },
                },
                "required": ["title", "spec"],
            },
        },
    }


#: 提供给 LLM 的五类产物工具 schema（OpenAI tools 形态）
TOOL_SCHEMAS: list[dict] = [
    _artifact_tool_schema(
        "dashboard_spec_create",
        "仪表盘",
        # v2 结构示例对齐 web 渲染器契约（web/src/console/dashboard/spec.ts）：
        # 面板类型谱（基础五类 + 高级十类）/ 编码通道与轴类型 / 多窗格 /
        # 叠加层 / raw_option 逃生舱（wire-chart-contract-v2）——AI 产物默认
        # 可渲染可分享（wire-dashboard-share）；面板可选 query 引用——刷新走
        # 确定性契约路径零 AI（wire-dashboard-query-contract）。
        # list（wire-list-panel）：图文信息流面板，角色列映射见下方说明。
        (
            "仪表盘规格 v2（JSON）："
            '{"title":"仪表盘名","panels":[{"id":"p1",'
            '"type":"kpi|line|bar|table|list|candlestick|scatter|pie|heatmap|gauge|radar|treemap|sankey|funnel|boxplot",'
            '"title":"面板标题","span":1,"source":"指标code或数据集名",'
            '"encoding":{"x":"日期","y":["数值"],"xType":"category|value|time|log",'
            '"yType":"category|value|time|log","xName":"横轴名","yName":"纵轴名"},'
            '"panes":[{"title":"副图","encoding":{},"overlays":{},"height":1}],'
            '"overlays":{"markLine":[{"value":50,"label":"荣枯线","lineStyle":"dashed"}],'
            '"markArea":[{"from":"2020","to":"2021","label":"区间"}],'
            '"markPoint":[{"x":"2024","y":4.8,"label":"峰值"}]},'
            '"height":320,'
            '"query":{"dataset_id":"数据集id",'
            '"filters":[{"column":"列名","op":"eq|in|gt|gte|lt|lte|contains|startswith","value":null}],'
            '"sort":[{"column":"列名","dir":"asc|desc"}],"limit":100,"columns":["列名"]},'
            '"snapshot":{"columns":["年份","GDP"],"rows":[["2023",129.4]]},'
            '"config":{"unit":"万亿元","subtitle":"不变价"}}]}。'
            "字段说明：type 取上述十五类之一——基础五类（kpi/line/bar/table/"
            "list）不带 encoding 即可渲染；高级十类建议显式给 encoding。span 为 "
            "3 列栅格宽度（1/2/3）；height 可选（像素，缺省 220）。把取到的数据"
            "写进 snapshot（columns 列名 + rows 行值，kpi 数值取 rows[0][0]，"
            "table 整表呈现，list 按行渲染卡片）——渲染器只读 snapshot"
            "（raw_option 面板除外），不要放占位符或待查标记。"
            "encoding 是显式编码通道：x 指定横轴列、y 指定纵轴列（可多列，每列"
            "一个系列）、series 指定分组列（长表形态）、size 指定大小列；"
            "xType/yType 取 category/value/time/log——跨年份、跨月份等时间序列"
            "必须用 xType 为 time（类目轴会把不等间隔日期画成等距，伪造趋势）。"
            "candlestick 的 y 列序约定为 开盘/最高/最低/收盘，boxplot 为 "
            "min/q1/median/q3/max。"
            "list 是图文信息流（新闻/公告/资讯类数据的卡片流）：仅当数据含标题"
            "等文本列、且用户要的是「信息流/动态/资讯」时使用；encoding 必须给 "
            "title（标题列），可选给 summary/image/time/source/link（摘要/图片/"
            "时间/来源/外链列），值均为 snapshot 列名；图片与外链列的值必须是 "
            "http(s) URL；顺序按 snapshot 行序呈现——要最新在前，请把 query 的 "
            "sort 设为时间列 desc 并限制 limit。不得把长文塞进 table 假扮信息流。"
            "panes 是多窗格：首个为主图、其余为副图，共享同一横轴与十字光标"
            "（价格+成交量、指标+MACD 的既有范式）；窗格 height 为相对权重。"
            "overlays 是叠加层：markLine 画阈值/参考线（axis 可设 x 画垂直线）、"
            "markArea 高亮区间、markPoint 标事件点。"
            "raw_option 是逃生舱，只在类型化字段确实表达不了时使用（完整图表"
            "配置直通）；其内容必须是纯 JSON——绝不允许函数、正则或任何不可"
            "序列化的值（写了会被判不合契约并整面板拒绝）。"
            "config 可选（unit/subtitle/yName/xName；list 另可给 limit=1..20）。"
            "面板 source 应填指标 code 或数据集名（后续「刷新数据」依赖此标注"
            "重新取数）。"
            "面板数据取自数据集查询工具（dataset_query 等）时，该面板必须"
            "同时带 query 引用：把所用 dataset_id 与 filters/sort/limit/"
            "columns 查询形参如实写进 panel.query（「刷新数据」按契约确定性"
            "重取，零 AI）；kpi 单值面板用 rows 模式约定：sort 到最近一期 + "
            "limit 1 + columns 投影到值列；source 仍填展示标注。"
        ),
    ),
    _artifact_tool_schema(
        "composite_indicator_create",
        "组合指标",
        "组合指标规格：公式、来源指标 code、单位与口径（JSON）",
    ),
    _artifact_tool_schema(
        "research_report_create",
        "研究报告",
        "研究报告规格：大纲、章节要点、数据范围（JSON）",
    ),
    _artifact_tool_schema(
        "alert_rule_create",
        "告警规则",
        "告警规则规格：触发条件、阈值、通知渠道（JSON）",
    ),
    _artifact_tool_schema(
        "refresh_schedule_create",
        "定时刷新",
        "定时刷新规格：刷新频率、客户时区、目标产物（JSON）",
    ),
]


def validate_artifact_args(tool_name: str, args: dict[str, Any]) -> str | None:
    """校验产物工具入参；返回错误文案（None=合法）。

    不合法绝不落暂存表——「不留半成品」的第一道闸。
    """
    if tool_name not in ARTIFACT_TOOL_KINDS:
        return f"未知产物工具: {tool_name!r}"
    if not isinstance(args, dict):
        return f"工具 {tool_name} 入参必须是 JSON 对象"
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        return f"工具 {tool_name} 缺少非空 title"
    spec = args.get("spec")
    if not isinstance(spec, dict) or not spec:
        return f"工具 {tool_name} 缺少非空 spec 对象"
    if tool_name == "dashboard_spec_create":
        return _validate_dashboard_v2_payload(spec)
    return None


#: v2 面板类型谱（与 web spec.ts 的 DASHBOARD_PANEL_TYPES 同集合）。
DASHBOARD_PANEL_TYPES_V2: frozenset[str] = frozenset(
    {
        "kpi",
        "line",
        "bar",
        "table",
        "list",
        "candlestick",
        "scatter",
        "pie",
        "heatmap",
        "gauge",
        "radar",
        "treemap",
        "sankey",
        "funnel",
        "boxplot",
    }
)


def _find_unserializable(value: Any, path: str) -> str | None:
    """深度遍历检出不可序列化值（函数/正则/类实例）；None=通过。

    与 web spec.ts 的 findUnserializable 同口径：JSON 解析出来的纯数据
    一律合法（含字符串形式的 formatter），只有真正的非纯对象要拒。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return None
    if isinstance(value, (bytes, bytearray)):
        return f"{path} 是字节串"
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            problem = _find_unserializable(item, f"{path}[{index}]")
            if problem:
                return problem
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            problem = _find_unserializable(item, f"{path}.{key}")
            if problem:
                return problem
        return None
    return f"{path} 不是可序列化的 JSON 值"


def _validate_dashboard_v2_payload(spec: dict) -> str | None:
    """dashboard 生成侧结构闸（wire-chart-contract-v2）：面板类型与 raw_option。

    **只做两件事**——把「AI 写了个不存在的图表类型」和「AI 在 raw_option 里
    塞函数」在落库前拦住（后者落库会渲染成坏面板，前端还会整面板跳过）。
    其余形状宽容交给前端解析层（沿用既有纪律：不整板失败）。

    历史 v1 产物无 raw_option、type 也都在集合内，不会被这里拒——存量零漂移。
    """
    panels = spec.get("panels")
    if panels is None:
        return None  # 空板/其他形状：交给前端宽容解析
    if not isinstance(panels, list):
        return "dashboard spec.panels 必须是数组"
    for index, panel in enumerate(panels):
        if not isinstance(panel, dict):
            return f"dashboard spec.panels[{index}] 必须是 JSON 对象"
        panel_type = panel.get("type")
        if panel_type is not None and panel_type not in DASHBOARD_PANEL_TYPES_V2:
            allowed = " / ".join(sorted(DASHBOARD_PANEL_TYPES_V2))
            return (
                f"dashboard spec.panels[{index}].type 不受支持：{panel_type!r}"
                f"（可取 {allowed}）"
            )
        # list（wire-list-panel）：title 角色是渲染前提——生成侧先拦，
        # 免得落库后前端整面板判坏。
        if panel_type == "list":
            encoding = panel.get("encoding")
            title = encoding.get("title") if isinstance(encoding, dict) else None
            if not isinstance(title, str) or not title.strip():
                return (
                    f"dashboard spec.panels[{index}] 是 list 面板，"
                    "encoding.title（标题列）必填"
                )
        if panel.get("raw_option") is not None:
            raw = panel["raw_option"]
            if not isinstance(raw, dict):
                return f"dashboard spec.panels[{index}].raw_option 必须是 JSON 对象"
            problem = _find_unserializable(raw, f"panels[{index}].raw_option")
            if problem:
                return f"dashboard spec 含不可序列化的值：{problem}"
    return None


def stage_artifact(
    db: Session,
    *,
    conversation_id: int,
    customer_id: str,
    tool_name: str,
    args: dict[str, Any],
) -> dict:
    """产物工具的「执行」：spec 暂存进 ``ai_draft_artifacts``。

    返回回灌给 LLM 的结果 dict（``ok`` + artifact 概要或错误说明）。
    """
    error = validate_artifact_args(tool_name, args)
    if error is not None:
        return {"ok": False, "error": error}
    artifact = AIDraftArtifact(
        conversation_id=conversation_id,
        customer_id=customer_id,
        kind=ARTIFACT_TOOL_KINDS[tool_name],
        spec=dict(args),
        status=ARTIFACT_STATUS_DRAFT,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return {
        "ok": True,
        "artifact_id": artifact.id,
        "kind": artifact.kind,
        "title": args.get("title"),
        "status": artifact.status,
        "note": "产物规格已暂存为草稿（draft），待客户确认后物化到企业 cell",
    }
