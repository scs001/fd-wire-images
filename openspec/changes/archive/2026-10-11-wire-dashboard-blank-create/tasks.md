# Tasks: wire-dashboard-blank-create

- [x] 1.1 api `POST /api/v1/artifacts`：{kind,title} 校验（kind 白名单
  dashboard/title 非空 422）→ 最小空板 spec 建档（复用既有建档与审计语义）
  → 201 ArtifactDetailOut；测试（建板成功/kind 拒/空标题拒/未登录 401/
  他企隔离由既有语义覆盖）
- [x] 1.2 web ArtifactsPage「新建看板」（标题输入→创建→跳编辑器）+ api
  client createArtifact + i18n；测试（创建流/422 呈现/跳转）
- [x] 1.3 api pytest 全量 + web vitest 全量 + tsc 全绿
- [x] 1.4 生产发布与 E2E 实弹（免费档账号空白建板→编辑→绑定→刷新→导出）
