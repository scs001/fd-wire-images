#!/usr/bin/env python3
"""真 HTTP 端到端验收（wire-rich-text-docs 任务 7.1-7.4）。

与 pytest 的分工：pytest 用 TestClient（进程内 ASGI）验语义；本脚本起
**真 uvicorn 进程**、走**真 HTTP**、跑**真迁移**（alembic upgrade head），
覆盖三层 pytest 覆盖不到的东西：

1. 迁移链在真库上能从零 up 到 0038（含 artifact_drafts 表）；
2. 公开分享端点跨进程真网络可达（无鉴权面）；
3. 422/404/409 等错误形态经真实 HTTP 栈（不是异常被 TestClient 截住）。

鉴权：console 面（建文档/保存/草稿）用 dependency_overrides 换成本地身份
（与 pytest 同法）——`require_console_customer_id` 是登录面，不在本变更范围。
公开面不打桩，真令牌验签。

用法（api 目录下）：`.venv/bin/python scripts/e2e_doc.py`
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CUSTOMER = "cust-doc-e2e"
SHARE_SECRET = "e2e-secret"

# --serve 模式动态赋值（见 serve()）。
_TOKEN_FILE: Path | None = None

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, evidence: str = "") -> None:
    results.append((ok, name, evidence))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if evidence:
        for line in evidence.strip().splitlines():
            print(f"         {line}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# --serve 模式：真浏览器手验用（7.1/7.2 的浏览器面）。
#
# 与默认模式的差别只在**身份来源**：默认模式把登录面 override 成固定身份
#（curl 级验收足够）；--serve 必须让浏览器完整走 `#/console` 真流程——
# 于是打桩点在 JWKS 验签设施（pytest 同法 install_fake_jwks），而不是
# 登录依赖本身：浏览器拿到的是一把**真签名**的 ID token，establishSession
# 真跑、logto_sub 真映射、后续 Bearer 真解析。不绕过任何产品代码路径。
# ---------------------------------------------------------------------------

SERVE_CUSTOMER = "doc-e2e-browser"
SERVE_SUB = "sub-doc-browser"
FAKE_ISSUER = "https://auth.e2e.test"
FAKE_CLIENT_ID = "wire-spa-e2e"


def _serve_launcher_source(port: int) -> str:
    """--serve 的 uvicorn 启动子：假 JWKS + 种子客户 + 铸一把真 token。"""
    return f"""
import os, sys, time, json
sys.path.insert(0, {str(ROOT)!r})
sys.path.insert(0, {str(ROOT / 'tests')!r})
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt as pyjwt
from types import SimpleNamespace

# 1) 假 JWKS：进程内 RSA 指纹钥，PyJWKClient 打桩成测试公钥（签名仍真验）
_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_pub = _priv.public_key()

from fd_wire_api.console import auth as console_auth
console_auth._jwks_client = lambda url: SimpleNamespace(
    get_signing_key_from_jwt=lambda token: SimpleNamespace(key=_pub)
)

# 2) 种子客户（平台库 create_all + seed_plans + logto_sub 行；仿
#    tests/console_fixture.make_console_env 的最小化版本）
from fd_wire_api.admin.db import AdminBase, get_session, get_engine
from fd_wire_api.billing import Base as BillingBase, seed_plans
from fd_wire_api.billing.models import Customer, Plan
from fd_wire_api.console.models import ConsoleBase
from fd_wire_api.artifacts.models import Artifact, ArtifactVersion, ArtifactDraft  # noqa
AdminBase.metadata.create_all(get_engine())
BillingBase.metadata.create_all(get_engine())
ConsoleBase.metadata.create_all(get_engine())
with get_session() as sess:
    seed_plans(sess)
    exist = sess.query(Customer).filter(Customer.logto_sub == {SERVE_SUB!r}).one_or_none()
    if exist is None:
        free = sess.query(Plan).filter(Plan.code == 'free').one()
        sess.add(Customer(name={SERVE_CUSTOMER!r}, sub2api_user_id='caller-doc-e2e',
                          logto_sub={SERVE_SUB!r}, plan_id=free.id))
    sess.commit()

# 3) 铸一把真 token（签名可验；iss/aud 与 env 一致）
now = int(time.time())
token = pyjwt.encode({{
    'iss': {FAKE_ISSUER!r} + '/oidc', 'aud': {FAKE_CLIENT_ID!r},
    'sub': {SERVE_SUB!r}, 'roles': [],
    'iat': now, 'exp': now + 86400, 'email': 'doc-e2e@test',
}}, _priv, algorithm='RS256')
with open({str(_TOKEN_FILE)!r}, 'w') as f:
    f.write(token)
print('TOKEN_READY', flush=True)

# 4) 起 uvicorn（无任何 dependency override——浏览器面全真）
from fd_wire_api.app import app
import uvicorn
uvicorn.run(app, host='127.0.0.1', port={port}, log_level='warning')
"""


def serve() -> int:
    """--serve：起后端（假 JWKS + 种子客户），打印浏览器注入用的 token。

    前端另起 `vite`（代理 /api → 本后端），在浏览器里把 token 写进
    sessionStorage 即获得完整 console 会话（establishSession 真跑）。
    """
    global _TOKEN_FILE
    tmp = Path(tempfile.mkdtemp(prefix="fd-wire-doc-serve-"))
    meta_db = tmp / "meta.db"
    datasets_db = tmp / "datasets.db"
    port = free_port()
    _TOKEN_FILE = tmp / "id_token.txt"
    env = {
        **os.environ,
        "FD_WIRE_DATABASE_URL": f"sqlite:///{meta_db}",
        "FD_WIRE_DATASETS_DATABASE_URL": f"sqlite:///{datasets_db}",
        "FD_WIRE_SHARE_SECRET": SHARE_SECRET,
        "FD_WIRE_LOGTO_ISSUER": FAKE_ISSUER,
        "FD_WIRE_LOGTO_CLIENT_ID": FAKE_CLIENT_ID,
        "FD_WIRE_ADMIN_TOKEN": "e2e-admin-token",
        "PYTHONPATH": str(ROOT),
    }
    print(f"\n--serve 模式   临时库：{meta_db.parent}")
    mig = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180, check=False,
    )
    if mig.returncode != 0:
        print("迁移失败：", (mig.stderr or mig.stdout)[-800:])
        return 1
    launcher = tmp / "serve_launch.py"
    launcher.write_text(_serve_launcher_source(port))
    proc = subprocess.Popen(
        [sys.executable, str(launcher)], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    client = httpx.Client(trust_env=False, timeout=10, base_url=f"http://127.0.0.1:{port}")
    print(f"后端：http://127.0.0.1:{port}")
    print("等待就绪（ctrl-c 结束）…")
    try:
        for _ in range(120):
            if proc.poll() is not None:
                print("后端退出：", (proc.stdout.read() or "")[-1200:])
                return 1
            try:
                if client.get("/health").status_code == 200 and _TOKEN_FILE.exists():
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            print("就绪超时")
            return 1
        token = _TOKEN_FILE.read_text()
        print("\n=== 浏览器注入（DevTools Console，在 vite 页面执行一次）===")
        print(
            "sessionStorage.setItem('wire.console.session', JSON.stringify({"
            f"idToken: '{token}', expiresAt: Date.now() + 86400000, "
            "identity: 'doc-e2e@test', roles: []})); location.hash = '#/console/artifacts';"
        )
        print("\n=== 前端另开一个终端 ===")
        print(f"cd {ROOT.parent / 'web'} && npx vite --port 5199")
        print("\n后端日志（前 30 行）：")
        for _ in range(30):
            line = proc.stdout.readline()
            if not line:
                break
            print("  " + line.rstrip())
        print("\n进程保持中……")
        proc.wait()
    except KeyboardInterrupt:
        print("\n收尾：终止后端与清理临时目录")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


def main() -> int:
    if "--serve" in sys.argv[1:]:
        return serve()
    tmp = Path(tempfile.mkdtemp(prefix="fd-wire-doc-e2e-"))
    meta_db = tmp / "meta.db"
    datasets_db = tmp / "datasets.db"
    port = free_port()

    env = {
        **os.environ,
        "FD_WIRE_DATABASE_URL": f"sqlite:///{meta_db}",
        "FD_WIRE_DATASETS_DATABASE_URL": f"sqlite:///{datasets_db}",
        "FD_WIRE_SHARE_SECRET": SHARE_SECRET,
        "PYTHONPATH": str(ROOT),
    }

    print(f"\n临时库：{meta_db.name} / {datasets_db.name}   端口：{port}")

    # ---- 1. 真迁移：alembic upgrade head（任务 7.1 的迁移面）----
    print("\n1. 迁移链 upgrade head（0037 → 0038）")
    mig = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180, check=False,
    )
    check(mig.returncode == 0, "alembic upgrade head 成功",
          (mig.stdout or "").strip().splitlines()[-3:] and "\n".join((mig.stdout or "").strip().splitlines()[-3:]) or mig.stderr[-300:])
    # artifact_drafts 表存在（sqlite 直接查）
    import sqlite3
    con = sqlite3.connect(str(meta_db))
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    con.close()
    check("artifact_drafts" in tables, "迁移后 artifact_drafts 表存在",
          f"tables={sorted(t for t in tables if 'artifact' in t)}")

    # ---- 2. 起真 uvicorn（override 登录面为固定身份）----
    launcher = tmp / "launch.py"
    launcher.write_text(
        "import os\n"
        "os.environ.setdefault('PYTHONPATH', %r)\n" % str(ROOT)
        + "from fd_wire_api.app import app\n"
        "from fd_wire_api.ai.db import get_db as get_ai_db\n"
        "from fd_wire_api.billing.models import Customer\n"
        "from fd_wire_api.console.auth import require_console_customer_id\n"
        "from fd_wire_api.assets.routes import require_asset_editor\n"
        f"app.dependency_overrides[require_console_customer_id] = lambda: {CUSTOMER!r}\n"
        "# 市场可见性面走 require_asset_editor（真 JWKS 验签）；本脚本不打桩\n"
        "# 验签基础设施，按 pytest 同法替换为固定 (Customer, sub) 身份。\n"
        "def _fake_editor():\n"
        f"    return Customer(name={CUSTOMER!r}), 'sub-doc-e2e'\n"
        "app.dependency_overrides[require_asset_editor] = _fake_editor\n"
        "import uvicorn\n"
        f"uvicorn.run(app, host='127.0.0.1', port={port}, log_level='warning')\n"
    )
    proc = subprocess.Popen(
        [sys.executable, str(launcher)], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    client = httpx.Client(trust_env=False, timeout=30, base_url=f"http://127.0.0.1:{port}")

    try:
        up = False
        for _ in range(80):
            try:
                if client.get("/health").status_code == 200:
                    up = True
                    break
            except Exception:
                time.sleep(0.5)
        check(up, "真 uvicorn 进程就绪（/health 200）")
        if not up:
            return 1

        # ---------------------------------------------------- 7.1 主链
        print("\n2. 端到端主链：建文档 → 草稿自动保存 → 保存版本 → 发布 → 分享 → 归档失效")
        r = client.post("/api/v1/artifacts", json={"kind": "doc", "title": "E2E 报告"})
        check(r.status_code == 201, "建文档 201", f"{r.status_code} {r.text[:120]}")
        art = r.json()
        aid = art["id"]
        check(art["spec"]["spec"] == {"format": "markdown", "text": ""},
              "空白 spec 形状 = {format:markdown, text:''}", json.dumps(art["spec"]["spec"], ensure_ascii=False))

        draft_text = "# 标题\n\n**加粗**与[链接](https://example.com)。\n\n- 甲\n- 乙\n"
        r = client.put(f"/api/v1/artifacts/{aid}/draft", json={"text": draft_text})
        check(r.status_code == 200 and r.json()["text"] == draft_text, "草稿 PUT 覆盖写成功",
              f"{r.status_code}")
        r = client.get(f"/api/v1/artifacts/{aid}/draft")
        check(r.json()["text"] == draft_text, "草稿 GET 取回一致")

        # 版本链未动（D3 核心断言）
        r = client.get(f"/api/v1/artifacts/{aid}/versions")
        check(len(r.json()) == 1, "草稿写入不产生版本行（版本数仍为 1）", f"versions={len(r.json())}")

        # 保存版本
        r = client.put(f"/api/v1/artifacts/{aid}", json={
            "spec": {"title": "E2E 报告", "spec": {"format": "markdown", "text": draft_text}}})
        check(r.status_code == 200 and r.json()["version"] == 2, "保存版本 → v2", f"{r.status_code} v={r.json().get('version')}")

        # 发布 → 分享
        r = client.post(f"/api/v1/artifacts/{aid}/publish")
        check(r.status_code == 200 and r.json()["status"] == "published", "发布成功", r.json().get("status"))
        r = client.post(f"/api/v1/artifacts/{aid}/share", json={"ttl_days": 30})
        check(r.status_code == 200, "生成分享链接（doc 已放行）", f"{r.status_code} {r.text[:150]}")
        share = r.json()
        check("/share/doc/" in share["url"], "分享 URL 路径为 /share/doc/", share["url"][:80])

        # ------------------------------------------- 7.1 公开面真 HTTP
        print("\n3. 公开分享面（真 HTTP，无鉴权）")
        pub = httpx.Client(trust_env=False, timeout=30, base_url=f"http://127.0.0.1:{port}")
        r = pub.get(share["url"])
        check(r.status_code == 200, "匿名打开分享链接 200", f"{r.status_code}")
        payload = r.json()
        check(payload["spec"]["spec"]["text"] == draft_text, "返回钉死版本正文")
        check(payload["version"] == 2, "version=2（钉死）", f"v={payload['version']}")
        check(payload["expires_at"] is not None, "30 天档 expires_at 非空")

        # 改版不串版：保存 v3 后旧链接仍返回 v2
        r = client.put(f"/api/v1/artifacts/{aid}", json={
            "spec": {"title": "E2E 报告 v3", "spec": {"format": "markdown", "text": "# 改版后"}}})
        check(r.status_code == 200 and r.json()["version"] == 3, "保存 v3", f"v={r.json().get('version')}")
        r = pub.get(share["url"])
        check(r.json()["spec"]["spec"]["text"] == draft_text, "旧链接仍呈现 v2（改版不串版）")

        # 归档 → 链接失效
        r = client.post(f"/api/v1/artifacts/{aid}/archive")
        check(r.status_code == 200 and r.json()["status"] == "archived", "归档成功")
        r = pub.get(share["url"])
        check(r.status_code == 404 and r.json()["detail"] == "not found", "归档后链接 404（同文案不泄漏）",
              f"{r.status_code} {r.json().get('detail')}")

        # ------------------------------------------------------ 7.4
        print("\n4. 正文上限与格式校验（真 HTTP 422 形态）")
        r = client.post("/api/v1/artifacts", json={"kind": "doc", "title": "校验用例"})
        aid2 = r.json()["id"]
        r = client.put(f"/api/v1/artifacts/{aid2}", json={
            "spec": {"title": "x", "spec": {"format": "html", "text": "<p>x</p>"}}})
        check(r.status_code == 422 and "markdown" in r.json()["detail"], "format 非 markdown → 422 且指名",
              f"{r.status_code} {r.json().get('detail')}")
        r = client.put(f"/api/v1/artifacts/{aid2}", json={
            "spec": {"title": "x", "spec": {"format": "markdown", "text": 123}}})
        check(r.status_code == 422, "text 非字符串 → 422", r.json().get("detail"))
        r = client.get(f"/api/v1/artifacts/{aid2}/versions")
        check(len(r.json()) == 1, "两次非法保存零半写（版本数未增）", f"versions={len(r.json())}")
        # 超限（临时把上限调小不便——用环境变量启动的进程读的是启动时 env；
        # 直接构造超大文本按缺省 256KB 判）
        big = "字" * (300 * 1024)  # 300K 个中文字 ≈ 900KB UTF-8，超 256KB
        r = client.put(f"/api/v1/artifacts/{aid2}/draft", json={"text": big})
        check(r.status_code == 422 and "上限" in r.json()["detail"], "草稿超上限 → 422 载明上限",
              f"{r.status_code} {(r.json().get('detail') or '')[:80]}")

        # ------------------------------------------------------ 7.2
        print("\n5. XSS 底线（数据面：正文原样存取，渲染安全由前端渲染器保证）")
        evil = "<script>alert(1)</script>\n\n[点](javascript:alert(2))\n\n![x](data:text/html;base64,PHNjcmlwdD4=)"
        r = client.put(f"/api/v1/artifacts/{aid2}", json={
            "spec": {"title": "xss", "spec": {"format": "markdown", "text": evil}}})
        check(r.status_code == 200, "含危险串的正文可保存（存的是源文本不是 HTML）", f"{r.status_code}")
        r = client.post(f"/api/v1/artifacts/{aid2}/publish")
        r = client.post(f"/api/v1/artifacts/{aid2}/share", json={"ttl_days": 7})
        r = pub.get(r.json()["url"])
        got = r.json()["spec"]["spec"]["text"]
        check(got == evil, "公开面原样返回源文本（服务端零渲染=零注入面）",
              f"含 <script>={('<script>' in got)} 含 javascript:={'javascript:' in got}")

        # ------------------------------------------------------ 7.3
        print("\n6. 存量零漂移（既有 kind 路径抽验）")
        r = client.post("/api/v1/artifacts", json={"kind": "dashboard", "title": "回归看板"})
        check(r.status_code == 201 and r.json()["spec"]["spec"] == {"title": "回归看板", "panels": []},
              "看板空白创建行为不变", json.dumps(r.json()["spec"]["spec"], ensure_ascii=False))
        did = r.json()["id"]
        r = client.post(f"/api/v1/artifacts/{did}/publish")
        check(r.status_code == 200, "看板发布不变")
        r = client.post(f"/api/v1/artifacts/{did}/share", json={"ttl_days": 30})
        check(r.status_code == 200 and "/share/dashboard/" in r.json()["url"],
              "看板分享链接路径不变", r.json().get("url", "")[:60])
        r = pub.get(r.json()["url"])
        check(r.status_code == 200, "看板公开页仍可打开")
        # doc 不进市场：设 market 可见性 → 422（MARKETABLE_KINDS 闸门点名拒绝）
        r = client.put(f"/api/v1/artifacts/{aid2}/visibility", json={"visibility": "market"})
        ok = r.status_code == 422 and "doc" in (r.text or "")
        check(ok, "doc 进市场被拒（D5 显式排除，422 点名 kind）",
              f"{r.status_code} {(r.text or '')[:120]}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        client.close()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _, _ in results if ok)
    print(f"\n{'=' * 62}\n真 HTTP 端到端：{passed}/{len(results)} 通过\n{'=' * 62}")
    for ok, name, _ in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
