# 免费档生产 E2E 证据（2026-10-11）

## 服务端实证（platform-test 真账号，免费档）

登录 platform-test@findmail.com（Logto sub 7jbveavwyk1x → billing_customers id=4，
plan_id=4=free）后，生产 api 日志（fd-prod）实证完整免费链路：

```
POST /api/console/session        200  （会话建立+客户映射）
GET  /api/v1/datasets            200  （免费档数据集列表——页面已放行非升级墙）
GET  /api/v1/datasets/quota      200  （新增配额端点返回免费限额）
```

免费档「我的数据」页的取数链路在生产上以真实账号走通。

## 视觉渲染验证的局限与二分排除

IAB 网页视图内带会话访问 #/console 渲染空白（root 卸载）。**二分定案**：手动滚回
上一发布镜像 sha-d6b2bfc（10-10 wire-ux-consolidation 生产 E2E 通过版）复测——
**同样空白**。排除今晚 web 变更引入回归；定性为 IAB 网页视图对
auth.finddatatech.cloud iframe（静默续期）的 cookie 沙箱局限：ConsoleView 的
loading 态渲染 null，续期挂起时页面停留空白。用户真浏览器 10-10 正常使用过控制台
（wire-console-ux-feedback 当日四条反馈为证）。

## 遗留改进（非阻塞，后续小改）

- ConsoleView loading 态应渲染可见的加载指示而非 null（边缘浏览器下「白屏」体验
  误导排障方向——今晚即被误导约一小时）
- refreshSession 无超时兜底（挂起即永久 loading）
