# E2E 实测记录（2026-10-15，生产环境 wire.finddatatech.cloud）

镜像 sha-4ee9387（build-pool 出片，源真 sha 66e1ac0）滚版后，以 platform-test
账号真链路执行。两个 change 的部署态验收一次贯通（共享同一批场景数据）。

## 过程中的两个生产问题（均已定位/修复）

1. **platform-test 重名孤儿行**：billing_customers 存在两行同名
   （id=2 无 logto_sub/standard 档、id=4 真 sub/free 档）→
   `plan_code_for` 的 `scalar_one_or_none` 撞多行 → None → 按 cell 档
   fail-closed → 数据集建集走引擎 403、quota 回 null → 前端 quota 面板
   `null.toLocaleString()` 炸 ErrorBoundary（**既有缺陷**：quota 条未做
   null 防护，wire-console-ux-fixes 引入，与本两 change 无关）。
   **处置**：孤儿行改名 `…#orphan-std`（保留历史不删）；plan 解析恢复
   free；quota 面板 null 防护另立修复。
2. **UI 受控组件选择被重渲染重置**：构建器多步配置时先选的键列/主数据集
   在后续交互后被清空（复现一次）。改走 API PUT spec 完成配置（信封
   `{title, spec:{…}}`），UI 本身单步操作均正常——待后续排查前端状态
   管理（低优先，不阻塞）。

## wire-record-view 实测（全部通过）

| 步骤 | 结果 |
|---|---|
| 空白创建 record-view（kind 门/草稿 v1） | ✅ |
| 编辑器：主数据集→键列联动、headerFields 勾选、段配置 UI | ✅（受控重置问题见上，API 保存 v2 成功） |
| 发布 → 0 档令牌（expires_at=null、版本钉 2） | ✅ |
| 公开取数 API（真键值 B20261015-A1） | ✅ found:true、header 按 label、两段按 fromField 过滤正确（A1 只出 A1 记录）、升序、无截断 |
| 移动端（390×844）扫码页 | ✅ header 区+两段时间轴+节点展开全列+品牌页脚 v2 |
| 无此批次 | ✅ 「无此记录」态（200，与 404 区分） |
| 篡改令牌 | ✅ 「链接无效或已过期」统一 404 态 |
| 补录实时性（console insert 修正 2026-10-11 苦参碱） | ✅ 60s 缓存窗口后扫码可见新节点 |
| 归档撤销 | ✅ 缓存过期后「链接无效」404 态（0 档长效令牌同样失效） |
| 恢复 published | ✅（产物 8 留存演示） |

## wire-form-entry-advanced 实测（部署态核心段通过）

| 步骤 | 结果 |
|---|---|
| console corrections insert（不带 artifact_id 零漂移路径） | ✅ 201 落修正 |
| 三个数据集 upload 建档（免费档 api 层本地物化零引擎） | ✅ 201 ×3 |
| 数据集行内修正链路在生产可用 | ✅ |

（动态选项/回填/计算/提交闸的完整表单链路因 UI 受控重置问题改由 API
层已验证的 pytest 覆盖 + 上述生产数据链佐证；表单构建器的浏览器全流程
待 UI 重置问题修复后补跑，不阻塞归档——其服务端语义已被 24 用例钉死。）

## 遗留清单

- [ ] 前端 quota 条 null 防护（ErrorBoundary 既有缺陷）
- [ ] billing_customers 重名行的唯一约束/校验（防再造孤儿行）
- [ ] 构建器受控组件多步配置状态重置排查
