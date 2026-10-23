# Tasks

## 1. api

- [x] 1.1 迁移 0029：`member_preferences`（ConsoleBase/平台库；sub 主键+三键
      裸列+updated_at），链 0028→0029；链头断言测试全部 bump
- [x] 1.2 `require_console_member_identity`（任意已映射身份含 viewer，无方法
      门禁）；偏好服务：部分更新/校验（含跨客户同文案）/置顶重验自愈
- [x] 1.3 GET/PUT `/api/console/me/preferences` 端点（PUT 响应=写后全量）
- [x] 1.4 测试：per-sub 隔离/viewer 可写/部分更新与清空/三类 422/自愈清列/
      未映射 401（pytest 全绿）

## 2. web

- [x] 2.1 api client：getPreferences/putPreferences；登录引导拉取（失败不阻塞）
      +locale 收敛与首次领养+顶栏切换即写
- [x] 2.2 落地序：裸 #/console 才走偏好；pinned>landing_tab>datasets；置顶
      跳转=artifacts+展开看板；置顶跳转不回写 landing_tab，用户切页签即写
- [x] 2.3 看板行置顶/取消置顶+默认徽标（testid：artifact-dashboard-pin/
      unpin/default）；testing.ts fakes+i18n 键；vitest 全绿+tsc 0

## 3. 门禁与发布

- [x] 3.1 本地门禁：api pytest 全量+web vitest 全量+tsc --noEmit 0
- [x] 3.2 迁移 0029 离线 SQL 先行上库（wire_platform+wire_datasets 双库），
      再滚 api/web 镜像（快照 tag）
- [x] 3.3 生产冒烟：locale 跨端收敛/裸入口落地序/置顶自愈实证
- [ ] 3.4 归档+ROADMAP 标注+记忆更新
