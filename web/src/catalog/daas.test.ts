// 自助分析工作区转介常量（协同变更 wire-daas-analysis-referral）：
// 外链目标单一事实源——组件与测试都从这里取，改指只动本文件。
import { describe, expect, it } from "vitest";
import { DAAS_MARKET_URL, DAAS_INSTALL_URL } from "./daas";

describe("DAAS 转介常量", () => {
  it("两条通道指向既成公开面", () => {
    expect(DAAS_MARKET_URL).toBe("https://platform.finddatatech.cloud");
    expect(DAAS_INSTALL_URL).toBe("https://github.com/FindDataTechnology/fd-daas-mcp");
  });

  it("均为 https 绝对地址", () => {
    for (const url of [DAAS_MARKET_URL, DAAS_INSTALL_URL]) {
      expect(url.startsWith("https://")).toBe(true);
    }
  });
});