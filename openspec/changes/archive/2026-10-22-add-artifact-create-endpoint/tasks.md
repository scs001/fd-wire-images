# tasks — add-artifact-create-endpoint

## 1. 契约层（schema + service）

- [x] 1.1 `ArtifactCreateIn` 增可选 `spec` 字段（缺省 None = 空白创建路径完全不动）；
      模型只保字段在位与类型，业务校验留在 service 层
- [x] 1.2 service 新增带内容创建：给定 spec 走 `validate_kind_spec` 分派校验
      （与 PUT 同一函数），建档手法复用 `create_blank_artifact` 同款
      （v1 版本行 + operator 审计 + 初始状态留痕），title 投影口径一致
- [x] 1.3 slug 幂等：service 层同租户 `showcase_slug` 查重（下沉 import_showcase
      的遍历扫描）；命中走 PUT 同款追加版本；规范化 JSON 语义比对，内容一致
      不追加冗余版本
- [x] 1.4 权限门：带 spec 创建限 `finddata-official`（复用 `is_platform_publisher`
      判定），越权 403；不带 spec 路径不过此门

## 2. 路由层

- [x] 2.1 `POST /api/v1/artifacts` 接线：新建 201 / slug 重放 200（响应如实区分）；
      403 与 422 语义可区分
- [x] 2.2 api 测试：平台带内容建档、非平台 403、校验失败 422 零半写、不带 spec
      行为不变（既有空白创建测试全绿不动）、同 slug 重放追加版本、同 slug
      内容一致不冗余、跨租户 slug 隔离

## 3. import_showcase HTTP 化

- [x] 3.1 脚本增 `--http-base-url` + 平台租户凭据模式：POST（spec+slug 幂等）→
      publish → visibility 全走开放端点；pod 直跑保留为回退
- [x] 3.2 重放安全性实测：同批 slug 连跑两次，断言零新建、版本号 +1、内容一致

## 4. 验收与归档

- [x] 4.1 全量测试绿 + `openspec validate add-artifact-create-endpoint --strict`
- [x] 4.2 生产验收：HTTP 模式重放 5 张省级看板（province-population/economy/
      consumption/fiscal/livelihood），断言零新建且内容一致；`import_showcase`
      从此可离开 pod 运行
- [x] 4.3 归档（`openspec archive`）
