"""cell-manager 异常体系。

全部异常继承 ``CellManagerError``——调用方（CLI/后台）只需捕获基类；
子类用于程序化区分：未知客户 / 非法状态流转 / 配置与驱动故障 / registry 契约故障。
"""


class CellManagerError(Exception):
    """cell-manager 基础错误。"""


class UnknownCellError(CellManagerError):
    """目标客户不存在任何 cell 记录（从未 provision 或已 purge）。"""


class InvalidCellStateError(CellManagerError):
    """当前 cell 状态不接受该操作（状态机拒绝，附明确指引）。"""


class InvalidCustomerIdError(CellManagerError):
    """customer_id 不合法（路径安全与容器命名约束）。"""


class ConfigError(CellManagerError):
    """环境配置缺失或取值非法。"""


class DriverError(CellManagerError):
    """容器驱动故障（docker SDK 未安装 / daemon 不可达 / 操作失败）。"""


class RegistryError(CellManagerError):
    """registry 借用契约调用失败（注册/摘除 server 条目）。"""
