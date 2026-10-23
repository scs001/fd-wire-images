# 3.3 行数门灰度实弹 + registry 部署记录（2026-10-10 凌晨）

## 部署（registry 侧）

- 镜像：`ccr…/yizuo/mcp-auth-server:sha-0aad2b5`（= 生产基线 65139f8 + wire-quota-gate 分支 0aad2b53，最小增量不带 origin/main 未上线改动）
- 构建：**scs001/mcp-gateway-registry（public fork）GHA 免费构建**（org 私有仓 Actions 被 billing 挡；TCR secrets 用户口授配入 fork，5 分钟出双镜像）→ hkccr → cheap-1 手动回灌 ccr（tcr relay 不自动同步新 tag，补单配方与 fd-wire 同款）
- 上线：`docker-compose.prebuilt.yml`（备份 .bak-quota-20261010）tag 更换 + auth-server env 四键入 compose、值入 `.env`（`FD_WIRE_QUOTA_GATE=true`、URL=wire 公网、internal token 服务端注入、TTL=60）+ `up -d --no-deps auth-server` → healthy
- 分支留档：gitee `wire-quota-gate` 分支 + scs001 main；org github main 未动（registry 专属 session 归口）

## 实弹证据（生产真件，docstudio 客户）

| 场景 | 结果 |
|---|---|
| 0 行测试档（e2e-gate-drill）+ 新铸 key | **402 `MONTHLY_QUOTA_EXHAUSTED`**「当月额度已用完：本月包含的读取行数已耗尽，次月自动恢复…」——错误码与余额 402 `INSUFFICIENT_BALANCE` 明确区分 ✅ |
| free 档（1000 行）+ **sub2api 余额 0** | TTL 窗口后 **200**（enforce_balance=false → 余额门跳过；最初免费档 0 余额被误杀的问题根治）✅ |
| TTL 行为 | 换档后 60s 内探针返回陈旧判定（设计内分钟级缓存），过期后即取新值 ✅ |
| wire 侧口径 | `GET /internal/quota/70` → free 1000/1000/enforce_balance=false（与行数门同源一致）✅ |

## 附带事件（本窗处置）

- **registry 审计停摆 10-09 16:10–16:19**：根因=mcpgw-server 楔死（容器 healthy 但零处理零日志；Mongo 无写错误、磁盘 96% 非直接原因）；处置=重启 mcpgw；恢复=实证（wire/facet 线新事件持续入库）。复发监控建议另立。
- 测试键卫生：实弹过程铸 5 把测试 key（docstudio-e2e-2/gate-drill/free-zero×3，docstudio 测试身份），明文仅存于服务端 shell 变量从未落盘；patch-keys 列表面不枚举客户键，如需吊销走 registry 管理台按名搜索。
- cheap-1 磁盘 96%（containerd 13G 全在引用、safeline 5.9G 资源）——本次未清出空间，建议扩容或专项清理（/root 有并发会话清理工作目录现象，构建物已落 /opt）。

## 8.2 余项核对

- api/web 滚动 ✅（sha-7fae9e5→f226cae→0ba2e85→b717c7d 四连滚，皆实证）
- 登录流冒烟 ✅（4.7 E2E 即真实登录流全链）
- registry 探针灰度 ✅（本报告）
- 生产冒烟四项 ✅（定价四卡/plans 端点/旧三档 404/admin 401，此前记录）
