# 8.3 最终验收汇总（wire-self-serve-launch，2026-10-10 凌晨）

## 交付面（生产在跑）

| 面 | 版本/证据 |
|---|---|
| wire api/web | `sha-b717c7d`（cheap fd-prod，四连滚 7fae9e5→f226cae→0ba2e85→b717c7d） |
| registry auth-server | `sha-0aad2b5`（wire-quota-gate 分支=生产基线+行数门；cheap-1 prebuilt compose，gate=on） |
| 主官网 | `sha-5ae66e5`（tencent fd-web，伴生 change wire-tab-refresh 8/8 已收官） |
| 生产库 | alembic 版本位 0015（手写等价 SQL 路径；四档 seed、旧三档禁用、console_onboardings 表） |

## 逐能力验收锚点

1. **四档定价全链**：`GET /api/v1/plans` 信封逐字（free 0/1,000、basic ¥10/100,000、standard ¥50/1,000,000）；定价页 bundle 新口径「用完即停、次月恢复」、旧价格零残留；改价不发版（billing_plans DB 驱动）
2. **首登自动开户**（e2e-self-serve.md）：Logto 登录→sub2api 代建→铸 key→caller-map→映射落库；key_once 仅一次；断点幂等（mint 后失败重试不重铸）
3. **MCP 200**：initialize（finddata 4.0.11）+ tools/list + read 实调
4. **行数门**（quota-gate-livefire.md）：0 行档 → 402 `MONTHLY_QUOTA_EXHAUSTED`（与余额 402 区分）；free 档 0 余额 → 200（余额门对免超额档退位）
5. **停用即断**：吊 key → 401；suspended → quota 归零（0/0），复活 → 1000 恢复
6. **admin 双闸**：BEARER_GATE=on 生效（无角色/无令牌各拒）；wire-admin 角色挂 aloadtree
7. **令牌轮换**（token-rotation.md）：三键轮换，旧值实弹 401，值=root-only cheap-1 /opt/fd-wire-tokens.txt；INFRA-NOTES 明文已打码
8. **用量透明**：`/api/console/usage` 端点+控制台用量页正式化（「上线中」移除；数据流=registry 审计→poller→metering_records，审计恢复后自动充盈）
9. **主官网**：双语新文案+「查看定价」CTA→wire 定价页；全站零价格数字（构建期数字律合规）

## 占位退位清单（全部完成）

- `console/customer.ts`（X-Customer-Id 手填）——删除
- `admin/api.ts` DEFAULT_ADMIN_TOKEN/dev-admin-token——删除（nginx envsubst 服务端注入真值）
- `LoginView` 死按钮——激活为真登录
- 定价页 i18n 档位数字键——删除（API 活取）
- 「控制台上线中」过时口径——清除

## 测试与合规

- api **552/552** pytest；web **152/152** vitest（i18nKeys 门过）；registry 专项 78/78（全仓 17 失败=HEAD 基线预存，零回归）
- `openspec validate wire-self-serve-launch --strict` 通过（RFC2119 词法 warning 与仓内中文 spec 风格一致，非错误）

## 过程事件与移交（如实）

- 生产事件三起当场闭环：mcpgw 审计停摆（重启恢复）、sub2api 旧契约×2（重写）、铸 key form 形状（JSON 化）
- **移交 registry 专属 session**：gitee 分支 `wire-quota-gate`（=scs001 main）合流裁决、origin github main 未动；mcpgw 楔死根因未深挖（建议加监控）；cheap-1 磁盘 96% 建议扩容
- E2E 过客：docstudio 测试身份为首位自助客户（free 档，5 把实弹 key 明文未落盘）
