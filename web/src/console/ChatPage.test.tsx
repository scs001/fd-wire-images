// AI 构建页测试（wire-assistant-panel）：该页已从「壹座试用引导卡」改为助手
// 会话的整页形态——页内可对话（不再只是跳转），壹座深链仍保留（新窗口），
// 且页面文案不含任何测试账号明文。
//
// 原「零网络请求」「无输入框」两项断言随能力反转而退役：本变更的整个前提
// 就是客户能在这里直接说话（见 spec console-ai-entry 的 MODIFIED/ADDED 需求）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { ChatPage } from "./ChatPage";

beforeEach(() => {
  i18n.changeLanguage("zh");
  sessionStorage.clear();
  window.location.hash = "";
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  window.location.hash = "";
});

describe("ChatPage（助手整页形态）", () => {
  it("页内可对话：存在输入框与发送按钮", () => {
    render(<ChatPage planCode="basic" />);

    expect(screen.getByTestId("chat-page")).toBeTruthy();
    expect(screen.getByTestId("assistant-input")).toBeTruthy();
    expect(screen.getByTestId("assistant-send")).toBeTruthy();
  });

  it("保留壹座深链（新窗口，不内嵌）", () => {
    const { container } = render(<ChatPage planCode="basic" />);

    const link = screen.getByTestId("chat-page-base-link");
    expect(link.getAttribute("href")).toBe("https://platform.finddatatech.cloud");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    // 不内嵌壹座：页面里没有 iframe
    expect(container.querySelector("iframe")).toBeNull();
  });

  it("页面文案不含任何测试账号明文", () => {
    const { container } = render(<ChatPage planCode="basic" />);
    const text = container.textContent ?? "";
    expect(text).not.toContain("platform-test");
    expect(text).not.toContain("@");
    expect(text).not.toMatch(/密码|口令|password/i);
  });

  it("免费档：渲染能力说明徽章，无模式切换（统一副驾）", () => {
    render(<ChatPage planCode="free" />);

    expect(screen.getByTestId("assistant-free-badge")).toBeTruthy();
    expect(screen.queryByTestId("assistant-mode-switch")).toBeNull();
    expect(screen.queryByTestId("assistant-mode-build")).toBeNull();
  });

  it("付费档：同样无模式切换（问/做合并为单一副驾）", () => {
    render(<ChatPage planCode="basic" />);

    expect(screen.queryByTestId("assistant-mode-switch")).toBeNull();
    expect(screen.queryByTestId("assistant-free-badge")).toBeNull();
  });

  it("英文文案就位", () => {
    i18n.changeLanguage("en");
    render(<ChatPage planCode="basic" />);
    expect(screen.getByTestId("chat-page").textContent).toContain("AI Assistant");
  });
});
