// i18n 双语键位一致性：zh 与 en 的键集合必须完全一致（全键化、无缺漏）。
import { describe, expect, it } from "vitest";
import zh from "./zh";
import en from "./en";

function keyPaths(value: unknown, prefix = ""): string[] {
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>).flatMap(([k, v]) =>
      keyPaths(v, prefix ? `${prefix}.${k}` : k),
    );
  }
  return [prefix];
}

describe("i18n zh/en 键位一致", () => {
  it("两语言键集合完全一致", () => {
    const zhKeys = new Set(keyPaths(zh));
    const enKeys = new Set(keyPaths(en));
    const missingInEn = [...zhKeys].filter((k) => !enKeys.has(k));
    const missingInZh = [...enKeys].filter((k) => !zhKeys.has(k));
    expect(missingInEn).toEqual([]);
    expect(missingInZh).toEqual([]);
  });

  it("叶子值均为非空字符串", () => {
    for (const [path, value] of [...leafEntries(zh), ...leafEntries(en)]) {
      expect(
        typeof value === "string" && value.trim().length > 0,
        `${path} 应为非空字符串`,
      ).toBe(true);
    }
  });

  it("文案不携带手写指标计数字面量（目录规模数字一律活取）", () => {
    // 目录规模呈现键必须是 {{total}} 模板 + 无数字回退键
    expect(zh.home.value1Title).toBe("{{total}}+ 指标目录");
    expect(zh.home.value1TitleNoCount).not.toMatch(/\d/);
    expect(zh.pricing.featCatalog).toBe("{{total}}+ 指标目录全量浏览");
    expect(zh.pricing.featCatalogNoCount).not.toMatch(/\d/);
    expect(en.home.value1Title).toBe("{{total}}+ indicator catalog");
    expect(en.home.value1TitleNoCount).not.toMatch(/\d/);
    expect(en.pricing.featCatalog).toBe(
      "Full access to the {{total}}+ indicator catalog",
    );
    expect(en.pricing.featCatalogNoCount).not.toMatch(/\d/);
    // 全量扫描：任何叶子文案都不出现旧手写数字（57,000 / 5.7万 等）
    for (const [path, value] of [...leafEntries(zh), ...leafEntries(en)]) {
      expect(
        /57[,.]?000|5\.7\s*万/.test(String(value)),
        `${path} 携带手写指标数字`,
      ).toBe(false);
    }
  });
});

function leafEntries(
  value: unknown,
  prefix = "",
): Array<[string, unknown]> {
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>).flatMap(([k, v]) =>
      leafEntries(v, prefix ? `${prefix}.${k}` : k),
    );
  }
  return [[prefix, value]];
}
