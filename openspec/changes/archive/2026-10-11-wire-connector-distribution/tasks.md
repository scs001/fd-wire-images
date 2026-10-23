# Tasks — wire-connector-distribution

## 1. 下载端点（api）

- [x] 1.1 `FD_WIRE_DOWNLOADS_DIR` 配置项落 config（未配置时端点 404 且错误信息说明原因）；manifest 端点返回版本/平台清单/sha256/发布时间；二进制端点按 platform 流式返回并带 `Content-Length` 与 sha256 头
- [x] 1.2 manifest 与目录内文件一致性校验：manifest 声明的平台文件缺失时该条目标记 unavailable 而非整页 404；单测覆盖未配置/缺文件/正常三态

## 2. CI 晋升（构建链）

- [x] 2.1 build.yml connector job 增加晋升步骤：凭据（secrets）存在时上传产物至下载目录并重建 manifest（含 sha256）；无凭据时步骤 skip 并在 job 摘要输出手动晋升命令
- [x] 2.2 deploy/README.md 取件段重写：下载端点地址、手动晋升步骤（artifact 下载→目录放置→manifest 更新→sha256 校验命令）、GHA artifact 定位说明降位

## 3. 配对向导与版本对照（web）

- [x] 3.1 安装指引第一步接 manifest：按平台列真实下载链接（版本+sha256 展示+复制按钮）；manifest 不可用时显示「暂不可下载」并保留命令行指引（不隐藏步骤）
- [x] 3.2 连接器状态列表加「最新版本」对照：心跳版本落后时显示升级提示；i18n ZH/EN
- [x] 3.3 web 组件测试：manifest 三态（正常/部分 unavailable/不可达）渲染断言

## 4. 文档教程（web）

- [x] 4.1 「文档」页新增「我的数据与连接器」章节（ZH/EN）：上传三步（拖拽→列类型确认→建集）、连接器三步（令牌→pair→run）、常驻方式（systemd/nssm/launchd 配置样例）、故障排查（心跳 warnings/schema 漂移暂停/行上限/吊销后重配对）
- [x] 4.2 章节与配对向导互链（文档→控制台入口、向导→详细教程锚点）

## 5. 验证

- [x] 5.1 api 测试：downloads 三态、隔离（无鉴权要求的公开端点语义确认——下载不涉敏感数据，但 manifest 版本号对外可见性按公开口径断言）
- [x] 5.2 手动晋升演练：本地目录放测试二进制→端点可取→配对向导显示链接→sha256 校验一致；证据（curl 输出）存 `reports/`
