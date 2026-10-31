# wire-sensitivity-market-closure

## Why

产物面（看板/报告）的敏感度双点闸目前只查**直接绑定数据集自身**的敏感度
（`_sensitivity_of` 直查 meta）——敏感祖先链上的派生子集绑进看板后可设 market
并渲染，与数据集面的闭包闸（根仓 `wire-sensitivity-propagation`）形成旁路。
本 change 把产物面两处复检口径扩为**有效敏感度**（祖先闭包），消费根仓
change 交付的同一 helper，零第二实现。

## What Changes

- **发布闸**（产物可见性档位）：「绑定数据集含『敏感』档」的判定从直接绑定
  扩为绑定数据集的**有效敏感度**（含祖先闭包继承）——祖先敏感的派生子集
  绑进看板后，设 `market` 同样 422；`link`/`directed` 不受影响（口径不变）。
- **渲染复检**（Q5 双点闸渲染点）：渲染时复检同样按有效敏感度；发布后被
  祖先敏感化连带的绑定数据集，订阅者渲染 SHALL 被拒（不静默出数据）。
- 复用根仓 `wire-sensitivity-propagation` 的 `effective_sensitivity` helper
  （同源同实现，零漂移）；错误文案如实指名敏感祖先链。
- 本 change **不改**：产物四档语义、订阅/审批、快照披露、计量、
  helper 本身（在根仓 change 交付）。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `artifact-marketplace`: ①「产物可见性档位」发布闸的敏感判定扩为有效敏感度
  （祖先闭包）；②「只读看（订阅者渲染）」渲染复检同口径扩展。

## Impact

- **api**：`market/artifact_service.py` 的 `_sensitivity_of` 换用有效敏感度 helper
  （根仓 change 交付）；发布闸与渲染复检两调用点随 helper 自动获得闭包口径。
- **依赖**：根仓 `wire-sensitivity-propagation` 先行或同批（helper 是前置）。
- **不改**：web（错误文案已指名数据集，前端无结构变化）、快照/订阅语义。
