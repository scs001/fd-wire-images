"""REST 数据面（wire-customer-data-engine §5，capability wire-data-plane）。

三件套：key 生命周期（service）、双凭据鉴权（auth）、端点（routes=管理面
与后台只读视图，data_routes=数据面读写端点）。词汇与红线见 fd-wire
CONTEXT.md「数据面（data plane）」「数据面 key（data-plane key）」。
"""
