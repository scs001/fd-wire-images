# Tasks

## 1. api（自动出单核心）

- [x] 1.1 `demands/orchestration.py`：`export_workorder_service` 加可选
      `trigger: str | None = None` 参——非 None 时 `workorder_exported`
      事件带 `trigger` 键（缺省 None 零漂移，人工路径逐字节不变）
- [x] 1.2 `demands/auto_export.py` 新模块：
      - `EVENT_AUTO_EXPORT_FAILED = "auto_export_failed"` 常量；
      - `auto_export_demand(db, demand) -> dict`：in_progress 且无任何
        出单尝试留档才动作；调 `export_workorder_service(trigger="auto")`；
        `WorkorderExportError` 被拒 → 落 `auto_export_failed`
        （reason/status_code，零半写）；意外异常 → 同样落失败事件不抛出；
        返回动作摘要（exported/skipped/failed）
      - `sweep_missing_exports(db) -> dict`：扫 in_progress 单逐单调
        `auto_export_demand`，单次失败不中断，返回 {checked/exported/
        failed/skipped}
      - `start_auto_export_sweeper` / `stop_auto_export_sweeper`：照
        `orchestration_sync` 的 tick 范式（async 循环、to_thread 下放、
        异常只记日志；`DEMAND_AUTO_EXPORT_ENABLED` 缺省开、
        `DEMAND_AUTO_EXPORT_INTERVAL_SECONDS` 缺省 60）
- [x] 1.3 接线：`app.py` lifespan 挂 `start_auto_export_sweeper()` +
      shutdown 停止（照既有循环写法）
- [x] 1.4 测试 `api/tests/test_demand_auto_export.py`（slug 探测一律
      monkeypatch 成 free，零网络）：受理后补扫出单（trigger=auto 标注+
      pending 端点可见）/ 无源地址拒留痕（状态零变化）/ slug 占用拒留痕 /
      补扫幂等（已出单零新事件）/ 失败单不重试 / 关闭开关零动作 /
      submitted 与 delivered 单不被扫 / 意外异常落失败事件不抛出 /
      人工出单事件不带 trigger（零漂移）
- [x] 1.5 既有测试回归核对（transition 路径不变——补扫不挂迁移钩子，
      预期零波及；全量跑确认）——api 1076 passed 零回归；变异校验两轮
      （去失败分支→防重试测试红；去 trigger→标注测试红）

## 2. web（进度面）

- [x] 2.1 `admin/DemandsPage.tsx`：PROGRESS_STAGES 增 `auto_export_failed`
      （红色圆点）；`workorder_exported` 事件 trigger=auto 时渲染自动徽标；
      「尚未出单」占位判定改为「无任何导出与失败事件」
- [x] 2.2 i18n 双语（zh/en）：`progress.stages.auto_export_failed`（含
      失败原因呈现 `reason` 字段）、`progress.autoTrigger` 徽标文案、
      workorder 区补自动出单说明
- [x] 2.3 测试：失败阶段渲染+原因、自动徽标渲染、占位语义（有失败即不
      占位）、既有进度面用例零回归——web 561 绿（+4 用例）

## 3. 门禁与发布

- [x] 3.1 api pytest 全量绿（1077）+ web vitest 全绿（561）+ tsc 零错
- [x] 3.2 reconcile：逐条对 spec delta 核对实现（含边界：幂等/失败不重试/
      关闭开关/占位语义）；补客户面泄漏防护测试（auto_export_failed 零外露）
- [x] 3.3 发布：快照 647fe01→CI 绿（1m56s）→relay（18:21-18:23）→双部署
      滚版 sha-647fe01（api+web 均 Running）
- [x] 3.4 生产冒烟：无源地址存量单 60s 落 auto_export_failed（422 文案
      逐字）+ 合法源单自动出单（trigger=auto+ticket_path+sha256+pending
      端点可见）+ 管理台进度面双态渲染（自动徽标/红点+失败原因+占位
      退场）实弹全过；drill 单已清场（生产 5 单原状、pending 空）
- [x] 3.5 归档+ROADMAP（方向 C 触发自动化落地上产标注）/记忆更新
