# wire-entity-write-surface — Tasks

## 1. api：内置声明动作类型

- [x] 1.1 合成定义：`domain_actions/builtins.py` 落 `entity-declare` /
  `entity-relation-declare` 两定义（字段 schema 按 design D2，risk=write）
- [x] 1.2 动态源解析器扩：内置名保留（客户声明同名 409）；`wire_action_list`
  可见内置两行（`enabled=true` 恒定，响应带 `builtin: true` 标记）
- [x] 1.3 客户声明校验：effect 含 `metadata` 一律 422（仅内置可用）

## 2. api：metadata 效果执行器

- [x] 2.1 提议 preflight 完整跑既有声明校验器（解析 dataset_key/实体名→列快照/
  主键集/唯一性/互斥/隔离），非法 422 不进 pending
- [x] 2.2 执行器 `metadata` 通道：直调 entities service 创建函数（entity.create /
  relationship.create），同事务；执行时复校（漂移），失败实例 status=failed 记原因，
  零半写
- [x] 2.3 迁移 0045：`domain_action_instances.result_ref` varchar(128) 可空双方言
  （真链实证：sqlite+PG 双库）
- [x] 2.4 确认档位复用闸既有管线（write=行内确认）；审计台账带 result_ref；
  执行零计量
- [x] 2.5 既有缺陷修复（design D8，实施期暴露）：① domain_actions router 补
  `/api/v1` 前缀（web/wire-ops 调用的真路径）；② `require_action_editor` 的
  `Customer` 导入改 `billing.models`（原 `console.models` 无此类 → ImportError）

## 3. api：测试（对准坏的东西本身）

- [x] 3.1 声明提案全链：propose（合法草案）→pending→owner 确认→实体落库→图谱端点
  立即可见，与控制台直声明逐字段同构（含 m:n 中间集中间路径）
- [x] 3.2 校验矩阵：主键列不在快照/关系目标不在主键集/重复业务名/他企引用/
  m:n 缺中间集列——各自 422/409/404 如实、零半写、不进 pending
- [x] 3.3 漂移复校：propose 后删列→执行 failed 零半写；撞名竞态→一成一败如实
- [x] 3.4 内置类型不可停用/删除/修改（端点形态 404/405）；同名声明 409；
  客户 metadata 效果 422
- [x] 3.5 免费档可用（声明不付费闸）；跨企业 404 不泄露；viewer 身份拒提议
  （沿 MCP 侧既有语义）
- [x] 3.6 读面零漂移回归：CRUD 端点/图谱/AI 注入/物化端点既有测试全绿不改
- [x] 3.7 api 全量套件回归绿（现基线 1585 测，含路由级 6 测）
- [x] 3.8 路由级（TestClient + console_fixture）：列表内置注入、propose 负 id
  直达闸、非法草案 422 不进 pending、viewer 403、内置 id 编辑端点 404

## 4. web

- [x] 4.1 DomainActionsPanel 内置徽标+操作列空；确认卡影响面摘要=后端 `impact` 文本
  （实体/关系声明内容，经既有闸确认卡呈现，零新渲染）；台账展示 result_ref
  （spec 只要求如实返回引用，未要求跳转——跳转挂账不排期）
- [x] 4.2 i18n zh/en 同批；web 全量套件回归绿（现基线 1035 测）

## 5. 文档与交付

- [x] 5.1 文档「动作」章节增「声明提案」双语小节（同批上线红线）
- [ ] 5.2 strict validate 绿；rollout 双库真链；drillcust001 真库冒烟：
  MCP wire_action_propose(entity-declare)→控制台确认→对象面可浏览全链通
- [ ] 5.3 通知链检查：本 change wire-ops-mcp 零改动→无壹座重烘焙前置（在
  closeout 记一句「无需」以省协调轮）
