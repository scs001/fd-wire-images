# Tasks — wire-console-boot-ux

- [x] 1.1 ConsoleView loading 态渲染可见加载指示（spinner+「正在进入控制台…」文案，i18n ZH/EN），不再返回 null；测试断言 loading 期页面非空
- [x] 1.2 refreshSession 超时兜底：默认 15s（env 常量可调），超时按续期失败处理（清会话回落登录引导）；单测用假 timer 断言超时路径
- [x] 1.3 全量 web 测试绿 + tsc 干净
