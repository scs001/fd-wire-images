"""landing_tab 值域对拍（fix-landing-tab-market-drift）。

api `LANDING_TABS` 与 web `ConsoleTab` 联合类型必须同集——两处各自演化已
两次咬生产（workbench、market 切页签写回被 422 静默吞）。本测试把
preferences.py 注释里「冻结对表」变成可执行断言：读
web/src/console/ConsoleLayout.tsx 的 ConsoleTab 类型字面量与 api 枚举比对。
"""

import re
from pathlib import Path

from fd_wire_api.console.preferences import LANDING_TABS

LAYOUT = (
    Path(__file__).resolve().parents[2]
    / "web"
    / "src"
    / "console"
    / "ConsoleLayout.tsx"
)


def test_landing_tabs_lockstep_with_web_console_tab():
    assert LAYOUT.exists(), f"web ConsoleLayout 不在场：{LAYOUT}"
    union = re.search(
        r"export type ConsoleTab =\s*((?:[ \t]*\|[^\n]+\n)+)",
        LAYOUT.read_text(encoding="utf-8"),
    )
    assert union, "ConsoleTab 联合类型未找到——ConsoleLayout.tsx 结构变了须同步本测试"
    web_tabs = set(re.findall(r'"([a-z_]+)"', union.group(1)))
    assert web_tabs == set(LANDING_TABS), (
        "api/web landing_tab 值域漂移："
        f"api 独有={sorted(set(LANDING_TABS) - web_tabs)} "
        f"web 独有={sorted(web_tabs - set(LANDING_TABS))}"
    )


def test_parse_console_tab_covers_same_domain():
    """App.tsx parseConsoleTab 是页签值域的「第五本手抄账」——新增页签漏改会
    静默回落 datasets（App.tsx 回落兜底）。把它的字面量清单也钉进对拍：
    与 ConsoleTab 联合类型同集才算数（fix-landing-tab-market-drift 后续）。"""
    app = Path(__file__).resolve().parents[2] / "web" / "src" / "App.tsx"
    assert app.exists(), f"App.tsx 不在场：{app}"
    src = app.read_text(encoding="utf-8")
    body = re.search(
        r"function parseConsoleTab\(segment: string \| undefined\): ConsoleTab \{(.*?)\n\}",
        src,
        re.S,
    )
    assert body, "parseConsoleTab 未找到——App.tsx 结构变了须同步本测试"
    listed = set(re.findall(r'segment === "([a-z_]+)"', body.group(1)))
    assert listed == set(LANDING_TABS), (
        "parseConsoleTab 值域漂移（漏改会静默回落 datasets）："
        f"缺失={sorted(set(LANDING_TABS) - listed)} 多余={sorted(listed - set(LANDING_TABS))}"
    )
