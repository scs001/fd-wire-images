# Tasks — wire-source-packs

## 1. 定义与数据（api）

- [x] 1.1 packs/definitions.py：首批 6-10 个包（覆盖 domain/source_db 混合：如 宏观价格、人口与就业、货币金融、行业景气、区域经济、年鉴全景…），每包 {id,name_zh,name_en,desc,filter}；filter 支持 domains[]/source_dbs[]/codes[] 任一
- [x] 1.2 alembic 0022：pack_subscriptions（id、customer_id、pack_id、created_at，(customer_id,pack_id) 唯一）；迁移幂等+drift check
- [x] 1.3 service：包指标数与清单按 filter 实时查 indicator_snapshots（registered 态）；订阅/退订幂等

## 2. API（api）

- [x] 2.1 GET /api/v1/packs（公开：包列表+各包指标数）与 GET /api/v1/packs/{id}（公开：详情+分页指标，page/page_size）
- [x] 2.2 GET /api/v1/packs/subscriptions（登录，我的订阅 id 列表）；POST/DELETE /api/v1/packs/{id}/subscribe|unsubscribe（登录；重复订阅幂等 200；未知包 404；跨客户隔离断言）
- [x] 2.3 pytest 覆盖上述全部路径

## 3. 控制台页（web）

- [x] 3.1 ConsoleLayout 左侧栏增「源包」tab（顺序：AI 构建/产物/我的数据/源包/我的需求/用量）；PacksPage：包卡片墙（双语名/描述/指标数/订阅徽标）+「仅看已订阅」过滤
- [x] 3.2 包详情抽屉：分页指标清单（code/名称/domain），点击跳 #/catalog/{code}；订阅/退订按钮即时切换
- [x] 3.3 api.ts 追加 packs 函数（文件末尾）；i18n ZH/EN（console.packs.*）；组件测试：卡片墙/详情抽屉/订阅切换/过滤
- [x] 3.4 免费档可见可用（无档位闸）

## 4. 验证

- [ ] 4.1 api 全量测试绿；web 全量测试绿+tsc 干净；两提案 validate 零错误
- [ ] 4.2 生产冒烟（随发布）：/api/v1/packs 200 含包与指标数；订阅端点 401 未登录态
