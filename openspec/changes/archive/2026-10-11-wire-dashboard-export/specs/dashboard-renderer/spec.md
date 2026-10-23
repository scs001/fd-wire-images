# dashboard-renderer 变更（wire-dashboard-export）

## ADDED Requirements

### Requirement: 看板导出

控制台查看态提供「导出 CSV」：整板单文件（逐面板 section=标题注释行 +
列头 + 数据行 + 空行），数据来自面板快照本地生成（零请求零新端点）；
RFC4180 转义 + UTF-8 BOM；无面板/空快照如实导出空结构不报错；分享页与
渲染器本体零导出入口（公开只读红线）。

#### Scenario: 整板导出
- **WHEN** 客户在看板查看态点击导出
- **THEN** 下载单 CSV 含全部面板的 section 数据，与所见快照一致

#### Scenario: 分享页无导出
- **WHEN** 打开公开分享页
- **THEN** 无任何导出入口（渲染器不携带导出 UI）

#### Scenario: 空板如实
- **WHEN** 看板无有效面板或快照全空
- **THEN** 导出仅含标题的空结构文件，不报错
