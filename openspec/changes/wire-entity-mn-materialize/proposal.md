# wire-entity-mn-materialize

## Why

Wave C 留了一个诚实口径：m:n 关系可声明、可进图谱、可被 `wire_object_relations`
沿中间集遍历，但关系物化端点 MUST NOT 支持（调用 422）——对象世界的「关系对」
变不成可被管线与看板消费的子集。「本体建设 v1」验收判据第 4 条要求舰队的 m:n
声明「可真实遍历并可物化」；且声明写面（`wire-entity-write-surface`）落地后 m:n
声明会起量，物化缺腿将成为对象面唯一的收口洞。

## What Changes

- **物化端点放开 m:n**：`POST /api/v1/entities/{id}/relationships/{rel_id}/materialize`
  对 kind=m:n 不再 422，改经中间数据集展开关系对（桥行驱动的对表展开）。
- **引擎 `customer_dataset_link` 扩中间集参数**：同一工具加可选 junction 参数
  （单路径延伸，不造第二物化实现）；展开结果列＝源/目标两侧列，撞名沿 `right_`
  前缀，中间集列不进结果（纯路径）；n:1/1:1 行为零变化。
- **防行数爆炸双保险**：物化前按桥行数估算，超上限整批拒绝如实给数（子集保持
  原状）；展开实数仍受引擎既有行数闸（同 n:1 口径）。
- **档位闸三端**：源/目标/中间数据集三端均须 cell 档（任一免费档 403 如实）。
- **血缘与重放**：link 血缘 spec 记中间集三要素，`derive_refresh` 按 spec 重连
  （重物化语义与 n:1 一致）。
- **如实路径**：孤儿桥（引用失存的桥行）丢弃并计数如实上报；关系/数据集/列漂移
  404/4xx 如实零半写；旧 cell 镜像 link 无中间集支持→可读错误不伪造成功。
- **不做**：批量物化、共享源对象、m:n 的编辑/撤销（既有删除通道即语义）。

## Capabilities

### New Capabilities

（无——物化契约归 `entity-layer` 既有 capability）

### Modified Capabilities

- `entity-layer`：①「关系声明与纪律」——解除 m:n 物化端点 MUST NOT 支持条款
  （m:n 可声明、图谱、遍历、物化四处全通）；②「关系物化——数据集间语义连接」——
  增 m:n 桥展开路径（对表语义/孤儿桥如实/估算闸/三端档位闸/重放 spec 含中间集）。

## Impact

- **引擎（DAAS 仓协调）**：`customer_dataset_link` 加可选 junction 参数；移植到
  cell 镜像实际构建线 `internal-main` 后 rollout（Wave F 教训：只落 `main` 镜像内
  算子计数为 0）。
- **api**：materialize 端点 m:n 分支（三端校验、估算闸、血缘 spec）；零新迁移；
  wire-ops 工具面零变化（物化是 console 写面，沿「写不进工具面直调」红线）。
- **web**：实体页 m:n 关系「物化连接」解禁（原置灰态）；确认卡展开语义文案；
  管线图 spec 详情注明中间集；i18n zh/en；文档「实体」章节「物化」小节同批更新。
- **不改**：`wire_object_relations` m:n 遍历腿（v0 已通）、n:1/1:1 物化语义与逐字
  行为、档位规则、计量轨道、声明校验器。
