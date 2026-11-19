// 谦面自助分析工作区转介（协同变更 wire-daas-analysis-referral）。
//
// wire 目录是横向橱窗：指标条目来自概念目录同步管线，而「自助分析工作区」是
// 谦面提供的常驻能力条目——静态呈现，不参与目录的筛选/搜索/开通逻辑，其呈现
// 与同步源（fd-open-data-mcp concepts）是否含 DAAS 条目无关。
// 两条获取通道均指向既成公开面（URL 以常量维护，便于后续改指）：
// 壹座市场订阅谦面预设（零安装）与本地安装 fd-daas-mcp（一键私有部署）。

/** 壹座市场入口（订阅「数据-自助分析」等预设包，零安装即订即用）。 */
export const DAAS_MARKET_URL = "https://platform.finddatatech.cloud";

/** fd-daas-mcp 公开仓（install.sh 一键私有部署，数据主权在客户本机）。 */
export const DAAS_INSTALL_URL = "https://github.com/FindDataTechnology/fd-daas-mcp";