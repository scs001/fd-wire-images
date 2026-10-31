# wire-entity-mn-materialize — Tasks

## 1. 引擎腿（DAAS 仓协调 + cell 镜像构建线）

- [x] 1.1 `customer_dataset_link` 加可选 junction 参数（中间集 + 两列）：
  桥行驱动 INNER 对展开（design D1/D2）；缺省参数时 n:1/1:1 逐字节等价
- [x] 1.2 引擎测试：真 SQLite 对表展开逐行对账、孤儿桥丢弃计数上报、
  中间集列不进结果、`right_` 前缀、引擎行数闸整批拒绝、重放 spec 含 junction
- [ ] 1.3 PG 腿：同测试集 PG 后端跑（跳的须是 `@requires_sqlite` 执行类，
  Wave F 口径）
- [ ] 1.4 移植到 cell 镜像**实际构建线 `internal-main`**（非仅 `main`——Wave F
  实证教训），出片 sha tag 并在册

## 2. api 腿

- [x] 2.1 materialize 端点解除 m:n 422：kind=m:n 走 junction 路径；三端 cell 档闸
  （任一免费档 403 如实）
- [x] 2.2 物化前桥行数估算闸：闸值读既有 link 行数上限单一真源（配置，不硬编码），
  超估算 422 给数、零写、子集不登记（design D3）
- [x] 2.3 子集登记 derived（source_ref=左集）+ 列/体量回填 + 成对主键
- [x] 2.4 血缘 spec 增 junction 三要素；`derive_refresh` 按 spec 重连三端最新数据；
  n:1/1:1 无 junction 键 spec 重放逐字等价（向后兼容读）
- [x] 2.5 失存/漂移路径：关系/数据集/列快照漂移 404/4xx 如实、零半写；
  旧引擎无 junction 支持→可读错误不伪造成功

## 3. api 测试（对准坏的东西本身）

- [x] 3.1 m:n 物化真链冒烟（SQLite 全绿；PG 腿随 5.2）：手算关系对逐行对账、
  孤儿桥计数、三端档位闸、估算闸、重物化走最新桥
- [x] 3.2 n:1/1:1 物化回归：既有测试全绿、逐字零漂移（含旧引擎报错路径）
- [x] 3.3 api 全量套件回归绿（现基线 1616 测）

## 4. web

- [x] 4.1 实体页 m:n「物化连接」（勘误：web 本就未对 m:n 置灰——按钮渲染只查双端
  cell 档，m:n 点了吃服务端 422；本任务实做=成功提示 doneBridge 带展开对数/孤儿桥
  计数 + 假后端补 m:n 创建/物化支持 + 信封对齐测试更新 junction 三键）；确认卡展示展开语义
  （估算对数 + 孤儿桥预估）；派生集/管线图登记与既有一致（中间集在 spec 详情注记）
- [x] 4.2 i18n zh/en 同批；web 全量套件回归绿（现基线 1035 测）

## 5. 交付

- [x] 5.1 文档「实体」章节「物化」小节 zh/en 同批更新（同批上线红线）
- [ ] 5.2 strict validate 绿；cell 镜像 rollout（三支客户 cell 升级）+ 双库真链；
  drillcust001 真库冒烟：经写面通道声明 m:n → 物化 → 行数手算对账 → 重物化
- [ ] 5.3 closeout 记：本 change wire-ops-mcp 工具面零改动 → 无壹座重烘焙前置；
  引擎构建线 `internal-main` 移植确认（防「镜像内算子计数为 0」复现）
