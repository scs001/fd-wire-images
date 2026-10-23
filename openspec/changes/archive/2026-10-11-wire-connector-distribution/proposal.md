# wire-connector-distribution

## Why

配对向导第一步写「从发布页下载对应平台的 fd-wire-connector 二进制」，但发布页不存在：
CI connector job 产物只存 runner artifact（90 天、需仓库权限），客户拿到配对令牌后
无路可取件——本地数据能力的客户旅程在第一步断链。同时 web「文档」页通篇没有
「我的数据/连接器」教程（wire-customer-local-data 设计承诺的"上线同批双语说明"
未兑现）。本 change 补齐取件链与教程，是可发现性三断点的第二、三件。

## What Changes

- **api 下载端点**：`GET /api/v1/downloads/connector/manifest.json`（版本、平台
  清单、sha256、发布时间）与 `GET /api/v1/downloads/connector/{platform}`（二进制，
  platform 如 `linux-amd64`）；服务自 `FD_WIRE_DOWNLOADS_DIR` 目录，未配置或文件
  缺失时 404 带说明（诚实降级，不假装有发布页）。
- **CI 发布步骤**：connector job 产物归档到下载目录——runner 具备出口凭据时
  rsync/scs001 通道自动晋升；不具备时按 deploy 文档手动晋升（文档写死步骤与
  sha256 校验命令）。两条路径以 manifest 的 sha256 为唯一事实源。
- **配对向导真实链接**：安装指引第一步按 manifest 办平台下载链接（带版本与
  sha256 展示 + 复制按钮）；manifest 不可用时如实显示「暂不可下载」并保留
  命令行指引。
- **版本可见**：连接器状态列表呈现「当前版本 vs 最新版本」，落后时提示升级
  （心跳已上报版本，只差对照面）。
- **文档教程**：web「文档」页新增「我的数据与连接器」章节（ZH/EN）：上传三步、
  连接器安装三步（pair/run/常驻 systemd/nssm/launchd）、故障排查（心跳
  warnings、schema 漂移暂停、行上限、吊销重配对）。

## Capabilities

### New Capabilities

- `connector-distribution`: 连接器二进制的发布与取件——下载端点、版本清单、
  配对向导真实链接、CI 晋升路径（自动或手动文档化）、心跳版本对照；以及
  「我的数据与连接器」客户教程。

## Impact

- **fd-wire/api**：新增 downloads 路由（静态目录 + manifest 校验）与
  `FD_WIRE_DOWNLOADS_DIR` 配置。
- **fd-wire/.github/workflows/build.yml**：connector job 增加晋升步骤（凭据存在时
  自动，否则跳过并输出手动指引）。
- **fd-wire/web**：DatasetsConnectors（下载链接/版本对照）、DocsView（新章节）、
  i18n 双语。
- **fd-wire/deploy/README.md**：取件段改为「下载端点 + 手动晋升步骤 + sha256
  校验」，GHA artifact 降位为构建产物缓存。
- **不碰**：连接器二进制本体、配对协议、心跳协议。
