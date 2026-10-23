# Tasks: wire-workorder-console-action

## 1. api（service 下沉 + admin 端点）

- [x] 1.1 `demands/orchestration.py`：新增 `export_workorder_service(db, demand, slug_override=None) -> dict` 与异常类型 `WorkorderExportError(status_code, detail)`——整体搬入现路由体逻辑（状态门 409 / 无源 422 / slug 占用 409 / derive 失败 422 / append 留档 + commit + 响应 dict），纯逻辑零行为变化
- [x] 1.2 `demands/routes.py`：`POST /api/v1/demands/{id}/workorder` 改为薄壳调 service（`WorkorderExportError` → HTTPException），测试断言零变化
- [x] 1.3 `admin/routes.py`：新增 `POST /internal/admin/demands/{demand_id}/workorder`（路由级 `require_admin_access` 已双闸；`get_demands_db` 会话；入参沿用 `WorkorderIn` 可选 slug 覆盖）→ 同 service 同口径
- [x] 1.4 测试 `test_admin_demands_workorder.py`：双闸外的全语义——成功出单（响应含 ticket_yaml/ticket_path/queued_for_pull=true/delivered=false/slug_check）、非 in_progress 409、无 source_urls 422、slug 占用 409、重复出单允许（append-only 新事件）、缺令牌 401/403（fail-closed 口径对齐既有 admin 测试）

## 2. web（派发台动作）

- [x] 2.1 `admin/api.ts`：`exportDemandWorkorder(demandId, body?)` → `POST /internal/admin/demands/{id}/workorder`；结果类型 `AdminWorkorderResult{ticket_yaml, ticket_path, delivered, queued_for_pull, slug_check}`
- [x] 2.2 `admin/DemandsPage.tsx`：派发面板内 `in_progress` 单加「转生成工单」按钮（带 busy 态）；成功区展示 ticket_path、slug_check 释义（free=已确证空闲 / unverified=探测通道不可达如实放行）、折叠 `<details>` 工单全文、「已入队未投递——等待舰队拉取器（约 30 分钟）或人工推送」；失败走既有琥珀框如实回显 detail
- [x] 2.3 i18n zh/en 全量键；`DemandsPage.test.tsx` 增补：非 in_progress 不出按钮、成功面板断言（全文可见+未投递文案+无自动投递假成功字样）、422/409 回显、slug unverified 释义在场

## 3. 集成门与发布

- [x] 3.1 全量 `pytest`（api）+ `vitest run` + `tsc --noEmit`（web）零红
- [x] 3.2a commit b6a562a → 三远端推送 → scs001 CI build success
- [x] 3.2b tcr-relay（用户控制台清配额后单补 OK）→ manifest dda8b8c 滚版 → 生产验证全绿：内部端点重出工单 200（drill 单 DE-20261011-99，delivered=false/queued=true/unverified 放行）、admin 端点双闸 401 fail-closed、bundle 含全套新文案、拉取器投递+ack+状态回流 1/1、时间线 append-only 吸收两次导出
- [x] 3.3 strict 零 ERROR 后归档
