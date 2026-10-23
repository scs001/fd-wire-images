## tasks

- [x] 1. api A2A 客户端 `demands/wanxing.py` + 单测（mock transport：
  成功解析/401/403/网络/非 JSON；键不落日志断言；工厂注入出口）
- [x] 2. orchestration 闸助手抽取（export 路径零漂移）+ dispatch service +
  双闸端点 + 测试（状态门/无源/禁爬/slug occupied/未配置 503/失败留档
  502 带全文/成功事件形状/幂等键稳定/拉取队列不收录/内部鉴权 401）
- [x] 3. web DemandsPage 萬星直连按钮 + 结果/失败面板 + i18n 三语 +
  组件测试（成功展示回执、失败展示全文降级、非 in_progress 不渲染）
- [x] 4. deploy fd-wire-api.yaml 三明文 env + WANXING_CALLER_KEY
  secretKeyRef；deploy/README 增「萬星直连调度」节（env 表+冒烟配方）
- [x] 5. 门禁：api pytest 全绿 + web vitest 全绿 + web build（主代理）
- [x] 6. 发布：双推 → 镜像链（clone/rsync/GHA/hkccr/relay/ccr）→
  manifest bump + provenance 注 → ArgoCD hard-refresh → ready/health
- [x] 7. 生产冒烟：内部探针（未配置路径不触发）→ drill 需求单实弹直连
  （真 A2A 调用、事件落库验证、拉取队列空验证）→ drill 清理
- [ ] 8. 归档：validate --strict → archive → ROADMAP 方向 C 勾注 →
  memory 更新
