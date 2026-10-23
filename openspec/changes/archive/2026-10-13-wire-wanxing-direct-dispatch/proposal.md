## Proposal

### what

给 demand-orchestration 增加第二条工单投递通道：**萬星直连调度**。操作员对
in_progress 需求单除既有「转生成工单（舰队拉取器通道）」外，可选择
「萬星直连」——wire 按同一确定性映射与脱敏红线构造 health-loop 工单，
经 A2A `message/send` 直连 spider-heal agent（新域
`wanxing.finddatatech.cloud`、sk- 调用键、`Idempotency-Key` 幂等头），
并把投递事实 append-only 留档（`wanxing_dispatched`）。

### why

- **凭据已备**：wire-wanxing-caller 调用键（$1 限额）已入 fd-wire-env
  secret、spider-heal 允许清单已加、A2A 探针 200 实证（2026-10-12）——
  直连的三件前置全部就绪，只差 wire 侧消费端。
- **链路更快**：现通道=出单→拉取器（~30min 周期）→落仓自推→每日巡检
  代交萬星；直连把「操作员发起→萬星收到」压到一次同步 HTTP 调用，
  给急需单一条快车道。
- **通道仍是补充不是替代**：舰队通道（零外部凭据、公开仓审计）保持
  默认；直连是操作员显式选择的快车道，事件留档区分两通道，谁投的
  一目了然。

### impact

- `api/fd_wire_api/demands/wanxing.py`（新）：A2A 客户端（env 驱动、
  可注入测试 transport）；`demands/orchestration.py` 增 dispatch
  service 与出单闸复用；`demands/routes.py` 内部端点 +
  `admin/routes.py` 控制台动作；web DemandsPage 增「萬星直连」按钮。
- deploy：fd-wire-api.yaml 增 `WANXING_A2A_BASE` / `WANXING_AGENT_SLUG`
  / `WANXING_A2A_TIMEOUT` 明文 env + `WANXING_CALLER_KEY`
  secretKeyRef（fd-wire-env/wanxing_caller_key，secret 已存在）。
- 不动：舰队拉取通道全部行为、需求单状态机、既有 workorder_exported
  事件形状（直连单不进拉取队列）。
