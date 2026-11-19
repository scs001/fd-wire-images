// 渲染器测试（wire-rich-text-docs 任务 3.1-3.3 / 7.2 的组件级证据）。
//
// 最重要的三条：原始 HTML 不成为 DOM、脚本不执行、危险协议不成为可点击/
// 可加载源。这些不是「测了放心」——它们就是 spec 里「渲染安全底线」那条
// 需求的机器可验证形态。

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MarkdownView } from "./md";

describe("MarkdownView 正常渲染", () => {
  it("渲染标题/段落/列表/引用/代码", () => {
    const { container } = render(
      <MarkdownView
        source={"# 大标题\n\n一段正文。\n\n- 甲\n- 乙\n\n1. 一\n2. 二\n\n> 引用行\n\n```\ncode block\n```"}
      />,
    );
    expect(container.querySelector("h1")?.textContent).toBe("大标题");
    expect(container.querySelector("ul")?.children.length).toBe(2);
    expect(container.querySelector("ol")?.children.length).toBe(2);
    expect(container.querySelector("blockquote")?.textContent).toContain("引用行");
    expect(container.querySelector("pre")?.textContent).toContain("code block");
  });

  it("渲染粗体、斜体与行内代码", () => {
    const { container } = render(
      <MarkdownView source={"**粗** 与 *斜* 与 `code`"} />,
    );
    expect(container.querySelector("strong")?.textContent).toBe("粗");
    expect(container.querySelector("em")?.textContent).toBe("斜");
    expect(container.querySelector("code")?.textContent).toBe("code");
  });

  it("下划线强调不在词内生效：snake_case/dunder 标识符原样呈现（7.2 浏览器实抓）", () => {
    const { container } = render(
      <MarkdownView source={"设 window.__XSS_EXECUTED=1 与 user_id 都应原样。"} />,
    );
    // 不产生错误的 <em>（CommonMark 词边界纪律）
    expect(container.querySelector("em")).toBeNull();
    expect(container.textContent).toContain("window.__XSS_EXECUTED=1");
    expect(container.textContent).toContain("user_id");
  });

  it("词边界下划线强调照常生效（__强__/_斜_）", () => {
    const { container } = render(
      <MarkdownView source={"__强__ 与 _斜_"} />,
    );
    expect(container.querySelector("strong")?.textContent).toBe("强");
    expect(container.querySelector("em")?.textContent).toBe("斜");
  });

  it("http(s) 链接带 noopener noreferrer 且新窗口打开", () => {
    render(<MarkdownView source={"[外链](https://example.com/a)"} />);
    const link = screen.getByRole("link");
    expect(link.getAttribute("href")).toBe("https://example.com/a");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(link.getAttribute("rel")).toContain("noreferrer");
  });

  it("http(s) 外链图片渲染为 img 且带 alt", () => {
    const { container } = render(
      <MarkdownView source={"![示意图](https://example.com/p.png)"} />,
    );
    const img = container.querySelector("img");
    expect(img?.getAttribute("src")).toBe("https://example.com/p.png");
    expect(img?.getAttribute("alt")).toBe("示意图");
  });
});

describe("MarkdownView 安全底线（任务 3.1/3.2）", () => {
  it("原始 HTML 标签不成为 DOM 元素（仅文本）", () => {
    const { container } = render(
      <MarkdownView source={'<script>alert(1)</script>\n\n<div class="x">块级</div>'} />,
    );
    // 不产生 script/div 元素
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("div.x")).toBeNull();
    // 内容以纯文本如实呈现（不静默吞字符）
    expect(container.textContent).toContain("<script>alert(1)</script>");
    expect(container.textContent).toContain('<div class="x">块级</div>');
  });

  it("javascript: 链接不渲染为可点击元素", () => {
    const { container } = render(
      <MarkdownView source={"[点我](javascript:alert(1))"} />,
    );
    expect(container.querySelector("a")).toBeNull();
    // 文本保留（降级可见，不静默丢）
    expect(container.textContent).toContain("点我");
  });

  it("data: 图片源不渲染为 img", () => {
    const { container } = render(
      <MarkdownView source={"![x](data:text/html;base64,PHNjcmlwdD4=)"} />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("x");
  });

  it("相对路径链接放行（站内锚点/路径是安全的）", () => {
    const { container } = render(<MarkdownView source={"[锚](#section)"} />);
    expect(container.querySelector("a")?.getAttribute("href")).toBe("#section");
  });

  it("输出中不出现 dangerouslySetInnerHTML 的痕迹：注入的 <img onerror> 不成元素", () => {
    const { container } = render(
      <MarkdownView source={'<img src=x onerror="alert(1)">'} />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
  });
});

describe("MarkdownView 子集外语法如实降级（任务 3.3）", () => {
  it("表格与脚注按纯文本呈现，字符不丢", () => {
    const source = "| 列A | 列B |\n| --- | --- |\n| 1 | 2 |\n\n脚注[^1]\n\n[^1]: 注释";
    const { container } = render(<MarkdownView source={source} />);
    expect(container.querySelector("table")).toBeNull();
    // 竖线与方括号等字符原样可见
    expect(container.textContent).toContain("| 列A | 列B |");
    expect(container.textContent).toContain("[^1]");
  });

  it("未闭合围栏代码块容错（到文末收尾，不抛错）", () => {
    const { container } = render(<MarkdownView source={"```\n没有收尾"} />);
    expect(container.querySelector("pre")?.textContent).toContain("没有收尾");
  });

  it("空正文渲染为空容器（无报错）", () => {
    const { container } = render(<MarkdownView source={""} />);
    expect(container.textContent).toBe("");
  });
});
