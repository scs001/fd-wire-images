# wire-bff-reference

柏讯 REST 数据面的**参考 BFF**（wire-customer-data-engine 7.2）——浏览器
前端的标准接驳形态：数据面 key 全程持于服务端，浏览器只拿会话 cookie。

## 为什么必须经 BFF

数据面 key（`wdk-…`）是服务端秘密：浏览器裸持等于公开它（CONTEXT.md
「数据面 key」红线）。REST 数据面不开 CORS，浏览器直连本来就被拦——
BFF 不是可选项，是唯一形态。

## 形态

- `POST /bff/login` `{username, password}` → 签发 HttpOnly 会话 cookie
  （HMAC 签名、1 小时）。**参考实现用演示账号表**；接入方应替换为自己的
  用户体系（Logto/OAuth/自建），会话机制不变。
- `GET /bff/me` → 会话有效性探测。
- `ANY /bff/api/*` → 转发到 `WIRE_API_URL`（同名路径 `/api/v1/…`），
  注入 `Authorization: Bearer $WIRE_DATA_PLANE_KEY`；响应原样回传
  （含 429 限流与 403 allowlist 拒绝——不吞不改装）。

key 配置：环境变量 `WIRE_DATA_PLANE_KEY`（控制台「数据面 key」铸造后
填入部署 env，绝不进代码、绝不进浏览器）。

## 本地跑

```bash
npm install
WIRE_API_URL=https://wire.finddatatech.cloud \
WIRE_DATA_PLANE_KEY=wdk-xxxx \
BFF_SESSION_SECRET=change-me \
npm start          # :8787
```

端到端走查（7.2 验收形态）：

```bash
curl -c jar -X POST localhost:8787/bff/login \
  -H 'content-type: application/json' -d '{"username":"demo","password":"demo"}'
curl -b jar localhost:8787/bff/api/api/v1/datasets/<id>/query \
  -H 'content-type: application/json' -d '{"filters":[],"limit":10}'
```

## 壹座 pack 部署

镜像 `Dockerfile` 已就绪（Node 22 alpine、非 root、HEALTHCHECK /bff/me）。
以壹座 packs 部署台账发布（不直连 agent-runner——wire-base-integration
契约）；pack 发布环节由 paas 会话执行（paas 仓规约）。
