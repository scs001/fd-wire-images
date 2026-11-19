"""源包定义（wire-source-packs）：静态配置 + 目录实时切片，不落库。

首批 8 个包。包 = 指标目录（indicator_snapshots）的一个主题切片：
``filter`` 是目录查询条件（{domains, source_dbs, codes} 任一组合），包的
指标数与清单**实时按 filter 从目录镜像现算**（目录即真相，无快照表、无
同步任务）。

domain 取值约束：只使用**生产镜像里真实存在的 domain 值**（依据
production 语义下的 GROUP BY 分布核对，见各包注释）；目录后续扩充 domain
时包定义随之演进（filter 命中空集 = 该包计数为 0，属诚实呈现）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PackDefinition:
    """一个源包：双语名称/描述 + 目录切片 filter。"""

    id: str
    name_zh: str
    name_en: str
    desc: str
    filter: dict = field(default_factory=dict)


#: 首批源包（顺序即控制台卡片墙展示序）。
PACKS: tuple[PackDefinition, ...] = (
    PackDefinition(
        id="macro-prices",
        name_zh="宏观价格",
        name_en="Macro Prices",
        desc="居民消费价格（CPI）、工业生产者出厂价格（PPI）等价格类指数——通胀观察的核心切片。",
        filter={"domains": ["价格指数"]},
    ),
    PackDefinition(
        id="population-employment",
        name_zh="人口与就业",
        name_en="Population & Employment",
        desc="就业人数、失业率等劳动力市场指标。",
        filter={"domains": ["就业"]},
    ),
    PackDefinition(
        id="money-finance",
        name_zh="货币金融",
        name_en="Money & Finance",
        desc="货币供应量（M0/M1/M2）、央行与市场利率、数字资产行情。",
        filter={"domains": ["货币金融", "利率", "加密货币"]},
    ),
    PackDefinition(
        id="foreign-trade",
        name_zh="对外贸易",
        name_en="Foreign Trade",
        desc="进出口增速、外商直接投资（FDI）等跨境经贸指标。",
        filter={"domains": ["对外贸易"]},
    ),
    PackDefinition(
        id="industry-activity",
        name_zh="行业景气",
        name_en="Industry Activity",
        desc="PMI 景气指数、工业增加值、能源消费、房地产与国内贸易——行业冷暖面。",
        filter={"domains": ["PMI", "工业", "能源", "房地产", "国内贸易"]},
    ),
    PackDefinition(
        id="regional-economy",
        name_zh="区域经济",
        name_en="Regional Economy",
        desc="城市级面板与城市指标：城市 GDP、人口、房价、失业率等区域切片。",
        filter={"domains": ["city", "城市指标"]},
    ),
    PackDefinition(
        id="yearbook-panorama",
        name_zh="年鉴全景",
        name_en="Yearbook Panorama",
        desc="统计年鉴全量目录：宏观、人口、工业、能源……最完整的年度指标全景。",
        filter={"source_dbs": ["yearbook_catalog"]},
    ),
    PackDefinition(
        id="open-data-panorama",
        name_zh="开放数据全景",
        name_en="Open Data Panorama",
        desc="fd-open-data 全源目录：宏观、行情、基金、文档与技术指标等开放切片。",
        filter={"source_dbs": ["fd_open_data"]},
    ),
)

#: id → 定义（路由 404 判定与订阅写入口的合法包集合）。
PACKS_BY_ID: dict[str, PackDefinition] = {p.id: p for p in PACKS}


def get_pack(pack_id: str) -> PackDefinition | None:
    """按 id 取包定义；未知返回 None。"""
    return PACKS_BY_ID.get(pack_id)
