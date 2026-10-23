// 证据引用渲染（wire-analysis-console 4.7）：片段、来源、原文链接与不一致标注。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { EvidenceList } from "./EvidenceList";

const rows = [
  {
    index: 0,
    claim: "公司拟收购",
    source: "cninfo",
    stable_id: "cninfo:1",
    as_of: "2026-10-20",
    start: 5,
    end: 13,
    quote: "拟以十二亿元收购",
    url: "https://example.com/a.pdf",
    quote_ok: true,
    error: null,
  },
  {
    index: 1,
    claim: null,
    source: "cn-gov",
    stable_id: "cn-gov:2",
    as_of: "2026-10-21",
    start: 0,
    end: 2,
    quote: "通知",
    url: null,
    quote_ok: false,
    error: "quote 与原文片段不一致",
  },
];

vi.mock("./api", () => ({
  fetchArtifactEvidence: vi.fn(async () => rows),
}));

beforeEach(() => i18n.changeLanguage("zh"));
afterEach(() => cleanup());

describe("EvidenceList", () => {
  it("渲染片段并用 Text Fragment 链接回原文", async () => {
    render(<EvidenceList artifactId={1} />);
    expect(await screen.findByText("拟以十二亿元收购")).toBeTruthy();
    const link = screen.getByRole("link", { name: "打开原文" });
    expect(link.getAttribute("href")).toBe(
      `https://example.com/a.pdf#:~:text=${encodeURIComponent("拟以十二亿元收购")}`,
    );
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("片段不一致与原文链接缺失都如实标注", async () => {
    render(<EvidenceList artifactId={1} />);
    expect(await screen.findByText("片段与原文不一致")).toBeTruthy();
    expect(screen.getByText("原文链接不可用")).toBeTruthy();
    expect(screen.getAllByTestId("artifact-evidence-item")).toHaveLength(2);
  });
});
