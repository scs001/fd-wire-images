"""wire-rich-text-docs 后端核心测试（任务 1.1-1.3 / 2.1-2.3 后端侧）。

覆盖：
- doc kind 契约：空白创建最小 spec（``{format:"markdown", text:""}``）、
  白名单拒收（文案含 doc）、保存校验矩阵（缺 format / 非 markdown /
  text 非字符串 / 超限——全部 422 且**零半写**：版本行不增）；
- 版本链：合法保存两次版本递增、两版本均可取回；archived 拒改 409；
- 草稿层（design D3）：PUT/GET 往返、覆盖写、**版本链零新增**（核心断言）、
  超限 422 零半写、archived 409、跨企 404、非 doc kind 422；
- 草稿表级联：产物删除 → 草稿行随删（SQLite 显式开 ``PRAGMA
  foreign_keys=ON``——默认关，不开则 CASCADE 是死条文）；
- 校验面收口：``doc`` 不进 AI 门（ARTIFACT_TOOL_KINDS）与
  models.ARTIFACT_KINDS（五类口径零漂移）。

环境：照 test_record_view 先例（ai 与 datasets 同引擎合库、console 身份
假 JWKS）。``FD_WIRE_DOC_MAX_BYTES`` 由用例按需钉小值（上限校验读 env
实时值，不缓存——钉小值即可免造 256KB 正文）。
"""

from __future__ import annotations

import os

# 在导入 app 前把默认引擎指向内存库（TestClient 启动钩子不落盘）。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db, reset_ai_runtime
from fd_wire_api.ai.tools import ARTIFACT_TOOL_KINDS
from fd_wire_api.app import app
from fd_wire_api.artifacts.models import ARTIFACT_KINDS, ArtifactDraft, ArtifactVersion
from fd_wire_api.datasets.db import (
    DatasetsBase,
    get_db as get_datasets_db,
)

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

SUB_CUST = "sub-doc-001"
SUB_OTHER = "sub-doc-002"
CUST = bearer(mint_id_token(SUB_CUST))
OTHER = bearer(mint_id_token(SUB_OTHER))

TITLE = "季度报告"

VALID_MD = "# 标题\n\n正文段落，含 **粗体** 与 [链接](https://example.com)。\n"


@pytest.fixture()
def db_session():
    """独立内存库（AiBase 全表——artifacts 两表 + artifact_drafts）的会话。

    ``PRAGMA foreign_keys=ON``：SQLite 缺省**不**执行外键动作——不开则
    ON DELETE CASCADE 是死条文，级联用例会假绿（本仓首个依赖 FK 动作的
    表，故在测试引擎显式打开；生产 PG 无此问题）。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _connection_record):  # pragma: no cover - 驱动钩子
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    AiBase.metadata.create_all(bind=engine)
    DatasetsBase.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(monkeypatch, tmp_path, db_session):
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-001/mcp")
    # 上限用例按需钉小值；缺省不设 = 走 256KB 缺省（缺省路径也被本文件覆盖）
    monkeypatch.delenv("FD_WIRE_DOC_MAX_BYTES", raising=False)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    reset_ai_runtime()
    app.dependency_overrides[get_ai_db] = override_get_db
    app.dependency_overrides[get_datasets_db] = override_get_db
    install_fake_jwks(monkeypatch)
    make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_CUST, "cust-001"), (SUB_OTHER, "cust-002")],
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    reset_ai_runtime()


# ------------------------------------------------------------------ 流程助手


def make_doc(client, *, title: str = TITLE, headers=CUST) -> dict:
    """空白建 doc → 返回详情体（出生 v1 = 空正文最小 spec）。"""
    resp = client.post(
        "/api/v1/artifacts", json={"kind": "doc", "title": title}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _version_count(db_session, artifact_id: int) -> int:
    """版本行数（零半写断言用——走真表 count，不信产物 version 计数）。"""
    return len(
        list(
            db_session.execute(
                ArtifactVersion.__table__.select().where(
                    ArtifactVersion.artifact_id == artifact_id
                )
            )
        )
    )


def _draft_rows(db_session, artifact_id: int) -> list:
    return list(
        db_session.execute(
            ArtifactDraft.__table__.select().where(
                ArtifactDraft.artifact_id == artifact_id
            )
        )
    )


def _put_spec(client, artifact_id: int, inner, *, title: str = TITLE, headers=CUST):
    return client.put(
        f"/api/v1/artifacts/{artifact_id}",
        json={"spec": {"title": title, "spec": inner}},
        headers=headers,
    )


# ------------------------------------------------- 1.1-1.3 doc kind 与空白创建


def test_blank_create_doc(client, db_session) -> None:
    """空白创建 doc（任务 1.1/1.3）：201 draft v1 = 空正文最小 spec；
    版本行 v1 与初始状态留痕同在。"""
    resp = client.post(
        "/api/v1/artifacts", json={"kind": "doc", "title": TITLE}, headers=CUST
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "doc"
    assert body["status"] == "draft"
    assert body["version"] == 1
    # 最小 spec：format 恒 markdown、text 空串（BLANK_DOC_INNER 的形状）
    assert body["spec"] == {"title": TITLE, "spec": {"format": "markdown", "text": ""}}

    # v1 版本行在位（出生即接既有编辑/版本语义）
    versions = client.get(
        f"/api/v1/artifacts/{body['id']}/versions", headers=CUST
    ).json()
    assert [v["version"] for v in versions] == [1]
    assert versions[0]["operator"] == "console:cust-001"
    detail = client.get(
        f"/api/v1/artifacts/{body['id']}/versions/1", headers=CUST
    ).json()
    assert detail["spec"]["spec"] == {"format": "markdown", "text": ""}

    # 初始状态留痕（同既有出生路径口径）
    assert body["status_history"][0]["status"] == "draft"
    assert "空白创建" in body["status_history"][0]["note"]

    # 他企不可见
    assert client.get("/api/v1/artifacts", headers=OTHER).json() == []


def test_blank_create_kind_whitelist_still_422(client) -> None:
    """白名单外 kind 仍 422（任务 1.3 验证面）：拒收文案已含 doc，
    且零半写（不落任何产物行）。"""
    resp = client.post(
        "/api/v1/artifacts",
        json={"kind": "research_report", "title": "报告"},
        headers=CUST,
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "doc" in detail
    assert (
        detail
        == "空白创建仅支持 dashboard、form、alert_rule、record-view、doc"
        "（其他 kind 由 AI 构建产生）"
    )
    # 拒收零半写（doc 之外仍无产物）
    assert client.get("/api/v1/artifacts", headers=CUST).json() == []


# ------------------------------------------------- 1.2 doc spec 保存校验矩阵


def test_doc_spec_validation_matrix(client, db_session, monkeypatch) -> None:
    """doc spec 校验矩阵（任务 1.2）：缺 format / format≠markdown /
    text 非字符串 / text 超限——全部 422 且零半写（版本行不增）。"""
    artifact = make_doc(client)
    aid = artifact["id"]
    assert _version_count(db_session, aid) == 1

    cases = [
        ({"text": "x"}, "format"),  # 缺 format
        ({"format": "html", "text": "x"}, "markdown"),  # 非 markdown
        ({"format": "markdown", "text": 123}, "字符串"),  # text 非字符串
        ({"format": "markdown"}, "字符串"),  # text 缺失（同上口径）
    ]
    for inner, keyword in cases:
        resp = _put_spec(client, aid, inner)
        assert resp.status_code == 422, (inner, resp.text)
        assert keyword in resp.json()["detail"], (inner, resp.json())
        # 零半写：每次拒绝后版本数不变
        assert _version_count(db_session, aid) == 1, inner

    # 超限（钉小上限免造 256KB 正文）：422 载明上限字节数
    monkeypatch.setenv("FD_WIRE_DOC_MAX_BYTES", "32")
    oversize = "字" * 20  # UTF-8 下 20 字 = 60 字节 > 32
    resp = _put_spec(client, aid, {"format": "markdown", "text": oversize})
    assert resp.status_code == 422
    assert "32" in resp.json()["detail"]
    assert _version_count(db_session, aid) == 1

    # 上限内的合法保存仍可（小上限下刚过线）
    ok = "字" * 10  # 30 字节 ≤ 32
    saved = _put_spec(client, aid, {"format": "markdown", "text": ok})
    assert saved.status_code == 200, saved.text
    assert _version_count(db_session, aid) == 2


def test_doc_spec_valid_save_appends_version(client, db_session) -> None:
    """合法保存两次：版本递增、两版本均可取回（append-only 语义对 doc
    逐字沿用）。"""
    artifact = make_doc(client)
    aid = artifact["id"]

    first = _put_spec(client, aid, {"format": "markdown", "text": "v2 正文"})
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 2

    second = _put_spec(
        client, aid, {"format": "markdown", "text": VALID_MD}, title="季度报告（修订）"
    )
    assert second.status_code == 200, second.text
    assert second.json()["version"] == 3
    # title 随 spec.title 投影同步（既有口径）
    assert second.json()["title"] == "季度报告（修订）"

    assert _version_count(db_session, aid) == 3
    v2 = client.get(f"/api/v1/artifacts/{aid}/versions/2", headers=CUST).json()
    v3 = client.get(f"/api/v1/artifacts/{aid}/versions/3", headers=CUST).json()
    assert v2["spec"]["spec"]["text"] == "v2 正文"
    assert v3["spec"]["spec"]["text"] == VALID_MD
    # 历史版本不可改写（append-only 未被 doc 破坏）
    assert v2["spec"]["spec"]["text"] != v3["spec"]["spec"]["text"]


def test_doc_archived_save_rejected(client, db_session) -> None:
    """archived 产物拒改（任务 1.2 语义边界）：PUT 409 提示先恢复，版本链零增。"""
    artifact = make_doc(client)
    aid = artifact["id"]
    archived = client.post(f"/api/v1/artifacts/{aid}/archive", headers=CUST)
    assert archived.status_code == 200, archived.text

    resp = _put_spec(client, aid, {"format": "markdown", "text": "归档后写入"})
    assert resp.status_code == 409
    assert "恢复" in resp.json()["detail"]
    assert _version_count(db_session, aid) == 1

    # 恢复后可继续编辑（唯一回退边对 doc 同样成立）
    client.post(f"/api/v1/artifacts/{aid}/restore", headers=CUST)
    again = _put_spec(client, aid, {"format": "markdown", "text": "恢复后保存"})
    assert again.status_code == 200, again.text
    assert _version_count(db_session, aid) == 2


# ----------------------------------------------------- 2.1-2.2 草稿层读写


def test_doc_draft_put_get_roundtrip(client, db_session) -> None:
    """草稿层 PUT/GET 往返（核心 D3 断言）：覆盖写、**版本链零新增**。

    连 PUT 三次是「自动保存」的真实形态——版本链必须纹丝不动（一次也不
    增），否则敲字期间版本被炸掉（design D3 的立论）。"""
    artifact = make_doc(client)
    aid = artifact["id"]

    # 无草稿行：GET 双 null（前端据此回退当前版本 spec.text）
    initial = client.get(f"/api/v1/artifacts/{aid}/draft", headers=CUST)
    assert initial.status_code == 200, initial.text
    assert initial.json() == {"text": None, "updated_at": None}

    for payload in ("第一稿", "第二稿\n\n加一段。", VALID_MD):
        put = client.put(
            f"/api/v1/artifacts/{aid}/draft", json={"text": payload}, headers=CUST
        )
        assert put.status_code == 200, put.text
        assert put.json()["text"] == payload
        assert isinstance(put.json()["updated_at"], str)

    # 覆盖写不是追加：草稿行恒一行
    assert len(_draft_rows(db_session, aid)) == 1
    # **核心断言**：三次自动保存，版本链零新增
    assert _version_count(db_session, aid) == 1
    assert client.get(f"/api/v1/artifacts/{aid}", headers=CUST).json()["version"] == 1

    # GET 取回最新（刷新页面不丢）
    got = client.get(f"/api/v1/artifacts/{aid}/draft", headers=CUST)
    assert got.status_code == 200, got.text
    assert got.json()["text"] == VALID_MD
    assert isinstance(got.json()["updated_at"], str)

    # 显式「保存版本」才进版本链（草稿层与版本链分工的对照面）
    saved = _put_spec(client, aid, {"format": "markdown", "text": VALID_MD})
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2
    assert _version_count(db_session, aid) == 2

    # 保存版本后草稿层原样保留（工作区不清空——与版本链各自独立事务）
    assert client.get(f"/api/v1/artifacts/{aid}/draft", headers=CUST).json()["text"] == VALID_MD


def test_doc_draft_oversize_422(client, db_session, monkeypatch) -> None:
    """草稿超限 422（任务 2.2：限长复用 1.2 同一校验器）且零半写——
    已存草稿不被半更，无草稿时不落行。"""
    artifact = make_doc(client)
    aid = artifact["id"]
    monkeypatch.setenv("FD_WIRE_DOC_MAX_BYTES", "32")

    # 无草稿行：超限拒收后仍无行（不是「先建后校验」）
    too_long = "字" * 20  # 60 字节 > 32
    resp = client.put(
        f"/api/v1/artifacts/{aid}/draft", json={"text": too_long}, headers=CUST
    )
    assert resp.status_code == 422
    assert "32" in resp.json()["detail"]
    assert _draft_rows(db_session, aid) == []

    # 超限拒绝不动已存草稿
    monkeypatch.delenv("FD_WIRE_DOC_MAX_BYTES", raising=False)
    ok = client.put(
        f"/api/v1/artifacts/{aid}/draft", json={"text": "留在草稿里的正文"}, headers=CUST
    )
    assert ok.status_code == 200, ok.text
    monkeypatch.setenv("FD_WIRE_DOC_MAX_BYTES", "32")
    rejected = client.put(
        f"/api/v1/artifacts/{aid}/draft", json={"text": too_long}, headers=CUST
    )
    assert rejected.status_code == 422
    rows = _draft_rows(db_session, aid)
    assert len(rows) == 1 and rows[0].text == "留在草稿里的正文"
    # 草稿写不进版本链（拒绝路径同样不产生版本行）
    assert _version_count(db_session, aid) == 1


def test_doc_draft_archived_409(client) -> None:
    """archived 拒写草稿：409（ArtifactLocked 映射，与 update_artifact 同款
    ——归档是只读位）。读路径不拦（归档文档仍可查看）。"""
    artifact = make_doc(client)
    aid = artifact["id"]
    before = client.put(
        f"/api/v1/artifacts/{aid}/draft", json={"text": "归档前草稿"}, headers=CUST
    )
    assert before.status_code == 200, before.text

    client.post(f"/api/v1/artifacts/{aid}/archive", headers=CUST)
    resp = client.put(
        f"/api/v1/artifacts/{aid}/draft", json={"text": "归档后写入"}, headers=CUST
    )
    assert resp.status_code == 409
    assert "归档" in resp.json()["detail"]

    # 读仍可（只读面不设门）；内容未被覆写
    got = client.get(f"/api/v1/artifacts/{aid}/draft", headers=CUST)
    assert got.status_code == 200
    assert got.json()["text"] == "归档前草稿"

    # 恢复后可写（只读位解除）
    client.post(f"/api/v1/artifacts/{aid}/restore", headers=CUST)
    again = client.put(
        f"/api/v1/artifacts/{aid}/draft", json={"text": "恢复后草稿"}, headers=CUST
    )
    assert again.status_code == 200, again.text


def test_doc_draft_cross_tenant_404(client) -> None:
    """跨企草稿面一律 404（不泄露他企存在性）——读与写两路同款。"""
    artifact = make_doc(client)
    aid = artifact["id"]
    assert (
        client.get(f"/api/v1/artifacts/{aid}/draft", headers=OTHER).status_code == 404
    )
    assert (
        client.put(
            f"/api/v1/artifacts/{aid}/draft", json={"text": "越权写入"}, headers=OTHER
        ).status_code
        == 404
    )
    # 本企业读不到越权写入的痕迹
    assert client.get(f"/api/v1/artifacts/{aid}/draft", headers=CUST).json() == {
        "text": None,
        "updated_at": None,
    }
    # 不存在的产物同款 404
    assert client.get("/api/v1/artifacts/99999/draft", headers=CUST).status_code == 404


def test_doc_draft_rejects_non_doc_kind(client, db_session) -> None:
    """kind≠doc：读写两路均 422（草稿层只服务正文型产物），且零半写。"""
    board = client.post(
        "/api/v1/artifacts", json={"kind": "dashboard", "title": "看板"}, headers=CUST
    ).json()
    bid = board["id"]

    assert client.get(f"/api/v1/artifacts/{bid}/draft", headers=CUST).status_code == 422
    put = client.put(
        f"/api/v1/artifacts/{bid}/draft", json={"text": "看板不能存草稿"}, headers=CUST
    )
    assert put.status_code == 422
    assert "doc" in put.json()["detail"]
    # 零半写：看板下无草稿行
    assert _draft_rows(db_session, bid) == []


# ------------------------------------------------------------- 级联与外延


def test_artifact_drafts_cascade_delete(db_session) -> None:
    """草稿行随产物删除（任务 2.1：唯一外键 ``ON DELETE CASCADE``）。

    用裸 SQL 造一条**无版本行**的产物再删：版本链外键无级联（历史留痕
    不随产物删——那是有意为之），走 ORM 删会发生版本行的 FK 冲突，测不到
    草稿表的级联语义。本用例只针对 ``artifact_drafts`` 的删除动作。
    """
    db_session.execute(
        text(
            "INSERT INTO artifacts (customer_id, kind, title, status, version,"
            " status_history, visibility, access_mode, allow_copy, created_at,"
            " updated_at) VALUES ('cust-001', 'doc', '待删文档', 'draft', 1,"
            " '[]', 'private', 'open', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )
    artifact_id = db_session.execute(
        text("SELECT id FROM artifacts WHERE title = '待删文档'")
    ).scalar()
    db_session.execute(
        text(
            "INSERT INTO artifact_drafts (artifact_id, text, updated_at, updated_by)"
            " VALUES (:aid, '草稿正文', CURRENT_TIMESTAMP, 'console:cust-001')"
        ),
        {"aid": artifact_id},
    )
    db_session.commit()

    # 前置：外键动作真开着（否则本用例是死条文）
    assert db_session.execute(text("PRAGMA foreign_keys")).scalar() == 1
    assert len(_draft_rows(db_session, artifact_id)) == 1

    db_session.execute(text("DELETE FROM artifacts WHERE id = :aid"), {"aid": artifact_id})
    db_session.commit()
    assert _draft_rows(db_session, artifact_id) == []


def test_doc_not_in_ai_tool_kinds() -> None:
    """校验面收口（任务 1.1/1.3）：doc 不进 AI 门与 models.ARTIFACT_KINDS
    ——doc 是 console 直建的正文型产物，不是 AI 五类工具产物（同 form/
    record-view 的 service 层常量路线）。"""
    assert "doc" not in set(ARTIFACT_TOOL_KINDS.values())
    assert "doc" not in set(ARTIFACT_KINDS)
    # 五类校验面零漂移（既有断言面不动）
    assert set(ARTIFACT_KINDS) == set(ARTIFACT_TOOL_KINDS.values())
