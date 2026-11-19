// 标准本体只读区（add-bench-wire-seam-contract 任务 2.3/2.4）：
// - 有数据：每域一行元数据（版本 / digest 前 8 / 实体·关系规模 / 拉取时间）；
// - 空数组：空态「尚未拉取」，**不是**错误（两者视觉与文案都可区分）；
// - 403：ACL 报因（不是空态——否则用户会以为确实没拉过）；
// - 其它错误：报因卡带 HTTP 状态码。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { BenchOntologySection } from "./BenchOntologySection";
import { ConsoleApiError } from "./api";

const fetchOverview = vi.fn();

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    fetchBenchOntologyOverview: (...args: unknown[]) =>
      fetchOverview(...args),
  };
});

beforeEach(() => {
  i18n.changeLanguage("zh");
  fetchOverview.mockReset();
});
afterEach(() => cleanup());

describe("BenchOntologySection", () => {
  it("有数据时渲染每域元数据且不含正文", async () => {
    fetchOverview.mockResolvedValue({
      domains: [
        {
          domain_key: "legal",
          version: 2,
          digest: "5897354d" + "0".repeat(56),
          entity_count: 5117,
          relationship_count: 25,
          pulled_at: "2026-11-19T02:00:00",
        },
      ],
    });
    render(<BenchOntologySection />);
    expect(await screen.findByText("legal")).toBeTruthy();
    expect(screen.getByText("v2")).toBeTruthy();
    expect(screen.getByText("5897354d")).toBeTruthy(); // digest 前 8
    expect(screen.getByText("5117")).toBeTruthy();
    expect(screen.getByText("25")).toBeTruthy();
    expect(screen.getByText("2026-11-19T02:00:00")).toBeTruthy();
  });

  it("空数组显示「尚未拉取」空态而不是错误", async () => {
    fetchOverview.mockResolvedValue({ domains: [] });
    render(<BenchOntologySection />);
    expect(await screen.findByTestId("bench-ontology-empty")).toBeTruthy();
    expect(screen.queryByTestId("bench-ontology-error")).toBeNull();
  });

  it("403 显示 ACL 报因而非空态", async () => {
    fetchOverview.mockRejectedValue(new ConsoleApiError("forbidden", 403));
    render(<BenchOntologySection />);
    const card = await screen.findByTestId("bench-ontology-error");
    expect(card.textContent).toContain("无权查看标准本体");
    // 关键：403 不得渲染成「尚未拉取」——那会让用户以为确实没有
    expect(screen.queryByTestId("bench-ontology-empty")).toBeNull();
  });

  it("其它错误显示报因与 HTTP 状态码", async () => {
    fetchOverview.mockRejectedValue(new ConsoleApiError("boom", 500));
    render(<BenchOntologySection />);
    const card = await screen.findByTestId("bench-ontology-error");
    expect(card.textContent).toContain("HTTP 500");
  });
});
