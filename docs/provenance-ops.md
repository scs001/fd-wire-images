# 柏讯溯源账本（wire-provenance-ledger）运维与交付手册

> 定位：客户数据"可追溯、防篡改、第三方可验证"的核心能力。架构与决策见
> OpenSpec change `wire-provenance-ledger`（design.md 十决策）；本文只讲
> 怎么用、怎么运维。

## 一、档位说明（每数据集旋钮）

| 档位 | 行为 | 适用 |
|---|---|---|
| `off` | 不入账（引擎 WAL 仍恒写、转发器跳过入账照 ack；登记基线 sha256 不受影响恒生效） | 内部试算数据 |
| `chain_only`（默认） | 事件入哈希链账本，不锚定 | 客户自查、内部审计 |
| `anchored_batch` | 周期 epoch Merkle 根上链 | 监管年审、审计师核验 |
| `anchored_realtime` | 事件到达即提交单事件根 | 高价值数据集、涉诉场景 |

调档：控制台溯源区块下拉，或 `POST /api/v1/provenance/datasets/{key}/tier`。
调档不影响已入账事件，链保持完整。

## 二、证书卡与核验指引（客户视角）

- 控制台「我的数据」→ 数据集详情 → 溯源区块：状态条（档位/链完整性徽标/
  锚定进度与滞后）、事件时间线（操作者/新旧值摘要）、存证证书卡、
  「下载证据包」。
- 证据包 = `ledger.jsonl` + `checkpoints/` + `anchors/`（zip）。离开平台后用
  `fd-wire/scripts/evidence_verify.py`（自包含，仅标准库）离线核验——用法与
  三层防篡改语义见 `fd-wire/scripts/EVIDENCE-VERIFY.md`。
- OTS（公链）锚定的终局核验只需 BTC 公开区块数据；客户自持检查点模式
  只需客户自存的 `epoch-*.json`（内含公钥，可离线验签）。

## 三、运维手册

### 后台任务（app lifespan 自动起停）

| 任务 | 周期 env（默认） | 职责 |
|---|---|---|
| WAL 转发器 | `FD_WIRE_PROVENANCE_FORWARD_INTERVAL_SECONDS`（30s） | 拉引擎 WAL → 档位过滤 → 入账 → ack；崩溃按 event_id 幂等补转 |
| 封存器 | `FD_WIRE_PROVENANCE_EPOCH_HOURS`（1h） | epoch Merkle 根 → 锚定后端 → 回执回写链；`FD_WIRE_PROVENANCE_ANCHOR_BACKEND=none` 时空转 |
| 完整性自检 | `FD_WIRE_PROVENANCE_SELFCHECK_INTERVAL_SECONDS`（86400s） | 只读巡检链连续/区间构成/回执匹配；异常 `logger.error` 告警；`POST /api/v1/provenance/selfcheck/run` 可手动触发 |

### 锚定后端选择

- `none`：不锚定（chain_only 世界）
- `ots`：公链 OpenTimestamps，免费；回执 pending→BTC 确认异步补齐（分钟~小时），
  滞后在控制台如实可见；`FD_WIRE_PROVENANCE_OTS_CALENDARS` 可覆盖 calendar 池
- `customer`：客户自持检查点，零网络依赖；检查点落
  `FD_WIRE_PROVENANCE_CHECKPOINT_DIR/<ledger>/epoch-<N>.json`，经
  `GET /api/v1/provenance/checkpoints/{epoch}` 交付客户离线保存
- `consortium` / `rfc3161`：接口就绪留位，商务接入后即插（v1 调用即
  NotImplementedError，属预期）

### 密钥与轮换

- 客户自持检查点签名钥匙：`FD_WIRE_PROVENANCE_SIGNING_KEY_PATH`（ed25519
  PEM）。轮换 = 换钥 + 新钥签后续检查点 + 旧钥签一份退役声明（检查点含
  key_id/pubKeyPem，混签期可并存验证）。生产建议钥匙入 OpenBAO 后导出路径
  指向。
- 平台侧账本不可变由触发器（迁移 0012/0013）+ 应用账号最小权限双保险；
  锚定层外部信任锚兜底"整库重写"级威胁。

### datasets 迁出 /tmp（任务 1.1 runbook）

见 `deploy/README.md`「datasets 迁出 /tmp」一节：备份（
`fd-wire/scripts/datasets_off_tmp.py backup`）→ 部署 PVC 清单 → 恢复 →
回归 → 回滚（env 回切）。注意：生产库若由 `create_all` 兜底建表，恢复后
需对库跑 `alembic upgrade head` 使 0013 触发器生效（应用层行为已按触发器
约束改写，不依赖其在场）。

## 四、事件类型命名空间（扩展面）

`dataset.upload.registered` / `dataset.correction.added|reverted|dangling` /
`dataset.snapshot.refreshed` / `dataset.deleted` / `cell.lifecycle` /
`ledger.epoch.sealed`（回执回写）。新事件类型只需新命名空间名 + 走
`append_event` 信封，不改账本结构。
