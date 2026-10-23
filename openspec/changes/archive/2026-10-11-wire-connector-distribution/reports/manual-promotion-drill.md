# 手动晋升演练证据（wire-connector-distribution 任务 5.2）

日期：2026-10-11 · 环境：macOS 本地（uvicorn 单进程，端口 8765）

## 演练目录

`/tmp/fdwire_drill.kXyUTi/`（按 deploy/README.md「手动晋升」步骤构造）：

- `connector/fd-wire-connector-linux-amd64`——**本仓真实构建**（`go build -trimpath -ldflags "-X main.version=sha-drill01"`，18,773,164 字节）
- `connector/fd-wire-connector-darwin-arm64`——17 字节占位件（演练「部分缺失」态用）
- `connector/manifest.json`——按文档 jq 循环生成（version=sha-drill01、逐平台 sha256/size）

启动：`FD_WIRE_DOWNLOADS_DIR=/tmp/fdwire_drill.kXyUTi uv run uvicorn fd_wire_api.app:app --port 8765`

## 1) manifest 端点（两平台齐备）

```
$ curl -s http://127.0.0.1:8765/api/v1/downloads/connector/manifest.json
{
  "version": "sha-drill01",
  "released_at": "2026-10-10T17:33:32Z",
  "platforms": [
    { "platform": "darwin-arm64", "filename": "fd-wire-connector-darwin-arm64",
      "sha256": "9a0933…fb1e", "size": 17, "available": true },
    { "platform": "linux-amd64", "filename": "fd-wire-connector-linux-amd64",
      "sha256": "a2fa69…9222", "size": 18773164, "available": true }
  ]
}
```

## 2) 部分缺失（删除 darwin 文件后再取）

```
$ rm connector/fd-wire-connector-darwin-arm64
$ curl -s …/manifest.json | jq '.platforms[] | {platform, available}'
{ "platform": "darwin-arm64", "available": false }   ← 仅该条目降级
{ "platform": "linux-amd64",  "available": true  }   ← 其余不受影响
```

## 3) 二进制端点头（Content-Length + X-Checksum-Sha256）

```
$ curl -s -D - -o downloaded-linux-amd64 …/downloads/connector/linux-amd64
HTTP/1.1 200 OK
x-checksum-sha256: a2fa69e0643538355a1a26aad30c6305d1d6175ae03340134a28a4538f039222
content-disposition: attachment; filename="fd-wire-connector-linux-amd64"
content-length: 18773164
```

## 4) sha256 校验（下载件 vs manifest 声明）

```
local:    a2fa69e0643538355a1a26aad30c6305d1d6175ae03340134a28a4538f039222
declared: a2fa69e0643538355a1a26aad30c6305d1d6175ae03340134a28a4538f039222
SHA256 MATCH ✔
```

## 5) 缺失平台二进制 → 404 带原因

```
$ curl -s -w "status=%{http_code}" …/downloads/connector/darwin-arm64
{"detail":"平台 darwin-arm64 的文件缺失（…/connector/fd-wire-connector-darwin-arm64）：
 该条目在 manifest 中标记为不可用，请等待重新晋升。"}
status=404
```

## 6) 配对向导显示链接

向导链接构造为 `${origin}/api/v1/downloads/connector/{platform}`（与上列端点同路径），
三态渲染由组件测试断言（`web/src/console/DatasetsConnectors.test.tsx`，8 用例：
正常链接/部分缺失/404 与 502 降级/sha256 复制/文档锚点互链/版本对照）。
