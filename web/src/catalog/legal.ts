// 法律域判定与识律转介（协同变更 wire-lex-referral）。
//
// wire 目录是横向橱窗：命中法律域判定的条目保留可见，但开通动线转介识律平台
// （法律域数据不由 wire 供给——任何 status 一律转介，避免"绿色可读"的假象）。
// 判定口径保守且文档化（见 openspec/specs/wire-indicator-catalog）：误伤面
// 最坏只是多一个外链。

/** 识律平台入口（纯外链，目标可达性与目录呈现解耦）。 */
export const LEX_PLATFORM_URL = "https://lex.finddatatech.cloud";

const LEGAL_DOMAINS = new Set(["legal", "law", "法律", "法律法规"]);
const LEGAL_CODE_PREFIXES = [
  "law.",
  "law_",
  "flk.",
  "flk_",
  "wenshu.",
  "wenshu_",
  "guide_case",
  "legal.",
  "legal_",
];

export function isLegalIndicator(input: {
  domain?: string | null;
  code?: string | null;
}): boolean {
  const domain = (input.domain ?? "").trim().toLowerCase();
  if (domain && LEGAL_DOMAINS.has(domain)) return true;
  const code = (input.code ?? "").trim().toLowerCase();
  return LEGAL_CODE_PREFIXES.some((prefix) => code.startsWith(prefix));
}