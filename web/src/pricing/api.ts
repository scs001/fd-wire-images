// 公开档位 API 客户端（wire-self-serve-launch 任务 7.1，portal-pricing）。
// - GET /api/v1/plans（公开只读，仅 is_active 的公开档）→
//   {"plans":[{"code","monthly_price_yuan","included_lines","sort"}]}。
// - 定价页/首页活取的唯一事实源：源码不携带档位价格与行数面量（spec 场景
//   「源码无档位面量」）；请求失败由调用方回退为无数字卡片。
// - 价格符号两语言统一 ¥（与 ZH 一致，避免双货币口径，design.md D4）。
import { API_BASE } from "../catalog/api";

export interface PublicPlan {
  code: string;
  monthly_price_yuan: number;
  included_lines: number;
  sort: number;
}

export interface PublicPlansResponse {
  plans: PublicPlan[];
}

/** 公开档位 URL（VITE_API_BASE 覆盖，默认 /api 同源——沿用 catalog/api 范式）。 */
export function buildPlansUrl(): string {
  return `${API_BASE}/v1/plans`;
}

export class PlansApiError extends Error {
  readonly status: number | undefined;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "PlansApiError";
    this.status = status;
  }
}

/** 公开档位列表（按 sort 升序；仅服务端筛选后的公开档）。 */
export async function fetchPublicPlans(): Promise<PublicPlan[]> {
  let response: Response;
  try {
    response = await fetch(buildPlansUrl(), {
      headers: { Accept: "application/json" },
    });
  } catch (err) {
    throw new PlansApiError(`network error fetching plans: ${(err as Error).message}`);
  }
  if (!response.ok) {
    throw new PlansApiError(`plans api ${response.status}`, response.status);
  }
  try {
    const body = (await response.json()) as PublicPlansResponse;
    return sortPlans(body.plans ?? []);
  } catch (err) {
    throw new PlansApiError(`invalid json from plans: ${(err as Error).message}`);
  }
}

/** sort 升序（同 sort 按 code 稳定排序，渲染顺序可预期）。 */
export function sortPlans(plans: PublicPlan[]): PublicPlan[] {
  return [...plans].sort((a, b) => a.sort - b.sort || a.code.localeCompare(b.code));
}

/** 月费呈现：¥ + 千分位（数据来自端点，非源码面量；两语言统一 ¥）。 */
export function formatPlanPriceYuan(yuan: number): string {
  return `¥${yuan.toLocaleString("en-US")}`;
}

/** 含额行数千分位呈现（数据来自端点）。 */
export function formatIncludedLines(lines: number): string {
  return lines.toLocaleString("en-US");
}
