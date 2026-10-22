// 内部后台 API 客户端（wire-platform-v1 任务 7.1 web 面）。
// - 端点族：/internal/admin/*（api 仓 fd_wire_api.admin 路由）——客户管理 /
//   账本 / 需求单派发 / 目录治理 / 用量分轨。
// - 鉴权（admin 双闸，wire-self-serve-launch 任务 5.2）：请求自动附
//   Authorization: Bearer <ID token>（Logto 登录会话，服务端校验 wire-admin
//   角色）；第二道 X-Admin-Token 闸由 web 容器 nginx 在 /internal/ 反代上
//   服务端注入（env FD_WIRE_ADMIN_TOKEN）——浏览器永不持有该值，开发占位的
//   DEFAULT_ADMIN_TOKEN / localStorage 令牌存储已整体退场。
// - 基址：VITE_ADMIN_API_BASE 显式覆盖；缺省从 API_BASE 剥掉结尾 /api 推导
//   （默认同源根——dev 本地直连可设 VITE_ADMIN_API_BASE=http://127.0.0.1:8000）。
// - 错误如实：非 2xx 一律抛 AdminApiError（携带 status 与后端 detail），
//   UI 原样呈现（401/403 = 鉴权失败态），绝不静默吞掉。
import { API_BASE } from "../catalog/api";
import { loadSession } from "../console/auth";

function resolveAdminBase(): string {
  const explicit = String(import.meta.env.VITE_ADMIN_API_BASE ?? "").trim();
  if (explicit) return explicit;
  // "/api"（同源代理前缀）→ ""；"https://host/api" → "https://host"
  return API_BASE.replace(/\/api\/?$/, "");
}

export const ADMIN_API_BASE = resolveAdminBase();

/** 登录会话 Bearer 头（无会话即 401 如实上抛；不携带任何手填令牌）。 */
function bearerHeaders(): Record<string, string> {
  const session = loadSession();
  if (!session?.idToken) {
    throw new AdminApiError("no console session (sign in required)", 401);
  }
  return { Authorization: `Bearer ${session.idToken}` };
}

// ---------------------------------------------------------------------------
// 类型（对齐 api 仓 fd_wire_api/admin/schemas.py 与 demands.schemas）
// ---------------------------------------------------------------------------

export interface AdminPlan {
  id: number;
  code: string;
  name: string;
  monthly_price_micro: number;
  monthly_price_yuan: string;
  included_lines: number;
  overage_unit_price_micro: number;
  is_active: boolean;
}

export interface AdminCustomer {
  id: number;
  name: string;
  /** v1 兼作计量身份映射（metering caller 名） */
  sub2api_user_id: string | null;
  plan_id: number;
  plan_code: string;
  plan_name: string;
  status: string;
  created_at: string;
}

export interface UsagePeriodRow {
  period: string;
  calls: number;
  rows: number;
}

export interface CustomerUsage {
  customer_id: number;
  customer_name: string;
  caller: string | null;
  credential_track: string;
  totals: UsagePeriodRow;
  periods: UsagePeriodRow[];
}

export type LedgerKind =
  | "grant"
  | "overage"
  | "hardstop"
  | "demand"
  | "adjustment";

export interface LedgerEntry {
  id: number;
  customer_id: number;
  kind: LedgerKind;
  quantity: number;
  amount_micro: number;
  amount_yuan: string;
  amount_yuan_str: string;
  period: string;
  idempotency_key: string;
  source_ref: string | null;
  sub2api_applied: boolean;
  created_at: string;
}

/** 需求单状态（三态机 + rejected；八态工作流的五个旧态已废除，前端不出现）。 */
export type DemandStatus =
  | "submitted"
  | "in_progress"
  | "delivered"
  | "rejected";

export interface DemandHistoryEvent {
  status: string;
  at: string;
  by: string;
  note: string | null;
}

export interface AdminDemand {
  id: number;
  code: string;
  customer_id: string;
  title: string;
  intent: string;
  status: DemandStatus;
  status_history: DemandHistoryEvent[];
  evaluation_summary: string | null;
  rejection_reason: string | null;
  quote_amount: string | null;
  quote_note: string | null;
  /** 流水线事件全文（append-only 内部留档；at=落库盖章的 ISO 时间戳，
   *  type 见 api 仓 demands/orchestration.py 与 orchestration_sync.py）。 */
  pipeline_events: Array<Record<string, unknown>>;
  /** 交付事件全文（append-only 内部留档；进度面只呈现 scope 状态，不渲染全文）。 */
  delivery_events: Array<Record<string, unknown>>;
  /** gate 门禁摘要（Demand.gate 计算属性：通过/尝试/流水线运行数/最近一次）。 */
  gate: {
    passed: boolean;
    attempts: number;
    pipeline_runs: number;
    latest: unknown;
  } | null;
  /** 私有 scope 交付状态：null=未发起 / "ok"=已就绪 / "pending"=挂起待重试。 */
  scope_status: string | null;
  /** 已建客户私有 scope 名（如 demand-<code>）。 */
  scope_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface DemandTransitionInput {
  target_status: DemandStatus;
  note?: string;
  by?: string;
  reason?: string;
}

/** 「转生成工单」结果（与内部端点共用同一 service 的响应形状）。 */
export interface AdminWorkorderResult {
  ticket_yaml: string;
  ticket_path: string;
  delivered: boolean;
  queued_for_pull: boolean;
  slug_check: "free" | "unverified";
}

export interface IndicatorPendingItem {
  indicator_id: string;
  code: string;
  name_zh: string | null;
  name_en: string | null;
  domain: string | null;
  frequency: string | null;
  status: string;
  synced_at: string;
}

export interface CatalogPendingResponse {
  status: string;
  /** 本页条目（服务端按 limit/offset 切片后的结果，不再是全量）。 */
  items: IndicatorPendingItem[];
  /** 匹配总数（不是本页条数）——页数按它计算，故只需一次响应即可翻页。 */
  total: number;
}

/** 目录治理列表分页参数（后端 limit/offset，单页上限 100）。 */
export interface CatalogPendingPage {
  limit?: number;
  offset?: number;
}

export interface ReviewNote {
  id: number;
  indicator_id: string;
  note: string;
  author: string;
  created_at: string;
}

export interface AggRow {
  key: string;
  calls: number;
  rows: number;
}

export interface UsageOverview {
  credential_track: string | null;
  start: string | null;
  end: string | null;
  tracks: AggRow[];
  by_source: AggRow[];
  totals: AggRow;
}

// ---------------------------------------------------------------------------
// 请求底座
// ---------------------------------------------------------------------------

export class AdminApiError extends Error {
  readonly status: number | undefined;
  /** 后端 error body 的 detail 文案（如 401/403 的鉴权说明），无则 undefined。 */
  readonly detail: string | undefined;
  /** detail 为对象时的原样载荷（如萬星直连 502 投递失败随响应附带的
   *  message + 工单全文——人工降级用），无则 undefined。 */
  readonly detailPayload: Record<string, unknown> | undefined;

  constructor(
    message: string,
    status?: number,
    detail?: string,
    detailPayload?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "AdminApiError";
    this.status = status;
    this.detail = detail;
    this.detailPayload = detailPayload;
  }
}

interface RequestOptions {
  method: "GET" | "POST" | "PATCH";
  body?: unknown;
  /** 端点要求的额外头（一般不用——X-Admin-Token 由 nginx 服务端注入）。 */
  headers?: Record<string, string>;
}

async function requestJson<T>(path: string, options: RequestOptions): Promise<T> {
  const { method, body } = options;
  const url = `${ADMIN_API_BASE}${path}`;
  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers: {
        Accept: "application/json",
        ...bearerHeaders(),
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(options.headers ?? {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
  } catch (err) {
    if (err instanceof AdminApiError) throw err;
    throw new AdminApiError(
      `network error fetching ${url}: ${(err as Error).message}`,
    );
  }
  if (!response.ok) {
    let detail: string | undefined;
    let detailPayload: Record<string, unknown> | undefined;
    try {
      const data: unknown = await response.json();
      if (data && typeof data === "object") {
        const raw = (data as { detail?: unknown }).detail;
        if (typeof raw === "string") {
          detail = raw;
        } else if (raw && typeof raw === "object") {
          // 对象 detail 原样保留（如萬星直连 502 的 message + 工单全文）
          detailPayload = raw as Record<string, unknown>;
        }
      }
    } catch {
      /* 无 JSON body：保留 undefined */
    }
    throw new AdminApiError(
      `admin api ${response.status} for ${url}`,
      response.status,
      detail,
      detailPayload,
    );
  }
  try {
    return (await response.json()) as T;
  } catch (err) {
    throw new AdminApiError(`invalid json from ${url}: ${(err as Error).message}`);
  }
}

// ---------------------------------------------------------------------------
// 客户管理
// ---------------------------------------------------------------------------

export function fetchPlans(): Promise<AdminPlan[]> {
  return requestJson("/internal/admin/plans", { method: "GET" });
}

export function fetchCustomers(): Promise<AdminCustomer[]> {
  return requestJson("/internal/admin/customers", { method: "GET" });
}

export function createCustomer(
  payload: { name: string; plan_code: string; sub2api_user_id?: string | null },
): Promise<AdminCustomer> {
  return requestJson("/internal/admin/customers", {
    method: "POST",
    body: payload,
  });
}

export function patchCustomer(
  customerId: number,
  payload: { plan_code?: string; status?: string },
): Promise<AdminCustomer> {
  return requestJson(`/internal/admin/customers/${customerId}`, {
    method: "PATCH",
    body: payload,
  });
}

// ---------------------------------------------------------------------------
// 额度与账本
// ---------------------------------------------------------------------------

export function fetchCustomerUsage(customerId: number): Promise<CustomerUsage> {
  return requestJson(`/internal/admin/customers/${customerId}/usage`, {
    method: "GET",
  });
}

export function fetchLedger(
  params: { customer_id?: number; period?: string } = {},
): Promise<LedgerEntry[]> {
  const qs = new URLSearchParams();
  if (params.customer_id != null) qs.set("customer_id", String(params.customer_id));
  if (params.period) qs.set("period", params.period);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return requestJson(`/internal/admin/ledger${suffix}`, { method: "GET" });
}

// ---------------------------------------------------------------------------
// 需求单派发台
// ---------------------------------------------------------------------------

export function fetchDemands(status?: DemandStatus): Promise<AdminDemand[]> {
  const suffix = status ? `?status=${status}` : "";
  return requestJson(`/internal/admin/demands${suffix}`, { method: "GET" });
}

export function transitionDemand(
  demandId: number,
  payload: DemandTransitionInput,
): Promise<AdminDemand> {
  return requestJson(`/internal/admin/demands/${demandId}/transition`, {
    method: "POST",
    body: payload,
  });
}

/** 「转生成工单」：出单入拉取队列（出单不迁移状态；响应带全文供人工推送降级）。 */
export function exportDemandWorkorder(
  demandId: number,
  body?: { slug?: string },
): Promise<AdminWorkorderResult> {
  return requestJson(`/internal/admin/demands/${demandId}/workorder`, {
    method: "POST",
    ...(body ? { body } : {}),
  });
}

/** 「萬星直连」结果（wire-wanxing-direct-dispatch 契约冻结）：出单即时直投
 *  萬星 A2A（同步回执），不入舰队拉取队列——delivered/queued_for_pull
 *  如实分轨，绝无「入队未投递」歧义。 */
export interface AdminWanxingResult {
  ticket_yaml: string;
  ticket_path: string;
  slug: string;
  sha256: string;
  delivered: boolean;
  queued_for_pull: boolean;
  a2a: {
    http_status: number;
    message_id: string;
    idempotency_key: string;
    reply_excerpt: string;
  };
}

/**
 * 「萬星直连」：出单即时直投萬星 A2A（不迁移状态、不入拉取队列）。失败
 * 形状（契约冻结）：409/422/503 的 detail 为字符串；502 投递失败的 detail
 * 为对象（message + ticket_yaml 等工单全文随响应给人工降级——经
 * AdminApiError.detailPayload 原样上抛，UI 展示 detail.message 与全文）。
 */
export function dispatchDemandWanxing(
  demandId: number,
  body: { slug?: string } = {},
): Promise<AdminWanxingResult> {
  return requestJson(`/internal/admin/demands/${demandId}/wanxing-dispatch`, {
    method: "POST",
    body,
  });
}

/**
 * 「重试开 scope」（wire-scope-delivery-live）：对 delivered+pending 单重试
 * 建客户私有 scope（不迁移状态）。响应即最新单态——scope 面失败仍 200 且
 * scope_status=pending（留档 scope_create_failed），绝不虚构成功；非法状态
 * 409 由 AdminApiError.detail 如实上抛。
 */
export function retryDemandScope(demandId: number): Promise<AdminDemand> {
  return requestJson(`/internal/admin/demands/${demandId}/scope-retry`, {
    method: "POST",
  });
}

// ---------------------------------------------------------------------------
// 目录治理
// ---------------------------------------------------------------------------

/** 单页上限（与后端 _CATALOG_PENDING_MAX_LIMIT、公开目录 page_size 上限同值）。 */
export const CATALOG_PENDING_MAX_LIMIT = 100;

/**
 * 目录治理列表（分页）：status 筛镜像态，limit/offset 切片。
 * 规模现实（wire-admin-console-routing D3）：verified 3,421 条、registered
 * 54,132 条——全量拉取即 16 MB 级响应与一次性渲染（生产下滑白屏）。故缺省
 * 即按单页上限取，调用方不传参也绝不会拉全量；total 为匹配总数（非本页条数），
 * 页数由它算出，无需额外计数请求。
 */
export function fetchCatalogPending(
  status: "registered" | "verified" = "registered",
  page: CatalogPendingPage = {},
): Promise<CatalogPendingResponse> {
  const qs = new URLSearchParams();
  qs.set("status", status);
  qs.set("limit", String(page.limit ?? CATALOG_PENDING_MAX_LIMIT));
  qs.set("offset", String(page.offset ?? 0));
  return requestJson(`/internal/admin/catalog/pending?${qs.toString()}`, {
    method: "GET",
  });
}

export function addVerifyNote(
  indicatorId: string,
  note: string,
  author = "admin-console",
): Promise<ReviewNote> {
  return requestJson(
    `/internal/admin/catalog/${encodeURIComponent(indicatorId)}/verify-note`,
    { method: "POST", body: { note, author } },
  );
}

export function fetchReviewNotes(indicatorId: string): Promise<ReviewNote[]> {
  return requestJson(
    `/internal/admin/catalog/${encodeURIComponent(indicatorId)}/notes`,
    { method: "GET" },
  );
}

// ---------------------------------------------------------------------------
// 用量分轨概览
// ---------------------------------------------------------------------------

export interface UsageOverviewQuery {
  credential_track?: "customer" | "platform";
  start?: string;
  end?: string;
}

export function fetchUsageOverview(
  query: UsageOverviewQuery = {},
): Promise<UsageOverview> {
  const qs = new URLSearchParams();
  if (query.credential_track) qs.set("credential_track", query.credential_track);
  if (query.start) qs.set("start", query.start);
  if (query.end) qs.set("end", query.end);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return requestJson(`/internal/admin/usage/overview${suffix}`, {
    method: "GET",
  });
}

// ---------------------------------------------------------------------------
// 客户本地数据只读概览（wire-customer-local-data 任务 5.1）
// ---------------------------------------------------------------------------

/** 连接器健康（api 仓 datasets/schemas.py InternalConnectorOut，只读投影）。 */
export interface DatasetConnectorHealth {
  id: string;
  status: string;
  last_heartbeat_at: string | null;
}

/** per-cell 概览行（api 仓 datasets/schemas.py InternalOverviewOut）。 */
export interface CustomerLocalDataOverview {
  customer_id: string;
  datasets: number;
  storage_bytes: number;
  connectors: DatasetConnectorHealth[];
}

/**
 * per-cell 客户数据集存储用量与连接器同步健康（只读，无管理操作入口）。
 * 鉴权（admin 双闸后）：自动附登录 Bearer；X-Admin-Token 由 nginx 服务端注入。
 * 浏览器不再携带任何 X-Internal-Token——datasets 内部面若仍要求该头，
 * 由 api/网关侧把该端点并入双闸通道（前端不做令牌拼装）。
 */
export function fetchCustomerLocalDataOverview(): Promise<
  CustomerLocalDataOverview[]
> {
  return requestJson("/internal/datasets/overview", {
    method: "GET",
  });
}
