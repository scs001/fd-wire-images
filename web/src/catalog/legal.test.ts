import { describe, expect, it } from "vitest";
import { isLegalIndicator, LEX_PLATFORM_URL } from "./legal";

describe("isLegalIndicator（wire-lex-referral 判定口径）", () => {
  it("域命中：归一后命中白名单", () => {
    for (const domain of ["legal", "law", "法律", " 法律 ", "LEGAL", "法律法规"]) {
      expect(isLegalIndicator({ domain, code: "X" })).toBe(true);
    }
  });

  it("代码前缀命中", () => {
    for (const code of [
      "law.guide_cases",
      "LAW_read",
      "flk.laws",
      "FLK_chunks",
      "wenshu.search",
      "wenshu_read",
      "guide_case_107",
      "legal_docs.get",
    ]) {
      expect(isLegalIndicator({ code, domain: null })).toBe(true);
    }
  });

  it("误伤边界：法人/法国类业务数据不命中", () => {
    expect(isLegalIndicator({ domain: "governance", code: "GOVCON2" })).toBe(false);
    expect(isLegalIndicator({ domain: "macro", code: "DC.DAC.FRAL.CD" })).toBe(false);
    expect(isLegalIndicator({ domain: "city", code: "city.x" })).toBe(false);
    expect(isLegalIndicator({})).toBe(false);
    expect(isLegalIndicator({ domain: null, code: null })).toBe(false);
  });

  it("平台常量", () => {
    expect(LEX_PLATFORM_URL).toBe("https://lex.finddatatech.cloud");
  });
});