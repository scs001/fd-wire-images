// 页脚组件测试（wire-ux-consolidation 任务 1.1）：
// - 全量页脚（门户）：署名（年份+组织+官网口径标语）、GitHub、ICP/公安备
//   （备案号原文 + 备案链接新窗口）、跨服务入口恰好四个活入口；
// - 紧凑页脚（控制台）：单行 版权+ICP+公安备，无跨服务行、无 GitHub。
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { SiteFooter } from "./SiteFooter";
import {
  FOOTER_GITHUB_URL,
  FOOTER_ICP_NUMBER,
  FOOTER_ICP_URL,
  FOOTER_MPS_NUMBER,
  FOOTER_MPS_URL,
} from "./footerConstants";

beforeEach(() => {
  i18n.changeLanguage("zh");
});

afterEach(() => {
  cleanup();
});

describe("SiteFooter（门户全量页脚）", () => {
  it("署名含年份与组织（寻数科技），标语口径同官网 footer.line", () => {
    render(<SiteFooter />);
    const org = screen.getByTestId("site-footer-org");
    expect(org.textContent).toContain(String(new Date().getFullYear()));
    expect(org.textContent).toContain("寻数科技 FindData Technology");
    expect(org.textContent).toContain(
      "服务 AI 时代的文本与数据基础设施与工具。",
    );
  });

  it("GitHub 链接指向组织页（新窗口）", () => {
    render(<SiteFooter />);
    const github = screen.getByTestId("footer-github");
    expect(github.getAttribute("href")).toBe(FOOTER_GITHUB_URL);
    expect(github.getAttribute("target")).toBe("_blank");
  });

  it("ICP 与公安备：备案号原文 + 备案链接（新窗口）", () => {
    render(<SiteFooter />);
    const icp = screen.getByTestId("footer-icp");
    expect(icp.textContent).toBe(FOOTER_ICP_NUMBER);
    expect(icp.getAttribute("href")).toBe(FOOTER_ICP_URL);
    expect(icp.getAttribute("target")).toBe("_blank");
    const mps = screen.getByTestId("footer-mps");
    expect(mps.textContent).toBe(FOOTER_MPS_NUMBER);
    expect(mps.getAttribute("href")).toBe(FOOTER_MPS_URL);
    expect(mps.getAttribute("target")).toBe("_blank");
    expect(FOOTER_ICP_NUMBER).toBe("粤ICP备2026118740号-1");
    expect(FOOTER_MPS_NUMBER).toBe("粤公网安备44030002016558号");
  });

  it("跨服务入口恰好四个活入口：官网/壹座/识律/更多产品线", () => {
    render(<SiteFooter />);
    const expectService = (key: string, href: string) => {
      const link = screen.getByTestId(`footer-service-${key}`);
      expect(link.getAttribute("href")).toBe(href);
      expect(link.getAttribute("target")).toBe("_blank");
    };
    expectService("official", "https://finddatatech.cloud");
    expectService("base", "https://platform.finddatatech.cloud");
    expectService("lex", "https://lex.finddatatech.cloud");
    expectService("more", "https://finddatatech.cloud/products");
    const services = screen
      .getAllByTestId(/^footer-service-/)
      .map((el) => el.getAttribute("data-testid"));
    expect(services).toHaveLength(4);
  });
});

