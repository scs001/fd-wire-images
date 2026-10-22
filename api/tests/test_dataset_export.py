"""数据集 CSV 导出端点测试（wire-dataset-export 任务 1.2）。

``GET /api/v1/datasets/{id}/export``（console 身份）：有效视图 CSV、
UTF-8 带 BOM、RFC4180 转义、CRLF 行尾、文件名 = key.csv。覆盖：

- api 档全量导出（免费档）：含修正的有效视图（修正后值入 CSV、删除行
  剔除、插入行在场）、BOM/表头/空值空串/数值原样、行尾 CRLF、
  Content-Disposition 文件名、全链路零引擎调用；
- cell 档引擎 query 分页循环：3 页拼装（offset 0/1000/2000）不截断；
  第 10 页仍满页 → 恰好 10,000 行 + ``X-Export-Truncated: true`` 如实标注；
- 转义边界（逗号/引号/换行/None）——csv.reader 往返可还原；
- 他企 404（不泄露存在性）；未登录 401。
"""

from __future__ import annotations

import csv
import io
import os

os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

from test_datasets_common import (  # noqa: E402
    CUST_HEADERS,
    FakeEngine,
    datasets_env,
    make_uploaded_dataset,
)
from test_datasets_free_tier import FREE, free_env, make_free_dataset  # noqa: E402

from fd_wire_api.datasets.ingest import TOOL_QUERY, get_engine_client_factory  # noqa: E402

#: fixture 注册标记（pytest 按参数名解析 fixture；模块级绑定引用使其成为
#: 已使用导入——同 test_local_corrections 手法）
FREE_ENV_FIXTURE = free_env
DATASETS_ENV_FIXTURE = datasets_env


def _export(env, dataset_id: str, *, customer: str = "cust-001"):
    return env.client.get(
        f"/api/v1/datasets/{dataset_id}/export", headers=CUST_HEADERS[customer]
    )


def _post_correction(env, dataset_id: str, payload: dict, *, customer: str = FREE):
    return env.client.post(
        f"/api/v1/datasets/{dataset_id}/corrections",
        headers=CUST_HEADERS[customer],
        json=payload,
    )


class PagedEngine(FakeEngine):
    """按 offset/limit 切片返回分页 query 结果的引擎假件（导出分页循环用）。"""

    def __init__(self, total_rows: int) -> None:
        super().__init__()
        self.total_rows = total_rows

    def _result(self, tool: str, arguments: dict) -> dict:
        if tool == TOOL_QUERY:
            offset, limit = int(arguments["offset"]), int(arguments["limit"])
            end = min(offset + limit, self.total_rows)
            return {
                "columns": [
                    {"name": "id", "type": "INTEGER"},
                    {"name": "name", "type": "TEXT"},
                ],
                "rows": [
                    {"id": i, "name": f"row-{i}", "__row_id": f"pk:{i}"}
                    for i in range(offset, end)
                ],
                "total": self.total_rows,
                "limit": limit,
                "offset": offset,
                "truncated": self.total_rows > end,
            }
        return super()._result(tool, arguments)


def _wire_engine(env, engine) -> None:
    """把指定引擎假件接进工厂覆盖（datasets_env 装的是默认 FakeEngine）。"""
    env.app.dependency_overrides[get_engine_client_factory] = lambda: (
        lambda customer_id: engine
    )


def _parse_csv(text: str) -> list[list[str]]:
    """剥 BOM 后按 RFC4180 解析（带引号字段可跨行还原）。"""
    return list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))


# ------------------------------------------------------------------ api 档全量


def test_export_api_tier_effective_view_full(free_env) -> None:
    """api 档全量导出：含修正的有效视图（update 改值/delete 剔除/insert 追加）、
    BOM 紧贴表头、空值空串、数值原样 str、行尾 CRLF、文件名=key.csv、零引擎。"""
    dataset = make_free_dataset(free_env)  # key = slugify("Free Data") = free_data
    dataset_id = dataset["id"]
    updated = _post_correction(
        free_env, dataset_id, {"op": "update", "row_id": "0", "column": "note", "value": "fixed-note"}
    )
    assert updated.status_code == 201, updated.text
    deleted = _post_correction(free_env, dataset_id, {"op": "delete", "row_id": "2"})
    assert deleted.status_code == 201, deleted.text
    inserted = _post_correction(
        free_env,
        dataset_id,
        {"op": "insert", "values": {"region": "new", "amount": 1.5, "qty": "9", "note": "n"}},
    )
    assert inserted.status_code == 201, inserted.text

    resp = _export(free_env, dataset_id, customer=FREE)

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    assert resp.headers["content-disposition"] == 'attachment; filename="free_data.csv"'
    assert "x-export-truncated" not in resp.headers  # 免费档无截断语义
    # 有效视图全量：基行 − 删除 + 插入（row_index 序，插入行按落层序追加）
    assert resp.text == (
        "\ufeffregion,amount,qty,note\r\n"
        "east,10.5,1,fixed-note\r\n"  # 修正后值入 CSV（基值 hello 不再呈现）
        "west,20.75,2,\r\n"  # 空值 → 空串；REAL/INTEGER 原样 str()
        "north,5.25,4,hi\r\n"
        "new,1.5,9,n\r\n"  # 插入行（qty 已按 INTEGER 归一）
    )
    # 全链路零引擎调用（免费档导出不过 EngineToolClient）
    assert free_env.fake.calls == []


def test_export_api_tier_cross_tenant_404(free_env) -> None:
    """他企数据集导出一律 404（不泄露存在性）。"""
    dataset = make_free_dataset(free_env)
    resp = _export(free_env, dataset["id"], customer="cust-002")
    assert resp.status_code == 404
    assert free_env.fake.calls == []


def test_export_requires_auth(datasets_env) -> None:
    """缺 Bearer ID token 一律 401。"""
    assert datasets_env.client.get("/api/v1/datasets/any/export").status_code == 401


# ------------------------------------------------------------------ cell 档分页循环


def test_export_cell_tier_three_pages_assembled(datasets_env) -> None:
    """cell 档分页循环：2500 行 → 3 页（offset 0/1000/2000，每页 limit 1000）
    拼装，短页见底不截断；引擎调用形参如实（filters/sort 空 + dataset_key）。"""
    dataset = make_uploaded_dataset(datasets_env, name="Cell Data")  # cell 档（basic 种子）
    paged = PagedEngine(total_rows=2500)
    _wire_engine(datasets_env, paged)

    resp = _export(datasets_env, dataset["id"])

    assert resp.status_code == 200, resp.text
    assert "x-export-truncated" not in resp.headers
    # 引擎分页循环如实：3 次调用、offset 递增 1000/页、limit 恒 1000
    assert paged.calls == [
        (
            TOOL_QUERY,
            {
                "dataset_key": dataset["key"],
                "filters": [],
                "sort": [],
                "limit": 1000,
                "offset": page * 1000,
            },
        )
        for page in range(3)
    ]
    rows = _parse_csv(resp.text)
    assert rows[0] == ["id", "name"]  # 表头 = 列名行（__row_id 是合成锚不导出）
    assert len(rows) == 2501  # 表头 + 2500 行
    assert rows[1] == ["0", "row-0"] and rows[-1] == ["2499", "row-2499"]
    assert resp.text.endswith("\r\n")


def test_export_cell_tier_cap_10000_truncated_header(datasets_env) -> None:
    """cell 档 10k 上限：15,000 行源 → 前 10 页（10,000 行）+ 第 10 页满页
    触顶 ``X-Export-Truncated: true``（不静默丢行、不谎报全量）。"""
    dataset = make_uploaded_dataset(datasets_env, name="Big Cell")
    paged = PagedEngine(total_rows=15000)
    _wire_engine(datasets_env, paged)

    resp = _export(datasets_env, dataset["id"])

    assert resp.status_code == 200, resp.text
    assert resp.headers["x-export-truncated"] == "true"
    assert len(paged.calls) == 10  # 10 页即止（不多探一页）
    assert [call[1]["offset"] for call in paged.calls] == [page * 1000 for page in range(10)]
    rows = _parse_csv(resp.text)
    assert len(rows) == 10001
    assert rows[-1] == ["9999", "row-9999"]  # 恰好前 10,000 行


# ------------------------------------------------------------------ 转义边界


def test_export_csv_escaping_boundaries(datasets_env) -> None:
    """RFC4180 转义：含逗号/引号/换行的字段双引号包裹且内部引号翻倍、
    空值空串、纯文本原样——csv.reader 往返可还原。"""
    dataset = make_uploaded_dataset(datasets_env, name="Tricky")
    datasets_env.fake.query_result = {
        "columns": [
            {"name": "id", "type": "INTEGER"},
            {"name": "note", "type": "TEXT"},
        ],
        "rows": [
            {"id": 1, "note": 'a,b"c\nd', "__row_id": "pk:1"},
            {"id": 2, "note": None, "__row_id": "pk:2"},
            {"id": 3, "note": "plain text", "__row_id": "pk:3"},
            {"id": 4, "note": 'quote"only', "__row_id": "pk:4"},
        ],
        "total": 4,
    }

    resp = _export(datasets_env, dataset["id"])

    assert resp.status_code == 200, resp.text
    assert resp.text == (
        "\ufeffid,note\r\n"
        '1,"a,b""c\nd"\r\n'  # 逗号+引号+换行 → 包裹 + 引号翻倍（字段跨行）
        "2,\r\n"  # None → 空串
        "3,plain text\r\n"  # 无特殊字符 → 原样
        '4,"quote""only"\r\n'  # 仅含引号 → 同样包裹翻倍
    )
    # 往返还原（带引号字段跨行不丢）
    assert _parse_csv(resp.text) == [
        ["id", "note"],
        ["1", 'a,b"c\nd'],
        ["2", ""],
        ["3", "plain text"],
        ["4", 'quote"only'],
    ]
