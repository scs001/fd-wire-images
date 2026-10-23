#!/usr/bin/env python3
"""wire-copilot-action-gate 6.2 真机验收（生产）。

用三个 wire-tester 账号的真实 ID token 打生产 API：
- free：问 + 读 + 订阅提议（async）+ 构建类被如实拦 + 审计检索；
- standard：描述看板 → 预览（草稿 spec）→ 确认 → 草稿 → 转正 → 发布
  → 分享进审批 → owner 批准 → 分享链接可匿名打开；
- 全程审计无凭据明文。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE = "https://wire.finddatatech.cloud/api"
TOKENS = Path("/tmp/wire-accept")
OUT = {"steps": [], "failures": []}


def load(name: str) -> str:
    data = json.loads((TOKENS / f"session-{name}.json").read_text())
    return data["idToken"]


def client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(180.0, connect=15.0),
    )


def rec(name: str, ok: bool, detail: object) -> None:
    OUT["steps"].append({"name": name, "ok": ok, "detail": detail})
    if not ok:
        OUT["failures"].append({"name": name, "detail": detail})
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}: {json.dumps(detail, ensure_ascii=False)[:600]}")


def body(resp: httpx.Response) -> object:
    try:
        return resp.json()
    except Exception:
        return resp.text[:1000]


def propose(c: httpx.Client, action_id: str, args: dict, conversation_id: int):
    return c.post(
        "/v1/ai/actions/propose",
        json={"action_id": action_id, "args": args, "conversation_id": conversation_id},
    )


def approve(c: httpx.Client, pid: int, payload: dict | None = None):
    return c.post(f"/v1/ai/actions/{pid}/approve", json=payload or {})


def free_tier() -> None:
    print("\n=== free 档（wire-tester1）===")
    c = client(load("tester1"))
    conv = c.post("/v1/ai/conversations", json={"message": None})
    rec("free: 建会话", conv.status_code == 201, {"status": conv.status_code})
    cid = body(conv)["id"]

    # 问：问答轨不依赖引擎（上游 token 网关偶发 503 时重试，仍失败则如实记录）
    t0 = time.time()
    ans = c.post("/v1/ai/answer", json={"content": "用一句话说明柏讯能做什么。", "conversation_id": cid})
    for _ in range(2):
        if ans.status_code != 502 or "503" not in ans.text:
            break
        time.sleep(8)
        ans = c.post("/v1/ai/answer", json={"content": "用一句话说明柏讯能做什么。", "conversation_id": cid})
    messages = body(ans).get("messages", []) if ans.status_code == 200 else []
    answer_text = " ".join(m.get("content", "") for m in messages if m.get("role") == "assistant")
    rec(
        "free: 问答轨可用",
        ans.status_code == 200 and bool(answer_text.strip()),
        {"status": ans.status_code, "elapsed_s": round(time.time() - t0, 1), "answer": answer_text[:200],
         "detail": None if ans.status_code == 200 else str(body(ans))[:300]},
    )

    # 读：自有数据集 / 产物 / 需求单列表
    for path, label in (
        ("/v1/datasets", "数据集"),
        ("/v1/artifacts", "产物"),
        ("/v1/demands", "需求单"),
    ):
        r = c.get(path)
        rec(f"free: 读 {label} 列表", r.status_code == 200, {"status": r.status_code})

    # 构建类被如实拦（preflight，不落 pending）
    draft = propose(
        c,
        "wire.artifact.draft.create",
        {"kind": "dashboard", "title": "免费档验收板", "spec": {"title": "免费档验收板", "panels": []}},
        cid,
    )
    detail = body(draft)
    text = json.dumps(detail, ensure_ascii=False)
    rec(
        "free: 构建类提议被如实拦（含修复指引，不落 pending）",
        draft.status_code == 422 and "引擎" in text,
        {"status": draft.status_code, "detail": text[:300]},
    )

    # 订阅提议（cost → async 待审批），市场有货时验证；随后拒绝回收
    market = c.get("/v1/market/datasets", params={"limit": 1})
    items = body(market).get("items", []) if market.status_code == 200 else []
    rec("free: 市场列表可用", market.status_code == 200, {"status": market.status_code, "items": len(items)})
    if items:
        ds_id = items[0].get("dataset_id") or items[0].get("id")
        sub = propose(c, "wire.market.subscribe", {"dataset_id": ds_id}, cid)
        sub_body = body(sub)
        rec(
            "free: 市场订阅提议可用（async 待审批）",
            sub.status_code == 201 and sub_body.get("state") == "pending_approval",
            {"status": sub.status_code, "dataset_id": ds_id, "resp": str(sub_body)[:300]},
        )
        if sub.status_code == 201:
            pid = sub_body["proposal_id"]
            rej = c.post(f"/v1/ai/actions/{pid}/reject")
            rec("free: 拒绝订阅提议（不执行）", rej.status_code == 200 and body(rej).get("state") == "rejected",
                {"status": rej.status_code})

    # 免费档合法写提议（需求单）→ 审计可检索；随后拒绝回收
    dem = propose(
        c,
        "wire.demand.submit",
        {"description": "验收用：希望增加区域港口吞吐量指标", "contact_email": "wire-tester1@findmail.com"},
        cid,
    )
    dem_body = body(dem)
    rec("free: 写提议可用（需求单 pending_confirm，合规字段留待卡上填）",
        dem.status_code == 201 and dem_body.get("state") == "pending_confirm"
        and set(dem_body.get("missing_fields") or []) == {"source_url", "public_claim"},
        {"status": dem.status_code, "missing": dem_body.get("missing_fields")})
    if dem.status_code == 201:
        rej = c.post(f"/v1/ai/actions/{dem_body['proposal_id']}/reject")
        rec("free: 拒绝需求单提议（不执行）", rej.status_code == 200, {"status": rej.status_code})

    actions = c.get("/v1/ai/actions", params={"conversation_id": cid})
    rows = body(actions) if actions.status_code == 200 else []
    blob = json.dumps(rows, ensure_ascii=False)
    has_secret = any(s in blob for s in ("wgk-", "Bearer ", "idToken", "token="))
    rec(
        "free: 审计/提议可按会话检索且无凭据",
        actions.status_code == 200 and len(rows) >= 1 and not has_secret,
        {"status": actions.status_code, "rows": len(rows), "no_secret": not has_secret},
    )
    c.close()


def paid_chain() -> None:
    print("\n=== standard 档（wire-tester3）完整链 ===")
    c = client(load("tester3"))
    conv = c.post("/v1/ai/conversations", json={"message": None})
    rec("paid: 建会话", conv.status_code == 201, {"status": conv.status_code})
    cid = body(conv)["id"]
    title = f"验收看板 {int(time.time())}"
    spec = {"title": title, "panels": []}

    before = c.get("/v1/artifacts")
    before_ids = {a["id"] for a in body(before)} if before.status_code == 200 else set()

    # 1) 描述看板 → 提议（预览数据就在草稿 spec 里）
    p1 = propose(c, "wire.artifact.draft.create", {"kind": "dashboard", "title": title, "spec": spec}, cid)
    b1 = body(p1)
    rec(
        "paid: 描述看板 → 草稿提议 pending_confirm（预览 spec 随卡）",
        p1.status_code == 201 and b1.get("state") == "pending_confirm" and b1.get("tier") == "inline",
        {"status": p1.status_code, "state": b1.get("state"), "impact": b1.get("impact")},
    )

    # 2) 卡上确认 → 执行 → 草稿可见
    a1 = approve(c, b1["proposal_id"])
    ab1 = body(a1)
    rec("paid: 确认草稿提议 → executed", a1.status_code == 200 and ab1.get("state") == "executed",
        {"status": a1.status_code, "result": ab1.get("result_summary"), "error": ab1.get("error")})

    drafts = c.get(f"/v1/ai/conversations/{cid}/artifacts")
    draft_rows = body(drafts) if drafts.status_code == 200 else []
    mine = [d for d in draft_rows if (d.get("spec") or {}).get("title") == title]
    rec(
        "paid: 草稿在会话草稿区可见（可预览）",
        drafts.status_code == 200 and len(mine) == 1,
        {"status": drafts.status_code, "drafts": len(draft_rows), "draft_id": mine[0]["id"] if mine else None},
    )
    if not mine:
        return
    draft_id = mine[0]["id"]

    # 3) 转正
    p2 = propose(c, "wire.artifact.promote", {"draft_id": draft_id}, cid)
    b2 = body(p2)
    rec("paid: 转正提议 pending_confirm", p2.status_code == 201 and b2.get("state") == "pending_confirm",
        {"status": p2.status_code, "impact": b2.get("impact")})
    a2 = approve(c, b2["proposal_id"])
    ab2 = body(a2)
    rec("paid: 确认转正 → executed", a2.status_code == 200 and ab2.get("state") == "executed",
        {"status": a2.status_code, "result": ab2.get("result_summary"), "error": ab2.get("error")})

    after = c.get("/v1/artifacts")
    after_rows = body(after) if after.status_code == 200 else []
    new_rows = [a for a in after_rows if a["id"] not in before_ids and a.get("title") == title]
    rec(
        "paid: 转正后产物出现在产物页（新产物行）",
        after.status_code == 200 and len(new_rows) == 1,
        {"status": after.status_code, "new": [a["id"] for a in new_rows], "titles": [a.get("title") for a in after_rows][:5]},
    )
    if not new_rows:
        rec("paid: 发布/分享链（因转正未产出产物而中止）", False, {"blocked_by": "promote 未创建 artifacts 行"})
        return
    artifact_id = new_rows[0]["id"]

    # 4) 发布（write/inline）
    p3 = propose(c, "wire.artifact.publish", {"artifact_id": artifact_id}, cid)
    b3 = body(p3)
    a3 = approve(c, b3["proposal_id"]) if p3.status_code == 201 else None
    ab3 = body(a3) if a3 is not None else {}
    rec("paid: 发布 → published（队内可见）", a3 is not None and a3.status_code == 200 and ab3.get("state") == "executed",
        {"status": p3.status_code, "result": ab3.get("result_summary"), "error": ab3.get("error")})

    # 5) 分享 → 进审批（async）
    p4 = propose(c, "wire.artifact.share.create", {"artifact_id": artifact_id, "ttl_days": 7}, cid)
    b4 = body(p4)
    rec("paid: 分享提议 → pending_approval（async，非立即执行）",
        p4.status_code == 201 and b4.get("state") == "pending_approval" and b4.get("tier") == "async",
        {"status": p4.status_code, "state": b4.get("state"), "tier": b4.get("tier")})

    # 审批人未批前必须仍是 pending
    peek = c.get(f"/v1/ai/actions/{b4['proposal_id']}") if p4.status_code == 201 else None
    rec("paid: 审批前状态保持 pending_approval",
        peek is not None and peek.status_code == 200 and body(peek).get("state") == "pending_approval",
        {"status": getattr(peek, "status_code", None)})

    a4 = approve(c, b4["proposal_id"]) if p4.status_code == 201 else None
    ab4 = body(a4) if a4 is not None else {}
    rec("paid: owner 批准分享 → executed 并返回链接",
        a4 is not None and a4.status_code == 200 and ab4.get("state") == "executed" and "share" in json.dumps(ab4),
        {"status": getattr(a4, "status_code", None), "result": ab4.get("result_summary")})

    share_link = None
    summary = str(ab4.get("result_summary") or "")
    if "/api/v1/share/" in summary:
        share_link = summary.split("）：", 1)[-1].strip()

    # 6) 链接可匿名访问（无 Authorization；链接是根路径 /api/v1/share/...）
    anon = httpx.Client(base_url="https://wire.finddatatech.cloud", timeout=30)
    if share_link:
        r = anon.get(share_link)
        rec("paid: 分享链接匿名可访问", r.status_code == 200, {"status": r.status_code, "path": share_link[:80]})
    anon.close()

    # 7) 审计：可按团队/会话检索，无凭据
    actions = c.get("/v1/ai/actions", params={"conversation_id": cid})
    rows = body(actions) if actions.status_code == 200 else []
    blob = json.dumps(rows, ensure_ascii=False)
    has_secret = any(s in blob for s in ("wgk-", "Bearer ", "idToken", "token=")) or "?t=" in blob
    states = [r.get("state") for r in rows]
    rec(
        "paid: 审计/提议全链可检索且无凭据（含 share 链接不落表）",
        actions.status_code == 200 and {"executed", "pending_approval"}.issubset(set(states)) and not has_secret,
        {"status": actions.status_code, "states": states, "no_secret": not has_secret},
    )
    c.close()


def main() -> int:
    free_tier()
    paid_chain()
    Path("/tmp/wire-accept/report-6.2-api.json").write_text(
        json.dumps(OUT, ensure_ascii=False, indent=2)
    )
    print("\n=== 汇总 ===")
    total = len(OUT["steps"])
    failed = len(OUT["failures"])
    print(f"steps={total} failed={failed}")
    for f in OUT["failures"]:
        print("  FAIL:", f["name"], "->", str(f["detail"])[:300])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
