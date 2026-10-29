# add-fleet-service-accounts

## Why

舰队/无人值守场景现用真人测试账号（aloadtree、platform-test、wire-tester2）承担机器凭据链：人工登录/重连/同会话重铸会杀链（2026-10-25 源码定案：SPA refresh token 会话绑定三条件），审计日志无法区分人与机器，用量与配额混在个人账号名下，且改密/禁用一个账号会连带废掉挂在它身上的全部舰队链。壹座侧 14 天周期重铸自动化（`automation-4665497d`，每 12 天）已上线——重铸已是纯机器操作，缺的只是「只给机器用、人不碰」的专用身份。5.x 外部 cell 开放前必须定案发号方式（壹座交接单 2.3 项）。

## What Changes

- **服务账号供给**：幂等建号配方（Logto 用户 + 专用组织 + wire Customer 映射 `kind=service`），复用既有测试账号建号配方（DB 直插 + argon2i 密码哈希，配方已在册）
- **组织隔离与命名约定**：服务账号入专用 Logto org（`svc-fleet`）、零角色（避开 role 与 registry 组补全互斥坑）、固定命名模式 `svc-<cell>-<n>@finddatatech.cloud`
- **护栏**：服务账号交互式登录巡检告警（UA/通道判别）；密码只进测试账号总表与密钥文件，不进代码与 UI（沿 console-ai-entry 红线）
- **生命周期**：停用/退役配方；停用后既有凭据链按既有 stale 语义诚实降级（壹座侧已实现，无需改动）
- **狗粮迁移**：现役舰队凭据从真人测试账号切换到服务账号（跨线协调：壹座铸币改用服务账号、走 platform mint 通道，续期机制零改动）
- 5.x 端点化（Management API 自动发号）仅在 design 记录方向，不在本 change 实施

## Capabilities

### New Capabilities

- `fleet-service-accounts`: 舰队/无人值守专用服务账号的供给、组织隔离、护栏与生命周期

### Modified Capabilities

（无——续期机制、登录面、重铸节奏均不变）

## Impact

- **Logto 租户**（xinru-master `fd-logto`/`fd-postgres`）：新增 org 与服务账号用户
- **fd-wire**：`scripts/` 新增幂等建号与停用脚本、服务账号巡检脚本；无 API/前端改动（狗粮阶段）
- **docs/TEST-ACCOUNTS.md**：新增「服务账号」节（既有敏感文件约定内流转）
- **壹座侧（协调项，不在本仓）**：fleet cell 铸币凭据源切换 + 观测计时重置知会
- **不改**：line-credentials 续期/重铸机制、console-auth 登录面、`wire-testers` 既有三号用途
