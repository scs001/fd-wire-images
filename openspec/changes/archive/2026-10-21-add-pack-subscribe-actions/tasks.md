## 1. 闸侧

- [x] 1.1 `GateSessions.packs` 会话（ai/routes 注入 packs 元数据库；actions.close 收口）
- [x] 1.2 两条 ActionDef（subscribe/unsubscribe，write/inline；preflight 非空 pack_id + 拒 viewer；执行体直调 PackSubscriptionService）
- [x] 1.3 ACTION_TO_TOOL_NAME +2；夹具对拍测试 ops 工具集扩充

## 2. 操作面（wire-ops-mcp）

- [x] 2.1 三工具（wire_packs_list 读 / wire_pack_subscribe / wire_pack_unsubscribe 写）+ 转发单测
- [x] 2.2 tool-surface.json tool_risk 两条 write + customer 组工具清单；selfcheck 13 tools 一致

## 3. 验证

- [x] 3.1 api 全量测试绿（1404 passed）；wire-ops-mcp 10/10 绿
- [x] 3.2 真机：壹座侧镜像条目上线后，经闸订阅/退订一个真实源包并核订阅态（2026-10-21：wire-ops-mcp sha-f3161a4 13 工具上线；壹座闸 propose `wire.pack.subscribe` population-employment → inline 确认 → executed（subscribed:true）；退订 → executed；`wire_packs_list` 回读 subscribed=[macro-prices] 复原）
