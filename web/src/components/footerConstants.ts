// 页脚共享常量（wire-ux-consolidation 任务 1.1）：备案号与跨服务入口集中
// 一处，全量页脚（门户）与紧凑页脚（控制台）共用；壹座直达链接供
// AI 构建引导卡（ChatPage）与工作台入口卡复用（直落 platform——
// 壹座与柏讯共用同一 Logto IdP，不经官网 demo 页中转）。
export const FOOTER_ICP_NUMBER = "粤ICP备2026118740号-1";
export const FOOTER_ICP_URL = "https://beian.miit.gov.cn/";
export const FOOTER_MPS_NUMBER = "粤公网安备44030002016558号";
export const FOOTER_MPS_URL =
  "https://beian.mps.gov.cn/#/query/webSearch?code=44030002016558";
export const FOOTER_GITHUB_URL = "https://github.com/FindDataTechnology";

export const OFFICIAL_SITE_URL = "https://finddatatech.cloud";
export const YIZUO_PLATFORM_URL = "https://platform.finddatatech.cloud";
export const LEX_PLATFORM_URL = "https://lex.finddatatech.cloud";
export const MORE_PRODUCTS_URL = "https://finddatatech.cloud/products";
/** 全量页脚的跨服务入口行——只放有独立门户的活入口（任务 1.1 口径）。 */
export const FOOTER_SERVICE_LINKS: ReadonlyArray<{
  key: "official" | "base" | "lex" | "more";
  href: string;
}> = [
  { key: "official", href: OFFICIAL_SITE_URL },
  { key: "base", href: YIZUO_PLATFORM_URL },
  { key: "lex", href: LEX_PLATFORM_URL },
  { key: "more", href: MORE_PRODUCTS_URL },
];
