"""源看板图表形状的封闭集合（openspec change `fix-showcase-table-panel`）。

源 ``charts[]`` 每项 SHALL 恰好是 echarts 图（``option``）或表（``table``）之一。
缺形状与双形状都必须 **硬失败**：静默降级成空面板比失败导入贵——失败会红，
坏面板不会（``cnreport-financials`` 用 table 源把整批发布挡了三天才发现）。

``_chart_to_panel`` 是纯函数（不连库、不打网络），所以本文件可离线跑。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest

from fd_wire_api.scripts.import_showcase import _chart_to_panel, build_showcase_spec

TABLE = {
    "columns": ["科目", "数值", "单位"],
    "rows": [["营业总收入", "1,234.00", "元"]],
}


# ------------------------------------------------------------------ 正常两分支


def test_option_chart_still_becomes_a_raw_option_panel():
    panel = _chart_to_panel(
        0,
        {"id": "c1", "title": "图一", "source": "某个源", "height": 430,
         "option": {"series": [{"type": "line", "data": [1, 2]}]}},
    )
    assert panel["type"] == "line"
    assert panel["raw_option"]["series"][0]["data"] == [1, 2]
    assert "snapshot" not in panel
    assert (panel["id"], panel["title"], panel["source"], panel["height"]) == (
        "chart-1", "图一", "某个源", 430
    )


def test_table_chart_becomes_a_snapshot_panel_without_raw_option():
    panel = _chart_to_panel(
        1,
        {"id": "c2", "title": "表一", "source": "巨潮披露", "height": 120,
         "table": TABLE},
    )
    assert panel["type"] == "table"
    assert panel["snapshot"] == TABLE
    assert "raw_option" not in panel, "table 面板带 raw_option 会被当 echarts 配置渲染成空板"
    assert (panel["id"], panel["title"], panel["source"], panel["height"]) == (
        "chart-2", "表一", "巨潮披露", 120
    )


# ------------------------------------------------------------- 负例：必须硬失败


def test_neither_option_nor_table_raises():
    with pytest.raises(ValueError, match="既无 option 也无 table"):
        _chart_to_panel(0, {"id": "chart-x", "title": "忘了给形状"})


def test_both_option_and_table_raises_as_mutually_exclusive():
    with pytest.raises(ValueError, match="同时带 option 与 table，二者互斥"):
        _chart_to_panel(
            0,
            {"id": "chart-y", "option": {"series": []}, "table": TABLE},
        )


def test_empty_columns_raises_naming_the_id_and_field():
    with pytest.raises(ValueError, match="chart-z"):
        _chart_to_panel(0, {"id": "chart-z", "table": {"columns": [], "rows": []}})
    with pytest.raises(ValueError, match="table.columns"):
        _chart_to_panel(0, {"id": "chart-z", "table": {"columns": [], "rows": []}})


def test_non_list_rows_raises_naming_the_id_and_field():
    with pytest.raises(ValueError, match="table.rows"):
        _chart_to_panel(0, {"id": "chart-w", "table": {"columns": ["a"], "rows": {"a": 1}}})


# ------------------------------------------------ 回归：真实的 cnreport 板产物


def test_real_cnreport_spec_yields_two_table_panels():
    """用盘上真实的 ``cnreport-financials.spec.json`` 跑一遍转换。

    这份板是事故的当事人：两张都是 table 形状。只取 ``charts`` 数组，不碰数据库。
    """
    spec_path = (
        Path(__file__).resolve().parents[3] / "dashboards" / "cnreport-financials.spec.json"
    )
    # parents[3] 在源码树里是工作区根；在 CI 的快照仓里越界到别处——两者都 skip，
    # 这条用例只在本机 + 真工作区有意义（跨仓语料不得成为 CI 硬依赖）。
    if not spec_path.exists():
        pytest.skip("工作区 dashboards/cnreport-financials.spec.json 不在（CI 环境）")
    source = json.loads(spec_path.read_text(encoding="utf-8"))
    envelope = build_showcase_spec(
        {"title": source["title"], "note": source.get("note", ""),
         "charts": source["charts"]},
        "cnreport-financials",
    )
    panels = envelope["spec"]["panels"]
    assert len(panels) == 2, panels
    assert all(p["type"] == "table" for p in panels)
    assert all("raw_option" not in p for p in panels)
    assert all(p["snapshot"]["columns"] and p["snapshot"]["rows"] for p in panels)
