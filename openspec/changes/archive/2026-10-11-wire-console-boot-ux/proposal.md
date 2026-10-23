# wire-console-boot-ux

## Why

生产排障实证（2026-10-11，free-tier-data-gate reports/production-e2e）：
ConsoleView 启动期 loading 态渲染 null——在静默续期挂起的浏览器环境（iframe
被沙箱）表现为整页白屏，无任何可见反馈，排障被误导约一小时。两处小改让启动
状态可见、挂起有界。

## What Changes

- loading 态渲染**可见加载指示**（居中 spinner+文案）而非 null。
- `refreshSession`（静默续期）加**超时兜底**（默认 15s 超时按失败处理→回落
  登录引导），不再无限等待。

## Capabilities

### Modified Capabilities

- `console-auth`: 登录会话续期具备超时兜底；控制台启动期呈现可见加载态。

## Impact

- fd-wire/web：ConsoleView.tsx（loading 渲染）、console/auth.ts（refreshSession
  超时）、i18n 双语；组件测试两处。
