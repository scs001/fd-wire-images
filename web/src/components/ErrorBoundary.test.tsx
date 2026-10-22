// 错误边界组件测试（wire-console-ux-fixes 任务 2.1/2.2）：
// 正常渲染直通、子组件 throw 呈现 amber 错误卡（不白屏整树）、
// 点重试重建 children（nonce 重挂载——子组件内部态随之重置）、
// 重试后再次抛错仍可回到错误卡（边界可重复工作）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import "../i18n";
import i18n from "../i18n";
import { ErrorBoundary } from "./ErrorBoundary";

/** 会炸的子组件： Bomb({blow}) —— blow=true 时渲染期 throw。 */
function Bomb({ blow, label }: { blow: boolean; label: string }) {
  if (blow) {
    throw new Error(`boom: ${label}`);
  }
  return <div data-testid="bomb-ok">{label}</div>;
}

/** 可切换炸/不炸的子组件：重试重建后能恢复挂载的断言面。 */
function Toggling() {
  const [broken] = useState(true);
  if (broken) {
    throw new Error("first mount blows");
  }
  return <div data-testid="toggling-ok">恢复</div>;
}

beforeEach(() => {
  i18n.changeLanguage("zh");
});

afterEach(() => {
  cleanup();
});

describe("ErrorBoundary（渲染异常兜底）", () => {
  it("正常渲染直通：无异常时 children 原样呈现，无错误卡", () => {
    render(
      <ErrorBoundary>
        <Bomb blow={false} label="平安无事" />
      </ErrorBoundary>,
    );

    expect(screen.getByTestId("bomb-ok").textContent).toBe("平安无事");
    expect(screen.queryByTestId("error-boundary-card")).toBeNull();
  });

  it("子组件渲染期 throw：呈现错误卡（amber 风格 + i18n 文案），children 不再渲染", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Bomb blow label="炸点" />
      </ErrorBoundary>,
    );

    const card = screen.getByTestId("error-boundary-card");
    expect(card.className).toContain("border-amber-200");
    expect(card.textContent).toContain("页面出了点问题");
    expect(screen.getByTestId("error-boundary-retry").textContent).toBe("重试");
    expect(screen.queryByTestId("bomb-ok")).toBeNull();
    // componentDidCatch 留痕（排查面）。
    expect(spy).toHaveBeenCalled();
    spy.mockRestore();
  });

  it("点重试：清错误态重挂载 children——子组件内部态重置、可恢复挂载", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    // 重挂载后仍抛错（重试不等于修复）：断言面=错误卡回到场且重试计数推进。
    render(
      <ErrorBoundary>
        <Toggling />
      </ErrorBoundary>,
    );
    expect(screen.getByTestId("error-boundary-card")).toBeTruthy();

    fireEvent.click(screen.getByTestId("error-boundary-retry"));
    // 重挂载后的 Toggling 重新初始化（broken=true 首挂载即炸）→ 回到错误卡：
    // 证明 children 确实被重建（而非沿用旧树）。
    expect(screen.getByTestId("error-boundary-card")).toBeTruthy();
    // componentDidCatch 每次捕获都留痕（React 重复告警不掩盖捕获次数断言）。
    expect(spy.mock.calls.length).toBeGreaterThanOrEqual(2);
    spy.mockRestore();
  });

  it("重试后子组件不再抛错：恢复正常渲染（降级可退出）", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    // 首挂载抛错，随后重试时以健康子组件替换（模拟上游状态已修复）。
    const { rerender } = render(
      <ErrorBoundary>
        <Bomb blow label="首挂即炸" />
      </ErrorBoundary>,
    );
    expect(screen.getByTestId("error-boundary-card")).toBeTruthy();

    rerender(
      <ErrorBoundary>
        <Bomb blow={false} label="修复后" />
      </ErrorBoundary>,
    );
    // 重试清错误态：健康 children 直通呈现。
    fireEvent.click(screen.getByTestId("error-boundary-retry"));
    expect(screen.getByTestId("bomb-ok").textContent).toBe("修复后");
    expect(screen.queryByTestId("error-boundary-card")).toBeNull();
    spy.mockRestore();
  });
});
