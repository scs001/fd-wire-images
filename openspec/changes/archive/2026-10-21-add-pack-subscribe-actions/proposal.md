## Why

柏讯控制台的「源包」页提供免费的指标切片订阅（浏览与订阅均免费、任意档位可用），
但动作闸 v1 名单只覆盖到数据集市场——副驾能代办市场订阅，却代办不了源包订阅，
客户在副驾里说「帮我订阅宏观价格包」时只能被指去控制台。源包订阅与既有
`wire.market.subscribe` 同属"订阅"语义，闸的机制（提议-确认-执行-审计）完全复用，
补进名单即可，无新机制。

## What Changes

- 注册表新增两条动作（第 12/13 条）：
  - `wire.pack.subscribe`（write / inline，**订阅免费**——非 cost，与市场订阅的
    「可能产生费用」语义区分）；
  - `wire.pack.unsubscribe`（write / inline，幂等）。
- 执行体直调 `packs.service.PackSubscriptionService`（订阅/退订幂等语义与
  控制台同源）；preflight 校验非空 `pack_id` + 拒 viewer。
- 会话束 `GateSessions` 增加 `packs` 会话（pack_subscriptions 落元数据库，
  ai/routes 注入）。
- wire-ops-mcp 同步三工具：`wire_packs_list`（读）+ `wire_pack_subscribe` /
  `wire_pack_unsubscribe`（写），`tool-surface.json` `tool_risk` 两条 write；
  夹具与对拍测试同步。

## Capabilities

### Modified Capabilities

- `wire-action-gate`：MODIFIED「v1 动作名单收口」——名单扩为十三条（+源包订阅、
  源包退订）；其余需求不动。

## Impact

- `api/fd_wire_api/ai/actions.py`（GateSessions.packs、两条 ActionDef、
  ACTION_TO_TOOL_NAME +2）
- `api/fd_wire_api/ai/routes.py`（get_gate_sessions 注入 packs 会话）
- `../wire-ops-mcp`：`tools_console.py` 三工具、`tool-surface.json` tool_risk/groups
- 测试：闸单测（tier/执行/幂等）、夹具对拍、mcp 工具转发三例
