## MODIFIED Requirements

### Requirement: 免费档边界

免费注册默认免费档：获得 wgk- key 与 1,000 行/月读取额度，不配给 cell；「我的数据」
以 api 层轻量数据集放行（上传 CSV、限额 3 集 × 10,000 行、REST 查询聚合，见
`free-tier-data` 能力），cell 物化面（连接器/派生/批量写/MCP/AI）仍需付费档。
cell 自首个付费档（基础档）起配。

#### Scenario: 免费用户无 cell
- **WHEN** 免费档用户打开「我的数据」
- **THEN** 呈现 api 层数据集列表与上传入口（配额内联），且不为该用户创建任何 cell 资源

#### Scenario: 免费物化面标注付费
- **WHEN** 免费档用户尝试连接器或派生入口
- **THEN** 入口可见但请求被档位闸拒绝并给出升档说明
