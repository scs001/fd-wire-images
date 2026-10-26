// 控制台 AI 对话构建 / 产物草稿 API 客户端（wire-platform-v1 任务 5.5 web 面）。
// - 基址与 catalog/api.ts 同约定：VITE_API_BASE 覆盖，默认 /api 同源
//   （dev 由 vite proxy 转发到 127.0.0.1:8000，生产同源由网关承接）。
// - 客户身份：Authorization: Bearer <ID token>（Logto 登录会话，见 auth.ts）；
//   开发占位 X-Customer-Id 已彻底弃用（wire-self-serve-launch 任务 4.4），
//   服务端以校验后的 Logto sub 经映射解析客户，跨客户一律 404。
// - 错误如实：非 2xx 一律抛 ConsoleApiError（携带 status 与后端 detail
//   文案，如 429 的每日限额说明），由 UI 原样呈现、绝不静默吞掉。
import { API_BASE } from "../catalog/api";
import { loadSession } from "./auth";
// 纯类型引用（契约试取入参 = 面板 query 同形）：spec.ts 零依赖，无环。
import type { DashboardPanelQuery } from "./dashboard/spec";
// record-view 响应归一（recordViewSpec.ts 只类型引用本模块——无运行时环）。
import {
  normalizeRecordViewPayload,
  type RecordViewPayload,
} from "./recordview/recordViewSpec";

export type MessageRole = "user" | "assistant" | "tool_trace";

export const ARTIFACT_KINDS = [
  "dashboard",
  "form",
  "composite_indicator",
  "research_report",
  "alert_rule",
  "refresh_schedule",
  // wire-record-view 任务 1.3 展示序追加：与 api 侧 workbench.WORKBENCH_KINDS
  // 两处一致（AI 生成门 ARTIFACT_TOOL_KINDS 不收录该 kind）。
  "record-view",
  // wire-rich-text-docs 任务 4.4 展示序追加：同样必须与 api 侧
  // workbench.WORKBENCH_KINDS 两处一致（该元组已含 doc）——两处漂移会让
  // 列表筛选/工作台计数与产物实况不符。
  "doc",
] as const;

export type ArtifactKind = (typeof ARTIFACT_KINDS)[number];

export interface Conversation {
  id: number;
  customer_id: string;
  title: string;
  created_at: string;
}

export interface ChatMessage {
  id: number;
  conversation_id: number;
  role: MessageRole;
  content: string;
  created_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: ChatMessage[];
}

export interface DraftArtifact {
  id: number;
  conversation_id: number;
  customer_id: string;
  kind: string;
  spec: Record<string, unknown>;
  status: string;
  created_at: string;
}

export interface ConversationCreateInput {
  title?: string;
  message?: string;
}

/** FastAPI 422 校验错误 detail 数组条目（wire-demand-generation-orchestration
 *  冻结契约：非法源地址条目 loc=("body","source_urls",<索引>)、声明缺失
 *  loc=("body","public_data_asserted")——由 UI 按条目映射本地化文案，
 *  绝不透传英文 msg 原文照抄）。 */
export interface ApiDetailItem {
  type?: string;
  loc?: Array<string | number>;
  msg?: string;
}

/** detail 数组形态收窄：仅取对象条目的 type/loc/msg（其余键忽略）。 */
function parseDetailItems(raw: unknown[]): ApiDetailItem[] {
  return raw.flatMap((entry): ApiDetailItem[] => {
    if (!entry || typeof entry !== "object") return [];
    const record = entry as Record<string, unknown>;
    const item: ApiDetailItem = {};
    if (typeof record.type === "string") item.type = record.type;
    if (Array.isArray(record.loc)) {
      const loc = record.loc.filter(
        (v): v is string | number => typeof v === "string" || typeof v === "number",
      );
      if (loc.length > 0) item.loc = loc;
    }
    if (typeof record.msg === "string") item.msg = record.msg;
    return [item];
  });
}

export class ConsoleApiError extends Error {
  readonly status: number | undefined;
  /** 后端 error body 的 detail 文案（如 429 限额说明），无则 undefined。 */
  readonly detail: string | undefined;
  /** 422 校验错误的 detail 数组条目（FastAPI 标准形状；string detail 时
   *  为 undefined）——按条目供 UI 映射本地化文案。 */
  readonly detailItems: ApiDetailItem[] | undefined;

  constructor(
    message: string,
    status?: number,
    detail?: string,
    detailItems?: ApiDetailItem[],
  ) {
    super(message);
    this.name = "ConsoleApiError";
    this.status = status;
    this.detail = detail;
    this.detailItems = detailItems;
  }
}

interface RequestOptions {
  method: "GET" | "POST" | "DELETE";
  body?: unknown;
}

/** 登录会话 Bearer 头（无会话即 401 如实上抛，绝不携带占位身份）。 */
function bearerHeaders(): Record<string, string> {
  const session = loadSession();
  if (!session?.idToken) {
    throw new ConsoleApiError("no console session (sign in required)", 401);
  }
  return { Authorization: `Bearer ${session.idToken}` };
}

/** 统一响应处理：非 2xx 抛 ConsoleApiError（带后端 detail），204 返回 null。 */
async function readResponse(response: Response, url: string): Promise<unknown> {
  if (!response.ok) {
    let detail: string | undefined;
    let detailItems: ApiDetailItem[] | undefined;
    try {
      const data: unknown = await response.json();
      if (data && typeof data === "object") {
        const rawDetail = (data as { detail?: unknown }).detail;
        if (typeof rawDetail === "string") {
          detail = rawDetail;
        } else if (Array.isArray(rawDetail)) {
          // FastAPI 422 校验错误数组：逐条收窄（string detail 语义不变）。
          detailItems = parseDetailItems(rawDetail);
        }
      }
    } catch {
      /* 无 JSON body：保留 undefined */
    }
    throw new ConsoleApiError(
      `console api ${response.status} for ${url}`,
      response.status,
      detail,
      detailItems,
    );
  }
  if (response.status === 204) return null;
  try {
    return await response.json();
  } catch (err) {
    throw new ConsoleApiError(`invalid json from ${url}: ${(err as Error).message}`);
  }
}

async function requestJson(url: string, options: RequestOptions): Promise<unknown> {
  const { method, body } = options;
  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers: {
        Accept: "application/json",
        ...bearerHeaders(),
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
  } catch (err) {
    if (err instanceof ConsoleApiError) throw err;
    throw new ConsoleApiError(
      `network error fetching ${url}: ${(err as Error).message}`,
    );
  }
  return readResponse(response, url);
}

/** multipart 上传（浏览器自定 Content-Type boundary，绝不手写头）。 */
async function requestMultipart(
  url: string,
  form: FormData,
): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: { Accept: "application/json", ...bearerHeaders() },
      body: form,
    });
  } catch (err) {
    if (err instanceof ConsoleApiError) throw err;
    throw new ConsoleApiError(
      `network error fetching ${url}: ${(err as Error).message}`,
    );
  }
  return readResponse(response, url);
}

/** 呈现用错误文案：ConsoleApiError 有后端 detail 用 detail，否则用兜底文案。 */
export function apiErrorText(err: unknown, fallback: string): string {
  return err instanceof ConsoleApiError && err.detail ? err.detail : fallback;
}

/** 新开对话（带 message 即当场跑一轮构建循环）。 */
export function createConversation(
  payload: ConversationCreateInput,
): Promise<ConversationDetail> {
  return requestJson(`${API_BASE}/v1/ai/conversations`, {
    method: "POST",
    body: payload,
  }) as Promise<ConversationDetail>;
}

/** 本企业对话列表（按创建时间倒序）。 */
export function fetchConversations(): Promise<Conversation[]> {
  return requestJson(`${API_BASE}/v1/ai/conversations`, {
    method: "GET",
  }) as Promise<Conversation[]>;
}

/** 会话全部消息（user / assistant / tool_trace 三角色）。 */
export function fetchMessages(
  conversationId: number,
): Promise<ChatMessage[]> {
  return requestJson(
    `${API_BASE}/v1/ai/conversations/${conversationId}/messages`,
    { method: "GET" },
  ) as Promise<ChatMessage[]>;
}

/** 增量取消息：只返回 id > afterId 的新增行（wire-assistant-panel）。
 *  构建循环每追加一条就落库，浮层据此在长耗时构建期间显示真实进展。 */
export function fetchMessagesAfter(
  conversationId: number,
  afterId: number,
): Promise<ChatMessage[]> {
  return requestJson(
    `${API_BASE}/v1/ai/conversations/${conversationId}/messages?after_id=${afterId}`,
    { method: "GET" },
  ) as Promise<ChatMessage[]>;
}

/** 浮层当前的页面上下文（只放标识，绝不放数据）。 */
export interface AssistantContext {
  tab?: string;
  artifact_id?: number;
  dataset_id?: number;
}

/** 一轮问答结果（conversation_id 供浮层维持单活会话）。 */
export interface AssistantAnswer {
  conversation_id: number;
  messages: ChatMessage[];
}

/** 问答轨一轮：不依赖数据处理引擎，免费档可用（wire-assistant-panel）。
 *  不给 conversationId 即新开（计入每日限额）。 */
export function askAssistant(
  content: string,
  options: { conversationId?: number; context?: AssistantContext } = {},
): Promise<AssistantAnswer> {
  const body: Record<string, unknown> = { content };
  if (options.conversationId != null) body.conversation_id = options.conversationId;
  if (options.context != null) body.context = options.context;
  return requestJson(`${API_BASE}/v1/ai/answer`, {
    method: "POST",
    body,
  }) as Promise<AssistantAnswer>;
}

/** 追加用户消息并驱动一轮构建循环（返回本轮落库的消息序列）。 */
export function postMessage(
  conversationId: number,
  content: string,
  context?: AssistantContext,
): Promise<ChatMessage[]> {
  // context（wire-assistant-panel 既有语义在统一循环上的延续）：白名单
  // 键 tab/artifact_id/dataset_id，服务端净化后注入 system 提示。
  return requestJson(
    `${API_BASE}/v1/ai/conversations/${conversationId}/messages`,
    {
      method: "POST",
      body: context ? { content, context } : { content },
    },
  ) as Promise<ChatMessage[]>;
}

/** 单个会话的产物草稿列表。 */
export function fetchArtifacts(
  conversationId: number,
): Promise<DraftArtifact[]> {
  return requestJson(
    `${API_BASE}/v1/ai/conversations/${conversationId}/artifacts`,
    { method: "GET" },
  ) as Promise<DraftArtifact[]>;
}

/** 聚合本企业全部会话的产物草稿（任一会话失败即如实上抛）。 */
export async function fetchAllArtifacts(
  conversations: Conversation[],
): Promise<DraftArtifact[]> {
  const grouped = await Promise.all(
    conversations.map((c) => fetchArtifacts(c.id)),
  );
  return grouped
    .flat()
    .sort((a, b) => b.id - a.id);
}

// ── 副驾动作闸（wire-copilot-action-gate 任务 3.4/4.1 web 面）────────────
// 契约（design D4 冻结，后端并行实现——本层按契约编码）：
// - POST /api/v1/ai/actions/propose {action_id, args, conversation_id} → 201
//   {proposal_id, action_id, state, tier, impact, missing_fields?}
// - POST /api/v1/ai/actions/{id}/approve {compliance?: {source_url,
//   public_claim}} → 200 {proposal_id, action_id, state, result_summary?,
//   error?}；非 owner/admin 批 publish 档 403、缺合规字段 422（detail 如实
//   上抛，由卡原样呈现）。
// - POST /api/v1/ai/actions/{id}/reject → 200 {proposal_id, state:"rejected"}
// - GET  /api/v1/ai/actions/{id} → 提议详情（含 result_summary/error——历史
//   回放已执行面的数据源）；GET /api/v1/ai/actions?conversation_id= → 同形
//   数组（按 id 升序）。

/** 提议/执行状态机（append-only：终态不可逆）。 */
export type WireActionState =
  | "pending_confirm"
  | "pending_approval"
  | "executed"
  | "rejected"
  | "expired"
  | "failed";

/** 确认档位：inline=行内确认、second=二次确认、async=异步审批。 */
export type WireActionTier = "inline" | "second" | "async";

/** propose 响应（201）。 */
export interface WireActionProposal {
  proposal_id: number;
  action_id: string;
  state: WireActionState;
  tier: WireActionTier;
  impact: string;
  /** 预检发现缺失的参数名（如实呈现，服务端不代填）。 */
  missing_fields?: string[];
}

/** 需求单合规门（D8）：模型给的取值服务端一律不采纳，只收卡上客户手填值。 */
export interface WireActionCompliance {
  source_url: string;
  public_claim: boolean;
}

/** approve 响应（200）：state 只有 executed|failed 两个终态。 */
export interface WireActionApproval {
  proposal_id: number;
  action_id: string;
  state: "executed" | "failed";
  result_summary?: string;
  error?: string;
}

/** 提议详情/列表行（GET /actions 形状——历史回放与审计指向的数据源）。 */
export interface WireAction {
  proposal_id: number;
  action_id: string;
  state: WireActionState;
  tier: WireActionTier;
  impact: string;
  args_summary?: string;
  created_at: string;
  approved_by?: string;
  expires_at?: string;
  result_summary?: string;
  error?: string;
}

/** 发起动作提议（写操作唯一入口——工具面结构性不含写工具）。 */
export function proposeAction(
  actionId: string,
  args: Record<string, unknown>,
  conversationId: number,
): Promise<WireActionProposal> {
  return requestJson(`${API_BASE}/v1/ai/actions/propose`, {
    method: "POST",
    body: { action_id: actionId, args, conversation_id: conversationId },
  }) as Promise<WireActionProposal>;
}

/**
 * action_id 前缀 → 受影响资源页（design D9 事件刷新的 resources 映射，
 * 单一真源）：artifact/dataset/demand 三族各归各页，未知前缀返回空数组
 * （无页可刷新，事件仍派发——接收方按命中过滤）。
 */
export function actionAffectedResources(actionId: string): string[] {
  if (actionId.startsWith("wire.artifact.")) return ["artifacts"];
  if (actionId.startsWith("wire.dataset.")) return ["datasets"];
  if (actionId.startsWith("wire.demand.")) return ["demands"];
  return [];
}

/**
 * 确认执行（inline/second 即时执行；publish 档要求本队 owner/admin）。
 * 执行成功后广播 window 级 `wire:action-executed`（detail.resources 由
 * action_id 前缀映射）——受影响页面挂载期监听并重取列表（wire 无 WS 栈，
 * 事件与执行同页同步发生，无需轮询兜底）。
 */
export async function approveAction(
  proposalId: number,
  compliance?: WireActionCompliance,
): Promise<WireActionApproval> {
  const result = (await requestJson(
    `${API_BASE}/v1/ai/actions/${proposalId}/approve`,
    {
      method: "POST",
      // compliance 只在携带时进 body（需求单合规门；其他动作不带该键）。
      ...(compliance ? { body: { compliance } } : { body: {} }),
    },
  )) as WireActionApproval;
  if (result.state === "executed") {
    window.dispatchEvent(
      new CustomEvent("wire:action-executed", {
        detail: { resources: actionAffectedResources(result.action_id ?? "") },
      }),
    );
  }
  return result;
}

/** 取消提议（append-only 落 rejected 态；已执行的动作不可取消）。 */
export function rejectAction(
  proposalId: number,
): Promise<{ proposal_id: number; state: "rejected" }> {
  return requestJson(`${API_BASE}/v1/ai/actions/${proposalId}/reject`, {
    method: "POST",
    body: {},
  }) as Promise<{ proposal_id: number; state: "rejected" }>;
}

/** 单条提议详情（轮询异步审批结果/审计指向用）。 */
export function fetchAction(proposalId: number): Promise<WireAction> {
  return requestJson(`${API_BASE}/v1/ai/actions/${proposalId}`, {
    method: "GET",
  }) as Promise<WireAction>;
}

/** 会话内提议列表（按 id 升序——历史回放两态裁决的数据源）。 */
export function fetchConversationActions(
  conversationId: number,
): Promise<WireAction[]> {
  return requestJson(
    `${API_BASE}/v1/ai/actions?conversation_id=${conversationId}`,
    { method: "GET" },
  ) as Promise<WireAction[]>;
}

// ── 我的数据：客户数据集与连接器（wire-customer-local-data 任务组 3）────────
// 路径/载荷严格对齐冻结契约 reports/interfaces.md §3.3 客户面（Bearer 登录身份）。

export type DatasetSourceKind =
  | "upload"
  | "connector"
  | "declared"
  | "derived"
  | "indicator";

/** 连接器数据集同步模式：全量快照（每次整表重拍）或增量水位（按水位列合并）。 */
export type DatasetSyncMode = "snapshot" | "incremental";

export const SYNC_STATUSES = [
  "idle",
  "syncing",
  "ok",
  "failed",
  "paused_drift",
  "paused_quota",
] as const;

export type SyncStatus = (typeof SYNC_STATUSES)[number];

export interface DatasetColumn {
  name: string;
  type: string;
}

export interface Dataset {
  id: string;
  key: string;
  name: string;
  source_kind: DatasetSourceKind;
  connector_id: string | null;
  /** 连接器本地源名（本地配置 sources[].name 的映射键）。 */
  source_name?: string;
  /** 源内的表/文件名（如 public.orders 或 sales.csv）。 */
  source_ref: string | null;
  columns: DatasetColumn[];
  row_count: number;
  size_bytes: number;
  sync_cron: string | null;
  sync_status: SyncStatus;
  paused_reason: string | null;
  /** 同步模式（wire-incremental-sync：连接器数据集；响应缺省按 snapshot
   *  容错读取——旧后端不带该字段时与全量口径一致）。 */
  sync_mode?: DatasetSyncMode;
  /** 增量水位列名（snapshot 模式 / 未配置为 null；须 ∈ columns 列清单）。 */
  watermark_column?: string | null;
  /** 当前水位值（字符串原样呈现；尚未同步为 null）。 */
  watermark_value?: string | null;
  created_at: string;
  updated_at: string;
  /** 假后端归属过滤用（客户面响应可省略）。 */
  customer_id?: string;
}

export interface DatasetRows {
  columns: DatasetColumn[];
  /** 每行含引擎注入的行锚 `__row_id`（`pk:…` / `hash:…` / `ins:…`）。 */
  rows: Array<Record<string, unknown>>;
  total: number;
}

export type CorrectionOp = "update" | "delete" | "insert";

export interface Correction {
  id: number;
  dataset_id: string;
  op: CorrectionOp;
  row_id: string | null;
  column: string | null;
  value: unknown;
  values: Record<string, unknown> | null;
  actor: string;
  /** 写入通道归属（wire-customer-data-engine）：console 成员 / data-key 应用 / agent。 */
  channel?: "console" | "data-key" | "agent";
  actor_id?: string | null;
  actor_label?: string | null;
  created_at: string;
  reverted_at: string | null;
  dangling_at: string | null;
  dangling_reason: string | null;
}

export type CorrectionInput =
  | { op: "update"; row_id: string; column: string; value: unknown }
  | { op: "delete"; row_id: string }
  | {
      op: "insert";
      values: Record<string, unknown>;
      /** wire-form-entry-advanced：携带时服务端按该 form 产物当前版本执行
       *  表单字段校验+提交闸（本企业 + kind=form + spec.datasetId 与目标集
       *  一致才生效；不携带时行为与现状逐字一致）。 */
      artifact_id?: string;
    };

export type SyncRecordStatus = "ok" | "failed" | "drift" | "quota";

export interface SyncRecord {
  id: string;
  dataset_id: string;
  started_at: string;
  finished_at: string | null;
  status: SyncRecordStatus;
  rows: number;
  error: string | null;
}

export type ConnectorStatus = "online" | "offline";

export interface Connector {
  id: string;
  name: string;
  version: string | null;
  status: ConnectorStatus;
  last_heartbeat_at: string | null;
  revoked_at: string | null;
  created_at: string;
  /** 假后端归属过滤用（客户面响应可省略）。 */
  customer_id?: string;
}

export interface PairingToken {
  token: string;
  expires_at: string;
}

export interface UploadPreview {
  upload_id: string;
  filename?: string;
  columns: DatasetColumn[];
  row_count: number;
  within_limit: boolean;
  sample_rows: Array<Record<string, unknown>>;
}

export type CreateDatasetSource =
  | { kind: "upload"; upload_id: string }
  | {
      kind: "connector";
      connector_id: string;
      /** 连接器本地源名（本地配置 sources[].name 的映射键），前端必填校验。 */
      source_name?: string;
      /** 源内的表/文件名（表名可带 schema 前缀；文件源为文件名）。 */
      source_ref: string;
      schedule_cron: string | null;
    }
  | {
      /** 指标目录建集（wire-indicator-to-dataset）：verified 指标一键落集。 */
      kind: "indicator";
      /** 统一注册目录标识 source_db:native_code。 */
      indicator_id: string;
      entity_type: string;
      entity_id: string;
      /** YYYY 或 YYYY-MM-DD；缺省 = 全史。 */
      start: string | null;
      end: string | null;
    };

export interface CreateDatasetInput {
  name: string;
  source: CreateDatasetSource;
}

/** 列表卡片（来源/行数/大小/同步状态/最近更新）。 */
export function fetchDatasets(): Promise<Dataset[]> {
  return requestJson(`${API_BASE}/v1/datasets`, {
    method: "GET",
  }) as Promise<Dataset[]>;
}

/** 上传文件解析预览（multipart 字段名 `file`；超限由后端 400 如实拒收）。 */
export async function uploadDataset(
  file: File,
): Promise<UploadPreview> {
  const form = new FormData();
  form.append("file", file);
  return (await requestMultipart(
    `${API_BASE}/v1/datasets/uploads`,
    form,
  )) as UploadPreview;
}

/** 建档：upload 类立即 ingest 引擎；connector 类下发白名单与调度。 */
export function createDataset(
  payload: CreateDatasetInput,
): Promise<Dataset> {
  return requestJson(`${API_BASE}/v1/datasets`, {
    method: "POST",
    body: payload,
  }) as Promise<Dataset>;
}

export function fetchDataset(
  datasetId: string,
): Promise<Dataset> {
  return requestJson(`${API_BASE}/v1/datasets/${datasetId}`, {
    method: "GET",
  }) as Promise<Dataset>;
}

/** 删除数据集（引擎删表 + 元数据；连接器白名单移除≠删除）。 */
export function deleteDataset(
  datasetId: string,
): Promise<{ deleted: boolean }> {
  return requestJson(`${API_BASE}/v1/datasets/${datasetId}`, {
    method: "DELETE",
  }) as Promise<{ deleted: boolean }>;
}

/** 预览分页（透传引擎 preview；每行带行锚 `__row_id`）。 */
export function fetchDatasetRows(
  datasetId: string,
  page = 1,
  pageSize = 50,
): Promise<DatasetRows> {
  return requestJson(
    `${API_BASE}/v1/datasets/${datasetId}/rows?page=${page}&page_size=${pageSize}`,
    { method: "GET" },
  ) as Promise<DatasetRows>;
}

// ── 数据集 CSV 导出（wire-dataset-export 任务 2.2）────────────────────
// 契约：GET /api/v1/datasets/{id}/export（Bearer 登录身份，跨客户 404）——
// 响应 text/csv 全文（UTF-8 带 BOM，Excel 直开）；Content-Disposition
// 文件名 = <数据集 key>.csv；行数超 10,000 时头 X-Export-Truncated: true。

/** CSV 导出结果：blob 交给调用方触发下载；truncated=已截断至前 10,000 行。 */
export interface DatasetCsvExport {
  blob: Blob;
  filename: string;
  truncated: boolean;
}

/** 导出数据集有效视图为 CSV（非 JSON 端点——带鉴权头 fetch 后转 blob，
 *  文件名取响应 Content-Disposition，缺省按 <datasetId>.csv 兜底命名；
 *  错误语义与 requestJson 完全一致：ConsoleApiError 带 status/detail）。 */
export async function exportDatasetCsv(
  datasetId: string,
): Promise<DatasetCsvExport> {
  const url = `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/export`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: {
        Accept: "text/csv",
        ...bearerHeaders(),
      },
    });
  } catch (err) {
    if (err instanceof ConsoleApiError) throw err;
    throw new ConsoleApiError(
      `network error fetching ${url}: ${(err as Error).message}`,
    );
  }
  if (!response.ok) {
    let detail: string | undefined;
    try {
      const data: unknown = await response.json();
      if (
        data &&
        typeof data === "object" &&
        typeof (data as { detail?: unknown }).detail === "string"
      ) {
        detail = (data as { detail: string }).detail;
      }
    } catch {
      /* 无 JSON body：保留 undefined */
    }
    throw new ConsoleApiError(
      `console api ${response.status} for ${url}`,
      response.status,
      detail,
    );
  }
  const disposition = response.headers?.get?.("content-disposition") ?? "";
  const match = disposition.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const filename = match?.[1] ?? `${datasetId}.csv`;
  const truncated =
    (response.headers?.get?.("x-export-truncated") ?? "").toLowerCase() ===
    "true";
  try {
    return { blob: await response.blob(), filename, truncated };
  } catch (err) {
    throw new ConsoleApiError(`invalid body from ${url}: ${(err as Error).message}`);
  }
}

/** 写修正层（改值/删行/加行）；不回写客户源。 */
export function addCorrection(
  datasetId: string,
  payload: CorrectionInput,
): Promise<{ correction_id: number }> {
  return requestJson(`${API_BASE}/v1/datasets/${datasetId}/corrections`, {
    method: "POST",
    body: payload,
  }) as Promise<{ correction_id: number }>;
}

/** 修正历史（含悬空与已回滚）。端点回 ``{corrections: […]}`` 信封
 * （引擎透传与 api 层本地实现同形）——此处解包；裸数组形态宽容兼容。 */
export async function fetchCorrections(
  datasetId: string,
): Promise<Correction[]> {
  const body = await requestJson(
    `${API_BASE}/v1/datasets/${datasetId}/corrections`,
    { method: "GET" },
  );
  if (Array.isArray(body)) return body as Correction[];
  const list = (body as { corrections?: unknown }).corrections;
  return Array.isArray(list) ? (list as Correction[]) : [];
}

/** 单条回滚（标记失效 + 重建有效表在 api/引擎侧完成）。 */
export function revertCorrection(
  datasetId: string,
  correctionId: number,
): Promise<{ reverted: boolean }> {
  return requestJson(
    `${API_BASE}/v1/datasets/${datasetId}/corrections/${correctionId}/revert`,
    { method: "POST" },
  ) as Promise<{ reverted: boolean }>;
}

export function fetchSyncs(
  datasetId: string,
): Promise<SyncRecord[]> {
  return requestJson(`${API_BASE}/v1/datasets/${datasetId}/syncs`, {
    method: "GET",
  }) as Promise<SyncRecord[]>;
}

/** 漂移人工确认恢复：accept_columns = 客户确认接受的新结构列。 */
export function resumeDataset(
  datasetId: string,
  acceptColumns: string[],
): Promise<Dataset> {
  return requestJson(`${API_BASE}/v1/datasets/${datasetId}/resume`, {
    method: "POST",
    body: { accept_columns: acceptColumns },
  }) as Promise<Dataset>;
}

// ── 增量同步设置（wire-incremental-sync）────────────────────────────
// 契约：PATCH /api/v1/datasets/{id}/sync-settings，body
// {sync_mode?, watermark_column?, reset_watermark?}——服务端校验：非连接器
// 数据集 422、水位列必须 ∈ 数据集列清单、增量模式必须配水位列、切回
// snapshot 清水位字段、reset_watermark 只清水位值；成功返回更新后数据集。

/** 同步设置更新入参（三字段均可选，至少传其一）。 */
export interface SyncSettingsInput {
  sync_mode?: DatasetSyncMode;
  /** 增量模式必配（服务端校验 ∈ 列清单）；切回 snapshot 由服务端清空。 */
  watermark_column?: string;
  /** true = 只清当前水位值（下次同步回到全量快照基线）。 */
  reset_watermark?: boolean;
}

/** 更新连接器数据集的同步模式 / 水位列 / 重置水位（返回更新后数据集）。 */
export function updateSyncSettings(
  datasetId: string,
  body: SyncSettingsInput,
): Promise<Dataset> {
  return requestJsonAnyMethod(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/sync-settings`,
    "PATCH",
    { body },
  ) as Promise<Dataset>;
}

// ── 数据面 key（wire-customer-data-engine 5.2）────────────────────

export interface DataPlaneKey {
  id: string;
  label: string;
  fingerprint: string;
  dataset_ids: string[];
  status: "active" | "revoked";
  created_at: string;
  revoked_at: string | null;
  last_used_at: string | null;
}

export interface DataPlaneKeyCreated extends DataPlaneKey {
  /** 明文仅铸造响应一次；落库只有 sha256。 */
  key: string;
}

export function fetchDataPlaneKeys(): Promise<DataPlaneKey[]> {
  return requestJson(`${API_BASE}/v1/data-plane-keys`, {
    method: "GET",
  }) as Promise<DataPlaneKey[]>;
}

/** 铸造：dataset_ids 空 = 本企业全部数据集。 */
export function createDataPlaneKey(payload: {
  label: string;
  dataset_ids?: string[];
}): Promise<DataPlaneKeyCreated> {
  return requestJson(`${API_BASE}/v1/data-plane-keys`, {
    method: "POST",
    body: payload,
  }) as Promise<DataPlaneKeyCreated>;
}

/** 吊销即时生效（验证路径无缓存层）。 */
export function revokeDataPlaneKey(keyId: string): Promise<DataPlaneKey> {
  return requestJson(`${API_BASE}/v1/data-plane-keys/${keyId}`, {
    method: "DELETE",
  }) as Promise<DataPlaneKey>;
}

// ── 结构化查询（wire-customer-data-engine 5.3；与 MCP 工具零漂移）──

export type QueryFilterOp =
  | "eq" | "ne" | "gt" | "gte" | "lt" | "lte"
  | "in" | "notin" | "like" | "isnull" | "notnull";

export interface QueryFilter {
  column: string;
  op: QueryFilterOp;
  value?: unknown;
  values?: unknown[];
}

export interface QuerySort {
  column: string;
  dir: "asc" | "desc";
}

export interface DatasetQueryInput {
  filters?: QueryFilter[];
  sort?: QuerySort[];
  limit?: number;
  offset?: number;
  count_only?: boolean;
  /** 去重列（wire-form-entry-advanced 动态选项解析）：携带时响应为
   *  distinct 形态（values 数组替代 rows——见 DatasetDistinctResult）。 */
  distinct_column?: string;
  raw?: boolean;
}

export interface DatasetQueryResult {
  columns: DatasetColumn[];
  rows: Array<Record<string, unknown>>;
  total: number;
  limit: number;
  offset: number;
  truncated: boolean;
}

/** 结构化查询（服务端执行：过滤/排序/精确 total；行含 __row_id 锚）。 */
export function queryDataset(
  datasetId: string,
  input: DatasetQueryInput,
): Promise<DatasetQueryResult> {
  return requestJson(`${API_BASE}/v1/datasets/${datasetId}/query`, {
    method: "POST",
    body: input,
  }) as Promise<DatasetQueryResult>;
}

// ── 动态选项解析（wire-form-entry-advanced 任务 5.1 web 面）────────────────
// 契约：console 运行时以会话身份走既有数据面查询端点
// POST /api/v1/datasets/{id}/query，body {filters, distinct_column, limit}——
// 响应为 distinct 形态 {distinct_column, values, count, total, truncated}；
// 值为引擎/api 档归一后的标量（前端统一 stringify 呈现）。

/** distinct 查询响应（携带 distinct_column 时 rows 替换为 values 数组）。 */
export interface DatasetDistinctResult {
  distinct_column: string;
  /** 去重值（服务端按值升序；元素可为 string|number|boolean|null）。 */
  values: unknown[];
  count: number;
  total: number;
  limit: number;
  offset: number;
  truncated: boolean;
}

/** 数据集列去重值查询（表单动态选项 console 解析路径；limit 由调用方给
 *  1..500，服务端可能不截断——前端兜底再裁剪并如实标注）。 */
export function queryDatasetDistinct(
  datasetId: string,
  input: { filters?: QueryFilter[]; column: string; limit?: number },
): Promise<DatasetDistinctResult> {
  return requestJson(`${API_BASE}/v1/datasets/${datasetId}/query`, {
    method: "POST",
    body: {
      ...(input.filters && input.filters.length > 0
        ? { filters: input.filters }
        : {}),
      distinct_column: input.column,
      limit: input.limit ?? 200,
    },
  }) as Promise<DatasetDistinctResult>;
}

/** 生成一次性配对令牌（15 分钟、单次；明文仅此一次）。 */
export function createPairingToken(): Promise<PairingToken> {
  return requestJson(`${API_BASE}/v1/connectors/pairing-tokens`, {
    method: "POST",
  }) as Promise<PairingToken>;
}

/** 连接器列表（在线/离线由心跳推导）。 */
export function fetchConnectors(): Promise<Connector[]> {
  return requestJson(`${API_BASE}/v1/connectors`, {
    method: "GET",
  }) as Promise<Connector[]>;
}

/** 吊销连接器（连 key 一起即时失效）。 */
export function revokeConnector(
  connectorId: string,
): Promise<Connector> {
  return requestJson(`${API_BASE}/v1/connectors/${connectorId}`, {
    method: "DELETE",
  }) as Promise<Connector>;
}

// ── 溯源账本客户面（wire-provenance-ledger 任务组 8）────────────────────
// 路径/载荷严格对齐冻结契约：/api/v1/provenance/*（Bearer 登录身份隔离）。

/** 溯源档位四档（off 不入账；已入账事件不因调档被清除）。 */
export const PROVENANCE_TIERS = [
  "off",
  "chain_only",
  "anchored_batch",
  "anchored_realtime",
] as const;

export type ProvenanceTier = (typeof PROVENANCE_TIERS)[number];

/** 事件操作者（v1 无签名，签名位为信封预留字段）。 */
export interface ProvenanceActor {
  kind: string;
  id: string;
}

/** 事件主体（kind 如 dataset / cell，key 即主体键）。 */
export interface ProvenanceSubject {
  kind: string;
  key: string;
}

/** 账本事件信封（面向控制台的呈现字段；哈希链字段不在此响应中）。 */
export interface ProvenanceEvent {
  /** 账本内单调序号（倒序分页游标 before_seq 用）。 */
  seq: number;
  event_id: string;
  /** 命名空间化类型，如 dataset.correction.added / cell.registered。 */
  type: string;
  ts_platform: string;
  actor: ProvenanceActor;
  subject: ProvenanceSubject;
  payload: Record<string, unknown>;
}

export interface ProvenanceEventsPage {
  /** 按 seq 倒序。 */
  events: ProvenanceEvent[];
  has_more: boolean;
}

export interface ProvenanceStatus {
  tier: ProvenanceTier;
  integrity: { ok: boolean; checked: number };
  anchored_through: { epoch: number | null; backend: string | null };
  /** 该数据集首条入账事件时间（溯源起点）。 */
  first_event_ts: string | null;
  /** 非空时如实展示（如启用前无历史事件——不伪造起点）。 */
  note: string | null;
}

export interface ProvenanceOverview {
  ledger: string;
  last_seq: number;
  integrity: { ok: boolean; checked: number };
  anchoring: {
    backend: string | null;
    last_sealed_epoch: number | null;
    lag_events: number;
  };
}

/** 数据集事件时间线（倒序；before_seq 为上一页最小 seq，向更早翻页）。 */
export function fetchProvenanceEvents(
  datasetKey: string,
  beforeSeq?: number,
  limit = 50,
): Promise<ProvenanceEventsPage> {
  const params = new URLSearchParams();
  if (beforeSeq != null) params.set("before_seq", String(beforeSeq));
  params.set("limit", String(limit));
  return requestJson(
    `${API_BASE}/v1/provenance/datasets/${encodeURIComponent(datasetKey)}/events?${params}`,
    { method: "GET" },
  ) as Promise<ProvenanceEventsPage>;
}

/** 数据集溯源状态（档位/链完整性/锚定进度/起点标注）。 */
export function fetchProvenanceStatus(
  datasetKey: string,
): Promise<ProvenanceStatus> {
  return requestJson(
    `${API_BASE}/v1/provenance/datasets/${encodeURIComponent(datasetKey)}/status`,
    { method: "GET" },
  ) as Promise<ProvenanceStatus>;
}

/** 调整数据集溯源档位（存活期内可调；已入账事件不受影响）。 */
export function updateProvenanceTier(
  datasetKey: string,
  tier: ProvenanceTier,
): Promise<{ tier: ProvenanceTier }> {
  return requestJson(
    `${API_BASE}/v1/provenance/datasets/${encodeURIComponent(datasetKey)}/tier`,
    { method: "POST", body: { tier } },
  ) as Promise<{ tier: ProvenanceTier }>;
}

/** 企业级账本总览（全局 last_seq/完整性/锚定滞后）。 */
export function fetchProvenanceOverview(
): Promise<ProvenanceOverview> {
  return requestJson(`${API_BASE}/v1/provenance/overview`, {
    method: "GET",
  }) as Promise<ProvenanceOverview>;
}

/** 证据包二进制内容 + 下载文件名（无 Content-Disposition 时按约定命名）。 */
export interface EvidencePackage {
  blob: Blob;
  filename: string;
}

/**
 * 导出企业级证据包（机器可读账本导出 + 全部锚定回执 + 离线核验工具，
 * 二进制流）。带鉴权头用 fetch 拉取后转 blob 交给调用方触发下载；
 * 文件名优先取响应 Content-Disposition，缺省按
 * evidence-package-<日期>.jsonl.bin 约定命名。
 */
export async function fetchEvidencePackage(
): Promise<EvidencePackage> {
  const url = `${API_BASE}/v1/provenance/evidence-package`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: {
        Accept: "application/octet-stream",
        ...bearerHeaders(),
      },
    });
  } catch (err) {
    if (err instanceof ConsoleApiError) throw err;
    throw new ConsoleApiError(
      `network error fetching ${url}: ${(err as Error).message}`,
    );
  }
  if (!response.ok) {
    let detail: string | undefined;
    try {
      const data: unknown = await response.json();
      if (
        data &&
        typeof data === "object" &&
        typeof (data as { detail?: unknown }).detail === "string"
      ) {
        detail = (data as { detail: string }).detail;
      }
    } catch {
      /* 无 JSON body：保留 undefined */
    }
    throw new ConsoleApiError(
      `console api ${response.status} for ${url}`,
      response.status,
      detail,
    );
  }
  const disposition = response.headers?.get?.("content-disposition") ?? "";
  const match = disposition.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const date = new Date().toISOString().slice(0, 10);
  const filename = match?.[1] ?? `evidence-package-${date}.jsonl.bin`;
  try {
    return { blob: await response.blob(), filename };
  } catch (err) {
    throw new ConsoleApiError(`invalid body from ${url}: ${(err as Error).message}`);
  }
}

// ── 客户侧用量（usage-transparency 任务 6.2；口径与行数门/结算同源）──────
// 契约：GET /api/console/usage → { period, plan_code, included_lines,
// used_lines, remaining_lines }（登录映射客户自己且仅自己的当月聚合）。

export interface ConsoleUsage {
  /** 账期（自然月，如 2026-10）。 */
  period: string;
  plan_code: string;
  included_lines: number;
  used_lines: number;
  remaining_lines: number;
}

/** 登录客户当月用量（已用/含额/余量，rows_returned 口径）。 */
export function fetchConsoleUsage(): Promise<ConsoleUsage> {
  return requestJson(`${API_BASE}/console/usage`, {
    method: "GET",
  }) as Promise<ConsoleUsage>;
}

// ── 需求单客户面（wire-ux-consolidation 任务 3.2/4.1/4.3）──────────────
// 契约：POST/GET /api/v1/demands（Bearer 登录身份，服务端按映射客户隔离，
// 他企数据不可见）。提交六入参：description（必填）/ contact_email（必填）/
// source_urls（≥1 条 http(s) 公开地址）/ public_data_asserted（必须 true）/
// company（选填）/ indicator_code（选填目录锚点）；状态三态 + rejected。
// wire-demand-generation-orchestration：详情响应新增 orchestration 时间线
// （客户安全子集，见 OrchestrationEntry）；422 校验错误为 FastAPI 标准数组
// （ConsoleApiError.detailItems），UI 按条目映射本地化文案。

/** 需求单提交入参（后端 DemandCreate：旧表单字段一律不再收；
 *  wire-demand-generation-orchestration 追加两硬校验字段）。 */
export interface DemandCreateInput {
  description: string;
  contact_email: string;
  /** 源地址（≥1 条 http(s) 公开地址、不内嵌账密——服务端逐条硬校验，
   *  422 按索引逐条回显）。 */
  source_urls: string[];
  /** 公开数据声明（必须 true 才能提交；缺失/为 false 服务端 422 拒收）。 */
  public_data_asserted: boolean;
  company?: string;
  indicator_code?: string;
}

/** 需求单时间线条目（客户可见投影）。 */
export interface DemandHistoryEvent {
  status: string;
  at: string;
  by: string;
  note: string | null;
}

/** 编排时间线条目（需求详情响应的客户安全子集，按时间升序；仅阶段+时间+
 *  PR 外链，内部字段不出客户面——wire-demand-generation-orchestration）。 */
export interface OrchestrationEntry {
  stage: "workorder_submitted" | "pr_open" | "merged" | "blocked";
  at: string;
  /** pr_open / merged 阶段带 PR 外链；其余为 null。 */
  pr_url: string | null;
}

/** 需求单客户视图（后端 DemandOut 截面：不含任何内部字段）。 */
export interface CustomerDemand {
  id: number;
  code: string;
  customer_id: string;
  /** 列表摘要（后端由描述截断生成）。 */
  title: string;
  /** 描述全文。 */
  intent: string;
  contact_email: string | null;
  company: string | null;
  status: string;
  status_history: DemandHistoryEvent[];
  /** 编排进度时间线（新单有、旧单缺省——UI 有则渲染区块，无则不渲染）。 */
  orchestration?: OrchestrationEntry[];
  /** 交付就绪视图（wire-demand-visibility 任务 3.1 客户面）：后端
   *  DemandOut.delivery 的客户安全子集——只有三个子字段，delivery_events
   *  全文与失败错误串绝不在此；旧单缺省该字段（delivered 无信号时不渲染）。 */
  delivery?: {
    /** 私有 scope 名（如 demand-DE-20261011-99）；未发起/挂起为 null。 */
    scope_name: string | null;
    /** null=未发起 / "ok"=私有 scope 已就绪 / "pending"=交付挂起重试中。 */
    scope_status: string | null;
    /** null=未授权 / true=已授权转公开 / false=已撤回。 */
    publish_authorized: boolean | null;
  } | null;
  rejection_reason: string | null;
  created_at: string;
  updated_at: string;
}

/** 提交需求单（四入参；成功返回 submitted 态新单）。 */
export function submitDemand(
  payload: DemandCreateInput,
): Promise<CustomerDemand> {
  return requestJson(`${API_BASE}/v1/demands`, {
    method: "POST",
    body: payload,
  }) as Promise<CustomerDemand>;
}

/** 本企业需求单列表（按创建时间倒序；跨客户隔离由服务端保证）。 */
export function fetchMyDemands(): Promise<CustomerDemand[]> {
  return requestJson(`${API_BASE}/v1/demands`, {
    method: "GET",
  }) as Promise<CustomerDemand[]>;
}

// ── 连接器下载清单（wire-connector-distribution 任务 3.1/3.2）──────────
// 契约：GET /api/v1/downloads/connector/manifest.json（公开端点，无鉴权；
// 未配置下载目录时 404）。二进制取件同命名空间
// GET /api/v1/downloads/connector/{platform}（Content-Length + X-Checksum-Sha256）。

/** manifest 单平台条目：available=false 即该平台文件缺失（如实呈现，不可链）。 */
export interface ConnectorDownloadPlatform {
  platform: string;
  filename: string;
  sha256: string;
  size: number;
  available: boolean;
}

/** 连接器发布清单（CI/手动晋升生成；版本号即心跳上报口径 sha-<7>）。 */
export interface ConnectorManifest {
  version: string;
  released_at: string;
  platforms: ConnectorDownloadPlatform[];
}

/** 取连接器下载清单（配对向导下载链接与「最新版本」对照的数据源）。 */
export function fetchConnectorManifest(): Promise<ConnectorManifest> {
  return requestJson(`${API_BASE}/v1/downloads/connector/manifest.json`, {
    method: "GET",
  }) as Promise<ConnectorManifest>;
}

/** 平台二进制下载地址（同源公开端点；与 manifest.platform 一一对应）。 */
export function connectorDownloadUrl(
  apiBase: string,
  platform: string,
): string {
  return `${apiBase}${API_BASE}/v1/downloads/connector/${encodeURIComponent(platform)}`;
}

// ── 免费档数据集配额（wire-free-tier-data-gate 任务 4.1）────────────────────
// 契约：GET /api/v1/datasets/quota → { plan_code, used_datasets,
// max_datasets, max_rows_per_dataset }。端点未就绪/失败时调用方应静默降级
// （配额条隐藏），绝不阻塞「我的数据」页面。max_* 为 `number | null`
//（wire-console-resilience：后端档位解析 fail-closed 时如实回 null，
// 与 schemas 的 `int | None` 对齐——消费方按「档位未知」降级，不抛错）。
export interface DatasetQuota {
  plan_code: string;
  used_datasets: number;
  max_datasets: number | null;
  max_rows_per_dataset: number | null;
}

/** 免费档配额余量（已用集数 / 上限集数、单集行数上限）。 */
export function fetchDatasetQuota(): Promise<DatasetQuota> {
  return requestJson(`${API_BASE}/v1/datasets/quota`, {
    method: "GET",
  }) as Promise<DatasetQuota>;
}

// ── 产物生命周期（wire-artifacts-persistence）：持久化 + 版本化 + 状态机 ──
// 契约：/api/v1/artifacts*（Bearer 登录身份，服务端按映射客户隔离，
// 他企一律 404）；spec 每次保存追加新版本（append-only），状态机
// draft → published → archived 单向、archived → draft 恢复。

export type ArtifactStatus = "draft" | "published" | "archived";

export const ARTIFACT_STATUSES = ["draft", "published", "archived"] as const;

/** 产物概要（列表用；spec 在详情响应里）。 */
export interface Artifact {
  id: number;
  customer_id: string;
  kind: string;
  title: string;
  status: ArtifactStatus;
  version: number;
  /** 来源会话指针（会话草稿导入时保留；直接创建为 null）。 */
  origin_conversation_id: number | null;
  created_at: string;
  updated_at: string;
}

/** 产物详情：当前 spec + 状态迁移操作记录（append-only）。 */
export interface ArtifactDetail extends Artifact {
  spec: Record<string, unknown>;
  status_history: Array<{
    status: string;
    at: string;
    by: string;
    note: string | null;
  }>;
}

/** 版本历史条目（列表用；spec 在单查响应里）。 */
export interface ArtifactVersionSummary {
  version: number;
  operator: string | null;
  created_at: string;
}

/** 单个历史版本（含该版本 spec 快照，只读）。 */
export interface ArtifactVersionDetail extends ArtifactVersionSummary {
  spec: Record<string, unknown>;
}

interface PutRequestOptions {
  body?: unknown;
}

/** 带任意 method 的 JSON 请求（上方 RequestOptions 联合不含 PUT/PATCH，
 * 产物端点与 sync-settings 等追加路由专用；鉴权与错误语义与 requestJson
 * 完全一致）。 */
async function requestJsonAnyMethod(
  url: string,
  method: "PUT" | "POST" | "PATCH",
  options: PutRequestOptions = {},
): Promise<unknown> {
  const { body } = options;
  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers: {
        Accept: "application/json",
        ...bearerHeaders(),
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
  } catch (err) {
    if (err instanceof ConsoleApiError) throw err;
    throw new ConsoleApiError(
      `network error fetching ${url}: ${(err as Error).message}`,
    );
  }
  return readResponse(response, url);
}

/** 本企业产物列表（按更新时间倒序；kind/status 可选过滤）。 */
export function fetchLifecycleArtifacts(
  kind?: string,
  status?: string,
): Promise<Artifact[]> {
  const params = new URLSearchParams();
  if (kind) params.set("kind", kind);
  if (status) params.set("status", status);
  const query = params.size > 0 ? `?${params}` : "";
  return requestJson(`${API_BASE}/v1/artifacts${query}`, {
    method: "GET",
  }) as Promise<Artifact[]>;
}

/** 产物详情（当前 spec + 状态操作记录；跨客户 404）。 */
export function fetchLifecycleArtifact(id: number): Promise<ArtifactDetail> {
  return requestJson(`${API_BASE}/v1/artifacts/${id}`, {
    method: "GET",
  }) as Promise<ArtifactDetail>;
}

/** 证据引用（wire-analysis-console 4.7）：条目 + 原文链接 + 片段核对结果。 */
export interface ArtifactEvidence {
  index: number;
  claim: string | null;
  source: string;
  stable_id: string;
  as_of: string;
  start: number;
  end: number;
  quote: string;
  url: string | null;
  quote_ok: boolean | null;
  error: string | null;
}

export function fetchArtifactEvidence(id: number): Promise<ArtifactEvidence[]> {
  return requestJson(`${API_BASE}/v1/artifacts/${id}/evidence`, {
    method: "GET",
  }) as Promise<ArtifactEvidence[]>;
}

/** spec 保存（PUT）：成功返回新版本号所在详情；archived 拒改 409。 */
export function saveArtifactSpec(
  id: number,
  spec: Record<string, unknown>,
): Promise<ArtifactDetail> {
  return requestJsonAnyMethod(`${API_BASE}/v1/artifacts/${id}`, "PUT", {
    body: { spec },
  }) as Promise<ArtifactDetail>;
}

/** 版本历史（新版本在前；append-only）。 */
export function fetchArtifactVersions(
  id: number,
): Promise<ArtifactVersionSummary[]> {
  return requestJson(`${API_BASE}/v1/artifacts/${id}/versions`, {
    method: "GET",
  }) as Promise<ArtifactVersionSummary[]>;
}

/** 查看任一历史版本（含 spec 快照）。 */
export function fetchArtifactVersion(
  id: number,
  version: number,
): Promise<ArtifactVersionDetail> {
  return requestJson(
    `${API_BASE}/v1/artifacts/${id}/versions/${version}`,
    { method: "GET" },
  ) as Promise<ArtifactVersionDetail>;
}

/** 发布（draft → published；非法迁移 409 说明合法路径）。 */
export function publishArtifact(id: number): Promise<ArtifactDetail> {
  return requestJsonAnyMethod(
    `${API_BASE}/v1/artifacts/${id}/publish`,
    "POST",
  ) as Promise<ArtifactDetail>;
}

/** 归档（draft/published → archived；不删除，可恢复）。 */
export function archiveArtifact(id: number): Promise<ArtifactDetail> {
  return requestJsonAnyMethod(
    `${API_BASE}/v1/artifacts/${id}/archive`,
    "POST",
  ) as Promise<ArtifactDetail>;
}

/** 恢复（archived → draft；恢复后方可再编辑/发布）。 */
export function restoreArtifact(id: number): Promise<ArtifactDetail> {
  return requestJsonAnyMethod(
    `${API_BASE}/v1/artifacts/${id}/restore`,
    "POST",
  ) as Promise<ArtifactDetail>;
}

/** 会话草稿导入：固化为 draft 产物（保留来源会话指针）。 */
export function importArtifactFromDraft(
  conversationId: number,
  draftId: number,
): Promise<ArtifactDetail> {
  return requestJsonAnyMethod(`${API_BASE}/v1/artifacts/from-draft`, "POST", {
    body: { conversation_id: conversationId, draft_id: draftId },
  }) as Promise<ArtifactDetail>;
}

// ── 空白建板（wire-dashboard-blank-create 任务 1.2 web 面）──────────────────
// 契约：POST /api/v1/artifacts（Bearer 登录身份）body {kind:"dashboard",
// title} → 201 ArtifactDetail（draft v1，spec = 最小空板 {title,
// spec:{title, panels:[]}}，origin_conversation_id=null）。kind 白名单 v0
// 仅 dashboard、title 非空——非法 422 带 detail 如实上抛（走既有
// requestJson POST 通道与 ConsoleApiError 语义，与其它创建端点一致）。

/** 空白新建看板：免 cell 免 AI 的出生路径（免费档第一块板）。 */
export function createBlankDashboard(title: string): Promise<ArtifactDetail> {
  return requestJson(`${API_BASE}/v1/artifacts`, {
    method: "POST",
    body: { kind: "dashboard", title },
  }) as Promise<ArtifactDetail>;
}

// ── 表单产物（wire-form-builder 任务 1.2/4.1 web 面）──────────────────────
// 契约：POST /api/v1/artifacts body {kind:"form", title} → 201 ArtifactDetail
// （draft v1，spec = 最小空表单 {title, spec:{title, datasetId:null, fields:[]}}，
// origin_conversation_id=null）。空白创建白名单 (dashboard, form)——form 与
// dashboard 同属可空白出生的 kind；保存走既有 PUT 版本链（saveArtifactSpec），
// spec 结构见 console/form/formSpec.ts。

/** 空白新建表单：选数据集配字段的录入面出生路径（v0 不入 AI 工具白名单）。 */
export function createBlankForm(title: string): Promise<ArtifactDetail> {
  return requestJson(`${API_BASE}/v1/artifacts`, {
    method: "POST",
    body: { kind: "form", title },
  }) as Promise<ArtifactDetail>;
}

/** 空白建告警规则（wire-alert-evaluation）：POST kind=alert_rule → 模板壳
 * draft v1（占位 dataset_id；PUT 真实规则 spec 才过契约校验，发布后武装）。 */
export function createBlankAlertRule(title: string): Promise<ArtifactDetail> {
  return requestJson(`${API_BASE}/v1/artifacts`, {
    method: "POST",
    body: { kind: "alert_rule", title },
  }) as Promise<ArtifactDetail>;
}

// ── 单记录公开页（wire-record-view 任务 4.1/4.2/4.3 web 面）────────────────
// 契约（api 仓 service 层 RECORD_VIEW_KIND 路线，同 form）：
// - POST /api/v1/artifacts body {kind:"record-view", title} → 201
//   ArtifactDetail（draft v1，spec = 最小空信封 {title, spec:{datasetId:null,
//   keyColumn:null, headerFields:[], sections:[]}}）；未绑数据集中间态可保存、
//   不可生成分享。spec 结构与草稿校验镜像见
//   console/recordview/recordViewSpec.ts。
// - 试预览 POST /api/v1/artifacts/{id}/record-view/preview body {key}
//   （console 会话身份）：按服务端当前版本 spec 组装返回与公开取数同形的
//   载荷（expires_at 恒 null），不生成公开链接。

/** 单记录公开页产物 kind 常量（与 api 侧 service 层同源）。 */
export const RECORD_VIEW_KIND = "record-view";

/** 空白新建单记录页：选主数据集/键列配模板的扫码页出生路径。 */
export function createBlankRecordView(title: string): Promise<ArtifactDetail> {
  return requestJson(`${API_BASE}/v1/artifacts`, {
    method: "POST",
    body: { kind: RECORD_VIEW_KIND, title },
  }) as Promise<ArtifactDetail>;
}

/** 试预览（console 会话身份；响应同公开取数形状、expires_at 恒 null）。 */
export async function previewRecordView(
  id: number,
  key: string,
): Promise<RecordViewPayload> {
  return normalizeRecordViewPayload(
    await requestJson(
      `${API_BASE}/v1/artifacts/${id}/record-view/preview`,
      { method: "POST", body: { key } },
    ),
  );
}

// ── 富文本文档（wire-rich-text-docs 任务 4.4 web 面，design D1/D3）────────
// 契约（api 仓 artifacts service 层 DOC_KIND 路线，同 form/record-view）：
// - POST /api/v1/artifacts body {kind:"doc", title} → 201 ArtifactDetail
//   （draft v1，spec = 最小空文档信封 {title, spec:{format:"markdown",
//   text:""}}）——出生即可进分栏编辑器写作。
// - GET/PUT /api/v1/artifacts/{id}/draft → {text: string|null, updated_at:
//   string|null}（草稿层：一对一的「工作区」行，自动保存覆盖写、**绝不**
//   产生版本行；无草稿行 text/updated_at 均 null，编辑器回退当前版本
//   spec.text——D3 读路径的「优先草稿、回退版本」判定在前端一处）。PUT
//   超限 422、非 doc kind 422、archived 409（与版本保存同映射）。
// - POST /api/v1/artifacts/{id}/share 放行 kind=doc（D5）：ttl_days 白名单
//   0|7|30|90 与 record-view 同源，响应形状同 RecordViewShareLink。

/** 富文本文档产物 kind 常量（与 api 侧 service 层 DOC_KIND 同源）。 */
export const DOC_KIND = "doc";

/** 空白新建文档：分栏编辑器的出生路径（spec 为 {format:"markdown",text:""}）。 */
export function createBlankDoc(title: string): Promise<ArtifactDetail> {
  return requestJson(`${API_BASE}/v1/artifacts`, {
    method: "POST",
    body: { kind: DOC_KIND, title },
  }) as Promise<ArtifactDetail>;
}

/** doc 草稿层读写响应：null=尚未自动保存过（回退当前版本 spec.text）。 */
export interface DocDraft {
  text: string | null;
  updated_at: string | null;
}

/** 读 doc 草稿层（编辑器打开路径；无草稿行 text=null 不回退由调用方判定）。 */
export function fetchDocDraft(id: number): Promise<DocDraft> {
  return requestJson(`${API_BASE}/v1/artifacts/${id}/draft`, {
    method: "GET",
  }) as Promise<DocDraft>;
}

/** 自动保存 doc 草稿层（覆盖写、不产生版本行；archived 409 / 超限 422）。 */
export function saveDocDraft(id: number, text: string): Promise<DocDraft> {
  return requestJsonAnyMethod(`${API_BASE}/v1/artifacts/${id}/draft`, "PUT", {
    body: { text },
  }) as Promise<DocDraft>;
}

/** 文档分享链接生成响应（形状与 record-view 同——同一个 share 机器）。 */
export type DocShareLink = RecordViewShareLink;

/** 生成文档分享链接（仅 published doc；ttlDays 0=不设期档，同 record-view）。 */
export function createDocShareLink(
  id: number,
  ttlDays: RecordViewTtlDays = 30,
): Promise<DocShareLink> {
  return requestJsonAnyMethod(
    `${API_BASE}/v1/artifacts/${id}/share`,
    "POST",
    { body: { ttl_days: ttlDays } },
  ) as Promise<DocShareLink>;
}

// ── 源包（wire-source-packs 任务 3.3）──────────────────────────────────
// 契约（tasks.md 2.1/2.2）：GET /api/v1/packs 与 GET /api/v1/packs/{id} 为
// 公开端点（匿名可读——目录公开通道，不过计量轨道）；详情响应为平铺分页
// {id,name_zh,name_en,desc,items,page,page_size,total}。订阅三端点
// （GET subscriptions / POST subscribe / DELETE unsubscribe）走 Bearer 登录
// 身份；重复订阅/退订幂等 200。

/** 源包概要（列表卡片用；指标数由服务端按 filter 实时算）。 */
export interface PackSummary {
  id: string;
  name_zh: string;
  name_en: string;
  desc: string;
  indicator_count: number;
}

/** 包内指标条目（详情清单行；code 即目录详情页锚点 #/catalog/{code}）。 */
export interface PackIndicator {
  code: string;
  name_zh: string;
  domain: string | null;
}

/** 包详情：平铺分页（items/total 与包元信息同层）。 */
export interface PackDetail extends PackSummary {
  items: PackIndicator[];
  page: number;
  page_size: number;
  total: number;
}

/** 订阅/退订响应（幂等：重复操作仍 200 返回终态）。 */
export interface PackSubscriptionResult {
  pack_id: string;
  subscribed: boolean;
}

/** 公开 GET（源包目录读）：有会话则附 Bearer，无会话不抛——匿名可浏览；
 * 非 2xx 错误语义与 requestJson 完全一致（ConsoleApiError 带 detail）。 */
async function requestJsonPublic(url: string): Promise<unknown> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const session = loadSession();
  if (session?.idToken) headers.Authorization = `Bearer ${session.idToken}`;
  let response: Response;
  try {
    response = await fetch(url, { method: "GET", headers });
  } catch (err) {
    if (err instanceof ConsoleApiError) throw err;
    throw new ConsoleApiError(
      `network error fetching ${url}: ${(err as Error).message}`,
    );
  }
  return readResponse(response, url);
}

/** 源包列表（公开；含各包指标数）。 */
export function fetchPacks(): Promise<PackSummary[]> {
  return requestJsonPublic(`${API_BASE}/v1/packs`) as Promise<PackSummary[]>;
}

/** 包详情 + 分页指标清单（公开；page 从 1 起）。 */
export function fetchPackDetail(
  packId: string,
  page = 1,
  pageSize = 20,
): Promise<PackDetail> {
  return requestJsonPublic(
    `${API_BASE}/v1/packs/${encodeURIComponent(packId)}?page=${page}&page_size=${pageSize}`,
  ) as Promise<PackDetail>;
}

/** 我的订阅（登录；返回已订阅包 id 列表）。 */
export function fetchPackSubscriptions(): Promise<string[]> {
  return requestJson(`${API_BASE}/v1/packs/subscriptions`, {
    method: "GET",
  }) as Promise<string[]>;
}

/** 订阅源包（登录；幂等）。 */
export function subscribePack(packId: string): Promise<PackSubscriptionResult> {
  return requestJson(
    `${API_BASE}/v1/packs/${encodeURIComponent(packId)}/subscribe`,
    { method: "POST" },
  ) as Promise<PackSubscriptionResult>;
}

/** 退订源包（登录；幂等）。 */
export function unsubscribePack(
  packId: string,
): Promise<PackSubscriptionResult> {
  return requestJson(
    `${API_BASE}/v1/packs/${encodeURIComponent(packId)}/unsubscribe`,
    { method: "DELETE" },
  ) as Promise<PackSubscriptionResult>;
}

// ── 仪表盘签名分享（wire-dashboard-share 任务 2 web 面）────────────────────
// 契约（api 仓 share 模块，proposal 冻结形状）：
// - POST /api/v1/artifacts/{id}/share（Bearer 登录身份）：仅 published 且
//   kind=dashboard 的产物可生成（其他 403/404 明确）；未配置
//   FD_WIRE_SHARE_SECRET 时 503 带原因。返回 {url, expires_at, version}——
//   注意 url 是**数据端点**相对路径（/api/v1/share/dashboard/{id}?t=…&v=…），
//   前端展示链接须另行构造（origin + #/share/dashboard/{id}?t=…&v={version}，
//   t 从响应 url 的 query 里解出）。
//   wire-share-ttl-options：body 可选 ttl_days（枚举 7|30|90，缺省 30；
//   非法值 422 列合法值）；expires_at 按所选有效期计算。

/** 分享链接生成响应（POST /artifacts/{id}/share）。 */
export interface DashboardShareLink {
  /** 数据端点相对路径（含 t/v 查询参数）——不是给用户看的公开页链接。 */
  url: string;
  /** 到期时间（naive UTC ISO；默认 30 天）。 */
  expires_at: string;
  /** 钉死的产物版本（生成时刻版本；改版后需重新生成链接）。 */
  version: number;
}

/** 公开分享端点返回的只读看板载荷。 */
export interface SharedDashboard {
  title: string;
  spec: Record<string, unknown>;
  version: number;
  shared_at: string;
}

/** 分享链接有效期三档（wire-share-ttl-options）：枚举对齐 api 契约。 */
export const SHARE_TTL_DAYS = [7, 30, 90] as const;

export type ShareTtlDays = (typeof SHARE_TTL_DAYS)[number];

/** 生成仪表盘分享链接（仅 published dashboard；draft/archived/非 dashboard
 *  由服务端 403/404 拒绝，未配置 secret 503——UI 如实呈现后端 detail）。
 *  ttlDays：有效期三选一（wire-share-ttl-options），缺省 30 天。 */
export function createDashboardShareLink(
  id: number,
  ttlDays: ShareTtlDays = 30,
): Promise<DashboardShareLink> {
  return requestJsonAnyMethod(
    `${API_BASE}/v1/artifacts/${id}/share`,
    "POST",
    { body: { ttl_days: ttlDays } },
  ) as Promise<DashboardShareLink>;
}

/** 公开取分享看板（无鉴权端点——绝不携带登录头，未登录接收者直接打开）。
 *  404 = 链接无效/过期/产物下线（服务端统一口径，不区分原因）；
 *  503 = 分享功能未配置（FD_WIRE_SHARE_SECRET 缺失）。 */
export async function fetchSharedDashboard(
  id: number,
  token: string,
  version?: number | null,
): Promise<SharedDashboard> {
  const params = new URLSearchParams({ t: token });
  if (version != null) params.set("v", String(version));
  const url = `${API_BASE}/v1/share/dashboard/${id}?${params}`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
    });
  } catch (err) {
    if (err instanceof ConsoleApiError) throw err;
    throw new ConsoleApiError(
      `network error fetching ${url}: ${(err as Error).message}`,
    );
  }
  return readResponse(response, url) as Promise<SharedDashboard>;
}

// ── 表单分享与公开提交（wire-form-builder 任务 3.1/3.3 web 面）──────────────
// 契约（api 仓 share 模块，proposal 冻结形状）：
// - POST /api/v1/artifacts/{id}/share 放行 kind∈{dashboard, form}（Bearer
//   登录身份）：form 产物响应 url 路径分派为 /api/v1/share/form/{id}?t=…&v=…
//   （TTL 选项、published 门、secret 禁用语义与 dashboard 一致）。
// - 公开 GET /api/v1/share/form/{id}?t&v → {title, spec(钉死版本信封原样),
//   version, expires_at}——spec 内只有字段定义与文案，零行数据；失败统一
//   404（令牌/版本/发布态任一不满足），secret 未配置 503。
// - 公开 POST /api/v1/share/form/{id}/submit?t&v body {values} → 201
//   {ok:true, message:成功文案}；422 detail=字段级原因；429=令牌限流/行数闸；
//   404=链接失效。公开面免登录——绝不携带 Bearer。

/** 公开分享表单载荷（钉死版本：spec 为产物信封原样，仅字段定义与文案）。 */
export interface SharedForm {
  title: string;
  spec: Record<string, unknown>;
  version: number;
  expires_at: string;
}

/** 公开表单提交响应（201 {ok:true, message}）。 */
export interface SharedFormSubmitResult {
  ok: boolean;
  message: string;
}

/** 生成表单分享链接（仅 published form；url 路径为 /api/v1/share/form/…）。 */
export function createFormShareLink(
  id: number,
  ttlDays: ShareTtlDays = 30,
): Promise<DashboardShareLink> {
  return requestJsonAnyMethod(
    `${API_BASE}/v1/artifacts/${id}/share`,
    "POST",
    { body: { ttl_days: ttlDays } },
  ) as Promise<DashboardShareLink>;
}

/** 公开取分享表单（无鉴权端点——绝不携带登录头，未登录填写者直接打开）。
 *  404 = 链接无效/过期/产物下线（统一口径）；503 = 分享功能未配置。 */
export async function fetchSharedForm(
  id: number,
  token: string,
  version?: number | null,
): Promise<SharedForm> {
  const params = new URLSearchParams({ t: token });
  if (version != null) params.set("v", String(version));
  const url = `${API_BASE}/v1/share/form/${id}?${params}`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
    });
  } catch (err) {
    if (err instanceof ConsoleApiError) throw err;
    throw new ConsoleApiError(
      `network error fetching ${url}: ${(err as Error).message}`,
    );
  }
  return readResponse(response, url) as Promise<SharedForm>;
}

/** 公开表单提交端点地址（t/v 进 query）——FormRuntime public 态的提交目标。 */
export function sharedFormSubmitUrl(
  id: number,
  token: string,
  version: number | null,
): string {
  const params = new URLSearchParams({ t: token });
  if (version != null) params.set("v", String(version));
  return `${API_BASE}/v1/share/form/${id}/submit?${params}`;
}

// ── 公开表单选项解析端点（wire-form-entry-advanced 任务 2.3/5.1 web 面）──────
// 契约：GET /api/v1/share/form/{id}/options?t=&v=（无鉴权端点——绝不携带
// 登录头，照 fetchSharedForm 模式裸 fetch）：令牌验签+钉死版本，仅返回该
// 版本声明了 optionsFromDataset 的字段之选项去重值（升序、截断标注）与
// fillFrom 回填映射（fills 键=fillFrom 声明列 → 选项值 → 值）；失败统一
// 404（令牌/发布态/版本行任一不满足）；未声明动态选项的表单返回空集。

/** 公开选项端点单字段载荷：去重选项 + 截断标注 + 回填映射 + 失效错误。 */
export interface SharedFormFieldOptions {
  options: string[];
  truncated: boolean;
  /** 引用数据集已删除/列不在快照等失效态的可读错误（不静默空白）。 */
  error?: string;
  /** 回填映射：fillFrom 声明列 → 选项值 → 该行该列取值。 */
  fills?: Record<string, Record<string, string>>;
}

/** 公开选项端点载荷（fields 键 = 声明动态选项的 select 字段 column）。 */
export interface SharedFormOptions {
  fields: Record<string, SharedFormFieldOptions>;
}

/** 公开选项解析端点地址（t/v 进 query；v 同 submit 的钉死版本语义）。 */
export function sharedFormOptionsUrl(
  id: number,
  token: string,
  version: number | null,
): string {
  const params = new URLSearchParams({ t: token });
  if (version != null) params.set("v", String(version));
  return `${API_BASE}/v1/share/form/${id}/options?${params}`;
}

/** 公开取分享表单选项（无鉴权端点——绝不携带登录头；404=令牌/版本失效
 *  统一口径，错误语义与 fetchSharedForm 一致）。 */
export async function fetchSharedFormOptions(
  id: number,
  token: string,
  version?: number | null,
): Promise<SharedFormOptions> {
  const url = sharedFormOptionsUrl(id, token, version ?? null);
  let response: Response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
    });
  } catch (err) {
    if (err instanceof ConsoleApiError) throw err;
    throw new ConsoleApiError(
      `network error fetching ${url}: ${(err as Error).message}`,
    );
  }
  return readResponse(response, url) as Promise<SharedFormOptions>;
}

/** 公开写端点（无鉴权头、无会话）：公开表单提交等——错误语义与 requestJson
 *  完全一致（ConsoleApiError 带 status/detail，如 422 字段级原因、429 限流）。 */
export async function postPublicJson(url: string, body: unknown): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
  } catch (err) {
    if (err instanceof ConsoleApiError) throw err;
    throw new ConsoleApiError(
      `network error fetching ${url}: ${(err as Error).message}`,
    );
  }
  return readResponse(response, url);
}

/** 公开读端点（无鉴权头、无会话，wire-form-entry-advanced）：公开选项解析
 *  等——调用方传完整带 t/v 的地址；错误语义与 postPublicJson 一致。 */
export async function fetchPublicJson(url: string): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
    });
  } catch (err) {
    if (err instanceof ConsoleApiError) throw err;
    throw new ConsoleApiError(
      `network error fetching ${url}: ${(err as Error).message}`,
    );
  }
  return readResponse(response, url);
}

// ── 单记录公开页公开取数与分享（wire-record-view 任务 3/4.3 web 面）────────
// 契约（api 仓 share 模块，proposal 冻结形状）：
// - GET /api/v1/share/record-view/{id}?t=<令牌>&k=<urlencoded 键值>（无鉴权
//   端点——裸 fetch 绝不带 Authorization）：验签+钉死版本后按 spec 声明查询
//   组装，返回 {title, version, expires_at, found, header[], sections[]}；
//   found=false 为「无此记录」态（200 非错误）；令牌/发布态错统一 404；
//   secret 未配置 503；限流 429 带 Retry-After。header value 与 rows 单元格
//   可为数字/字符串（前端统一 String()，无查表）。
// - POST /api/v1/artifacts/{id}/share 放行 kind=record-view：ttl_days 白名单
//   在 7|30|90 基础上新增 **0**（0=不设期·印刷物档，expires_at 返回 null）。

/** record-view 分享链接生成响应（0 档 expires_at 为 null——如实）。 */
export interface RecordViewShareLink {
  /** 数据端点相对路径（含 t 查询参数）——给接收者的公开页链接另行构造。 */
  url: string;
  expires_at: string | null;
  /** 钉死的产物版本（生成时刻版本）。 */
  version: number;
}

/** record-view 分享有效期四档：0=不设期（印刷物）+ 7/30/90 天。 */
export const RECORD_VIEW_TTL_DAYS = [0, 7, 30, 90] as const;

export type RecordViewTtlDays = (typeof RECORD_VIEW_TTL_DAYS)[number];

/** 公开取数端点地址（t/k 进 query；k 由调用方 urlencode）。 */
export function sharedRecordViewDataUrl(
  id: number,
  token: string,
  key: string,
): string {
  const params = new URLSearchParams({ t: token, k: key });
  return `${API_BASE}/v1/share/record-view/${id}?${params}`;
}

/** 生成单记录页分享链接（仅 published record-view；ttlDays 0=不设期档）。 */
export function createRecordViewShareLink(
  id: number,
  ttlDays: RecordViewTtlDays = 30,
): Promise<RecordViewShareLink> {
  return requestJsonAnyMethod(
    `${API_BASE}/v1/artifacts/${id}/share`,
    "POST",
    { body: { ttl_days: ttlDays } },
  ) as Promise<RecordViewShareLink>;
}

/** 公开取单记录页数据（无鉴权端点——绝不携带登录头，扫码接收者直接打开；
 *  404=链接无效/过期/产物下线统一口径；503=分享未配置；429=令牌限流）。
 *  响应经 normalizeRecordViewPayload 归一（found=false 为 200 的「无此记录」
 *  态，由页面如实呈现——不与 404 令牌态混淆）。 */
export async function fetchSharedRecordView(
  id: number,
  token: string,
  key: string,
): Promise<RecordViewPayload> {
  const url = sharedRecordViewDataUrl(id, token, key);
  let response: Response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
    });
  } catch (err) {
    if (err instanceof ConsoleApiError) throw err;
    throw new ConsoleApiError(
      `network error fetching ${url}: ${(err as Error).message}`,
    );
  }
  return normalizeRecordViewPayload(await readResponse(response, url));
}

/** 给接收者的公开页完整链接（分享面/二维码内容）：origin +
 *  /#/share/record-view/{id}?t=…&k=…（t/k 均按分量 urlencode；随生成时刻
 *  令牌钉死版本，产物归档/取消发布后指向统一 404 失效页）。 */
export function buildRecordViewSharePageUrl(
  origin: string,
  id: number,
  token: string,
  key: string,
): string {
  return (
    `${origin}/#/share/record-view/${id}` +
    `?t=${encodeURIComponent(token)}&k=${encodeURIComponent(key)}`
  );
}

// ── 仪表盘手动刷新（wire-dashboard-refresh 任务 2.1 web 面；
//    wire-dashboard-query-contract 任务 2.3 响应扩展）──────────────────────
// 契约（api 仓 refresh 端点，proposal 冻结形状）：
// - POST /api/v1/artifacts/{id}/refresh（Bearer 登录身份）：仅 kind=dashboard、
//   archived 不可刷；服务端按契约决策路径——≥1 面板带合法 query → 契约路径
//   （零 LLM 确定性执行，响应 mode="contract" + 逐面板结果 panels）；0 个 →
//   无头 AI 刷新轮（按各面板 source 重取数，响应 mode="ai"）。结构布局不动、
//   以 PUT 语义落新版本。成功返回 {version, refreshed_at, mode?, panels?}。
// - 409 = 同产物刷新进行中（单飞锁，两路共用）；403 = 档位未开通（无 cell，
//   detail 带升档引导——仅 AI 路径）；502/503 = LLM/引擎失败或契约全失败
//   （detail 带原因，零半写）——UI 如实呈现后端 detail。

/** 契约刷新逐面板结果（mode="contract" 响应的 panels 项）。 */
export interface DashboardRefreshPanelResult {
  /** 面板 id（spec 内 id；标题由前端按 spec 解析呈现）。 */
  panel_id: string;
  /** refreshed=快照已更新；kept=无契约原样保留；failed=保留原快照并记录原因。 */
  status: "refreshed" | "kept" | "failed";
  /** failed 面板的失败原因（服务端口径，如实呈现）。 */
  error?: string;
}

/** 手动刷新响应（POST /artifacts/{id}/refresh）。 */
export interface DashboardRefreshResult {
  /** 刷新落盘的新版本号（客户端据此提示并重拉详情）。 */
  version: number;
  /** 刷新完成时间（服务端 ISO 时间戳）。 */
  refreshed_at: string;
  /** 刷新路径：contract=确定性契约执行（零 LLM）；ai=无头 AI 轮（旧看板）。
   *  可选字段——旧后端/测试种子不带 mode 时按 ai 呈现（向后兼容）。 */
  mode?: "contract" | "ai";
  /** 逐面板结果（仅契约路径携带；AI 路径缺省或 null）。 */
  panels?: Array<DashboardRefreshPanelResult> | null;
}

/** 手动刷新仪表盘快照（draft/published 可刷；成功返回新版本号与时间）。 */
export function refreshDashboard(id: number): Promise<DashboardRefreshResult> {
  return requestJsonAnyMethod(
    `${API_BASE}/v1/artifacts/${id}/refresh`,
    "POST",
  ) as Promise<DashboardRefreshResult>;
}

// ── 契约试取（wire-panel-query-binding 任务 2.1 web 面）────────────────────
// 契约（api 仓 artifacts/routes.py，proposal 冻结形状）：
// POST /api/v1/artifacts/query-preview（Bearer 登录身份）——入参 = 面板 query
// 同形（dataset_id/filters/sort/limit/columns，DashboardPanelQuery）；复用
// 契约刷新同一执行路径（parse_panel_query → _query_dataset → _build_snapshot），
// 返回 {snapshot:{columns,rows}, query:{归一后}}——所见即所刷（试取与刷新
// 同一条代码路径，杜绝两口径）。错误：422=入参非法（parse 拒收）；
// 404=数据集不存在/他企；400=查询执行失败（引擎拒绝/列不存在，detail 带
// 原因）——一律 ConsoleApiError 带 detail，UI 如实呈现。

/** 契约试取响应：snapshot 与面板快照同形（列序=投影列序）；query 为服务端
 *  归一后契约（limit 缺省补 100、columns 空为 null 等，前端写回前再过
 *  normalizePanelQuery）。 */
export interface QueryPreviewResult {
  snapshot: {
    columns: string[];
    rows: Array<Array<string | number | null>>;
  };
  query: Record<string, unknown>;
}

/** 试取面板契约查询（不落库——结果只进编辑器本地预览快照）。 */
export function queryPreview(
  payload: DashboardPanelQuery,
): Promise<QueryPreviewResult> {
  return requestJson(`${API_BASE}/v1/artifacts/query-preview`, {
    method: "POST",
    body: payload,
  }) as Promise<QueryPreviewResult>;
}

// ── 定时刷新调度（wire-refresh-scheduler 任务 4.1 web 面）──────────────────
// 契约（api 仓 schedules 路由，proposal 冻结形状；Bearer 登录身份，跨客户 404）：
// - GET /api/v1/refresh-schedules：本人（本客户）调度行数组；
// - POST /api/v1/refresh-schedules：body
//   {target_type: "dataset"|"artifact", target_id, interval_minutes}
//   → 新建行（服务端校验属主 / 类型——dataset 须 derived 来源、artifact 须
//   kind=dashboard / 间隔地板 dataset ≥60、artifact ≥360 / 每客户配额，
//   非法 4xx 带 detail 如实呈现）；
// - DELETE /api/v1/refresh-schedules/{id} 与 POST /{id}/pause|resume
//   → 返回操作后的调度行。

/** 调度目标类型：派生数据集（复用 dataset_refresh，零 LLM 成本）与
 *  dashboard 产物（无头 AI 刷新轮）。 */
export type RefreshScheduleTargetType = "dataset" | "artifact";

/** 调度行（refresh_schedules 表的客户面投影）。 */
export interface RefreshSchedule {
  id: number;
  target_type: RefreshScheduleTargetType;
  /** dataset 目标 = 数据集 id；artifact 目标 = 产物 id（字符串化）。 */
  target_id: string;
  interval_minutes: number;
  /** 下次运行时间（naive UTC ISO；暂停期间不推进）。 */
  next_run_at: string;
  paused: boolean;
  /** 首轮前为 null；取值 pending | running | ok | failed。 */
  last_status: "pending" | "running" | "ok" | "failed" | null;
  last_run_at: string | null;
  /** 上次失败原因（last_status=failed 时非空；如实呈现）。 */
  last_error: string | null;
  created_at: string;
}

/** 本客户调度列表（含 dataset 与 artifact 两类目标）。 */
export function listRefreshSchedules(): Promise<RefreshSchedule[]> {
  return requestJson(`${API_BASE}/v1/refresh-schedules`, {
    method: "GET",
  }) as Promise<RefreshSchedule[]>;
}

/** 创建调度（间隔地板与配额由服务端校验，4xx detail 如实呈现）。 */
export function createRefreshSchedule(payload: {
  target_type: RefreshScheduleTargetType;
  target_id: string;
  interval_minutes: number;
}): Promise<RefreshSchedule> {
  return requestJson(`${API_BASE}/v1/refresh-schedules`, {
    method: "POST",
    body: payload,
  }) as Promise<RefreshSchedule>;
}

/** 删除调度（返回被删除的行；目标失存时调度器也会自动清理）。 */
export function deleteRefreshSchedule(id: number): Promise<RefreshSchedule> {
  return requestJson(`${API_BASE}/v1/refresh-schedules/${id}`, {
    method: "DELETE",
  }) as Promise<RefreshSchedule>;
}

/** 暂停调度（保留行与 next_run_at，不再到点执行）。 */
export function pauseRefreshSchedule(id: number): Promise<RefreshSchedule> {
  return requestJson(`${API_BASE}/v1/refresh-schedules/${id}/pause`, {
    method: "POST",
  }) as Promise<RefreshSchedule>;
}

/** 恢复已暂停的调度。 */
export function resumeRefreshSchedule(id: number): Promise<RefreshSchedule> {
  return requestJson(`${API_BASE}/v1/refresh-schedules/${id}/resume`, {
    method: "POST",
  }) as Promise<RefreshSchedule>;
}

// ── 变换管线图（wire-transform-pipeline-view 任务 2.2 web 面）────────────────
// 契约：GET /api/v1/pipeline/graph（Bearer 登录身份，require_console_customer_id
// 按映射客户隔离，他企节点/边不出现）——纯本地元数据组装、零引擎调用：
// nodes 为本企业全部数据集（含拓扑深度 depth：源=0，派生按链深）；
// edges 为父→子派生边（derived 的 source_ref 解析父 key，op v0 统一
// derived）；环/孤儿边跳过并计入 anomalies；节点上限 500、超出 truncated。

/** 管线图节点（数据集投影 + 拓扑深度标注）。 */
export interface PipelineGraphNode {
  dataset_id: string;
  key: string;
  name: string;
  source_kind: DatasetSourceKind;
  /** api（免费档轻量存储）/ cell（引擎物化）；未下发给 null。 */
  storage_tier: string | null;
  row_count: number;
  updated_at: string;
  /** 拓扑深度：源=0，派生逐级 +1（分层布局用）。 */
  depth: number;
  /** 定时重派生登记 cron；无登记为 null。 */
  schedule_cron: string | null;
}

/** 派生边：from（父）→ to（子）；op v0 统一 derived。 */
export interface PipelineGraphEdge {
  from_dataset_id: string;
  to_dataset_id: string;
  op: string;
}

/** 企业级数据集派生图。 */
export interface PipelineGraph {
  nodes: PipelineGraphNode[];
  edges: PipelineGraphEdge[];
  /** 被跳过的环/孤儿边计数（异常数据态，如实呈现）。 */
  anomalies: number;
  /** 节点超 500 上限被截断。 */
  truncated: boolean;
}

/** 本企业数据集派生图（管线视图数据源）。 */
export function getPipelineGraph(): Promise<PipelineGraph> {
  return requestJson(`${API_BASE}/v1/pipeline/graph`, {
    method: "GET",
  }) as Promise<PipelineGraph>;
}

// ── 重派生与定时重派生（wire-pipeline-actions 任务 1.1 web 面）──────────────
// 契约（api 仓 data_plane/data_routes.py，端点早已接受 console Bearer 会话）：
// - POST /api/v1/datasets/{id}/refresh?cascade=true|false（默认 true）：响应 =
//   DatasetOut 数据集字段 + cascade: [{dataset_id,name,depth,status,error}]——
//   本体刷新 + 级联刷新派生子链（单环失败隔离，失败级带原因如实呈现）；
//   仅 derived + cell 档（api 档 403 / 非 derived 400，detail 原样呈现）。
// - PUT /api/v1/datasets/{id}/schedule body {cron}（五字段 cron，naive UTC——
//   客户时区由登记侧换算的 v1 口径）→ 登记态；
// - GET 同路径 → {dataset_id, cron|null, enabled, last_run_at|null,
//   last_status|null, last_error|null, last_rows|null}（无登记时仅
//   dataset_id/cron=null/enabled=false）；
// - DELETE 同路径 → {dataset_id, removed: true}（幂等）。

/** 级联刷新逐级结果（refresh 响应的 cascade 项——单环失败隔离）。 */
export interface DatasetRefreshCascadeEntry {
  dataset_id: string;
  name: string;
  /** 拓扑深度（直接子级 = 1，逐级 +1）。 */
  depth: number;
  status: "ok" | "failed";
  /** failed 级的失败原因（服务端口径，如实呈现）。 */
  error: string | null;
}

/** 重派生响应：数据集字段（DatasetOut 形状）+ 级联各级结果摘要。 */
export interface DatasetRefreshResult extends Dataset {
  cascade: DatasetRefreshCascadeEntry[];
}

/** 定时重派生登记态（GET/PUT /datasets/{id}/schedule 响应；last_* 仅
 *  已登记且有运行后出现——按可选字段容错读取）。 */
export interface DatasetSchedule {
  dataset_id: string;
  /** 五字段 cron（naive UTC）；无登记为 null。 */
  cron: string | null;
  enabled: boolean;
  last_run_at?: string | null;
  /** 运行状态（success / partial_failure / failed / quota_exhausted…，原样呈现）。 */
  last_status?: string | null;
  last_error?: string | null;
  last_rows?: number | null;
}

/** 重派生：按存档 spec 对源当前有效态重算（默认级联派生子链）。 */
export function refreshDataset(
  datasetId: string,
  cascade = true,
): Promise<DatasetRefreshResult> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/refresh?cascade=${cascade}`,
    { method: "POST" },
  ) as Promise<DatasetRefreshResult>;
}

/** 查看定时重派生登记态（无登记：cron=null / enabled=false）。 */
export function getDatasetSchedule(
  datasetId: string,
): Promise<DatasetSchedule> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/schedule`,
    { method: "GET" },
  ) as Promise<DatasetSchedule>;
}

/** 登记/更新定时重派生（五字段 cron，naive UTC——时区换算属登记侧）。 */
export function putDatasetSchedule(
  datasetId: string,
  cron: string,
): Promise<DatasetSchedule> {
  return requestJsonAnyMethod(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/schedule`,
    "PUT",
    { body: { cron } },
  ) as Promise<DatasetSchedule>;
}

/** 删除定时重派生登记（幂等；删除后回到未登记态）。 */
export function deleteDatasetSchedule(
  datasetId: string,
): Promise<{ dataset_id: string; removed: boolean }> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/schedule`,
    { method: "DELETE" },
  ) as Promise<{ dataset_id: string; removed: boolean }>;
}

// ── 实体语义层（wire-entity-layer 任务 3.1 web 面）──────────────────────
// 契约（api 仓 entities 路由，Bearer 登录身份，跨客户一律 404 不泄露）：
// - GET  /api/v1/entities → {entities:[实体项]}（信封形状冻结）；
// - POST /api/v1/entities body {name,dataset_id,pk_columns,description?}
//   → 201 实体项（裸项，非信封）；
// - PUT  /api/v1/entities/{id} body 同创建（整量替换，重校验）
//   → 200 实体项；
// - DELETE /api/v1/entities/{id} → {deleted:true, deleted_relationships:N}
//   （级联删两端关系，数量如实回带）；
// - POST /api/v1/entities/{id}/relationships body
//   {to_entity_id,from_column,to_column,kind,label?} → 201 关系项
//   （toColumn 必须 ∈ 目标实体主键——关系指向身份的纪律，服务端裁决）；
// - DELETE /api/v1/entities/{id}/relationships/{rel_id} → {deleted:true}
//   （rel 必须以该实体为端点）；
// - POST /api/v1/entities/{id}/relationships/{rel_id}/materialize body
//   {name?}（可空）→ 201 DatasetOut（既有数据集项形状：source_kind=
//   "derived"、storage_tier="cell"）——LEFT JOIN 语义一键物化（wire-entity-
//   link）；两端数据集均须 cell 档（任一免费档 403 档位闸如实）；
// - GET  /api/v1/entities/graph → {nodes:[实体项], edges:[关系项]}——
//   控制台图谱页与 AI 上下文注入的唯一同源；空图谱空数组（200）。
// 错误口径：404 detail 如实（失存/他企）、409 冲突（重名/重复绑定/四元组
// 重复）、422 逐条（FastAPI 标准数组 → ConsoleApiError.detailItems）、
// 物化另有 403 档位闸 / 502 引擎不可达或旧 cell 无 link 工具（detail 如实）。

/** 实体绑定的数据集摘要（列表/图谱节点携带；列清单不在摘要内——列真相在
 *  Dataset.columns_json，声明时校验、之后漂移不追改）。 */
export interface EntityDatasetSummary {
  id: string;
  key: string;
  name: string;
  row_count: number;
  /** api（免费档轻量存储）/ cell（引擎物化）；未下发给 null。 */
  storage_tier: string | null;
}

/** 实体项（列表条目与图谱节点同形）。 */
export interface EntityItem {
  id: number;
  /** 业务名（客户内唯一）。 */
  name: string;
  description: string | null;
  /** 主键列（非空、⊆ 绑定数据集列快照）。 */
  pk_columns: string[];
  dataset: EntityDatasetSummary;
}

/** 关系基数枚举（v0 两值）。 */
export const ENTITY_RELATIONSHIP_KINDS = ["n:1", "1:1"] as const;

export type EntityRelationshipKind = (typeof ENTITY_RELATIONSHIP_KINDS)[number];

/** 关系项（图谱边同形）：源实体列 → 目标实体主键列。 */
export interface EntityRelationship {
  id: number;
  from_entity_id: number;
  from_column: string;
  to_entity_id: number;
  to_column: string;
  kind: EntityRelationshipKind;
  label: string | null;
}

/** 实体图谱（控制台页与 AI 注入同源；空 = 空数组而非 404）。 */
export interface EntityGraph {
  nodes: EntityItem[];
  edges: EntityRelationship[];
}

/** 创建实体入参（TS 侧 camelCase；请求边界翻译为 api 惯例 snake_case）。 */
export interface EntityCreateInput {
  name: string;
  datasetId: string;
  pkColumns: string[];
  description?: string;
}

/** 本企业实体列表（解包 {entities:[…]} 信封；裸数组宽容兼容）。 */
export async function fetchEntities(): Promise<EntityItem[]> {
  const body = await requestJson(`${API_BASE}/v1/entities`, {
    method: "GET",
  });
  if (Array.isArray(body)) return body as EntityItem[];
  const list = (body as { entities?: unknown }).entities;
  return Array.isArray(list) ? (list as EntityItem[]) : [];
}

/** 声明实体（201 实体项；409 重名/重复绑定、422 列级逐条）。 */
export function createEntity(payload: EntityCreateInput): Promise<EntityItem> {
  return requestJson(`${API_BASE}/v1/entities`, {
    method: "POST",
    body: {
      name: payload.name,
      dataset_id: payload.datasetId,
      pk_columns: payload.pkColumns,
      description: payload.description,
    },
  }) as Promise<EntityItem>;
}

/** 改实体（整量替换——api 侧 PUT 与创建同模型：name/dataset_id/pk_columns/
 *  description 全量重走校验链）。 */
export function updateEntity(
  id: number,
  payload: EntityCreateInput,
): Promise<EntityItem> {
  return requestJsonAnyMethod(`${API_BASE}/v1/entities/${id}`, "PUT", {
    body: {
      name: payload.name,
      dataset_id: payload.datasetId,
      pk_columns: payload.pkColumns,
      description: payload.description,
    },
  }) as Promise<EntityItem>;
}

/** 删除实体（级联删两端关系；响应回带级联数量）。 */
export function deleteEntity(
  id: number,
): Promise<{ deleted: boolean; deleted_relationships: number }> {
  return requestJson(`${API_BASE}/v1/entities/${id}`, {
    method: "DELETE",
  }) as Promise<{ deleted: boolean; deleted_relationships: number }>;
}

/** 关系创建入参（toColumn 须 ∈ 目标实体主键——纪律）。 */
export interface EntityRelationshipInput {
  toEntityId: number;
  fromColumn: string;
  toColumn: string;
  kind: EntityRelationshipKind;
  label?: string;
}

/** 声明关系（源实体列 → 目标实体主键列；201 关系项）。 */
export function createEntityRelationship(
  entityId: number,
  payload: EntityRelationshipInput,
): Promise<EntityRelationship> {
  return requestJson(`${API_BASE}/v1/entities/${entityId}/relationships`, {
    method: "POST",
    body: {
      to_entity_id: payload.toEntityId,
      from_column: payload.fromColumn,
      to_column: payload.toColumn,
      kind: payload.kind,
      label: payload.label,
    },
  }) as Promise<EntityRelationship>;
}

/** 删除关系（rel 必须以 entityId 为端点；{deleted:true}）。 */
export function deleteEntityRelationship(
  entityId: number,
  relationshipId: number,
): Promise<{ deleted: boolean }> {
  return requestJson(
    `${API_BASE}/v1/entities/${entityId}/relationships/${relationshipId}`,
    { method: "DELETE" },
  ) as Promise<{ deleted: boolean }>;
}

/** 物化关系（wire-entity-link）：按声明把两端 cell 数据集 LEFT JOIN 成新
 *  派生数据集——name 仅非空携带（TS 侧直传，无 camelCase 字段需翻译；
 *  缺省让服务端取「{左实体名}×{右实体名} 连接」）。
 *  201 回 DatasetOut（source_kind="derived"、storage_tier="cell"、撞名右列
 *  带 right_ 前缀）；404 关系或数据集失存、403 任一端免费档、422 列漂移
 *  逐条、502 引擎/旧 cell 无 link 工具（detail 如实）。 */
export function materializeEntityRelationship(
  entityId: number,
  relationshipId: number,
  name?: string,
): Promise<Dataset> {
  return requestJson(
    `${API_BASE}/v1/entities/${entityId}/relationships/${relationshipId}/materialize`,
    {
      method: "POST",
      // name 仅非空携带（空体 {} 也走 JSON——端点 body 可空）。
      body: name ? { name } : {},
    },
  ) as Promise<Dataset>;
}

/** 实体图谱（nodes=实体项、edges=关系项；空图谱空数组）。 */
export function fetchEntityGraph(): Promise<EntityGraph> {
  return requestJson(`${API_BASE}/v1/entities/graph`, {
    method: "GET",
  }) as Promise<EntityGraph>;
}

// ── 团队空间（wire-team-spaces 任务 5 web 面）────────────────────────────
// 契约（api 仓 console team 路由，proposal 冻结形状；Bearer 登录身份）：
// - GET /api/console/team → {me:{sub,role}, members:[{logto_sub,role,
//   created_at,invited_by_sub?}], invites:[{id,code,role,expires_at,
//   created_at}]}（三角色可读——viewer/editor 读面与 owner 一致）；
//   wire-team-spaces-v2 追加 seats_used/seats_limit（档位席位；超配
//   grandfather——不踢人只停邀新）；
// - POST /api/console/team/invites body {role} → 201 {id,code,role,expires_at}
//   （owner-only：editor/viewer 403 detail「团队成员权限不足…」；超配 409
//   detail 含档位/席位/升级指引——UI 原样呈现）；
// - DELETE /api/console/team/invites/{id} → {deleted:true}（owner-only）；
// - DELETE /api/console/team/members/{sub} → {deleted:true}（owner-only；
//   409 = owner 行不可撤，detail 带原因）；
// - GET /api/console/teams → {teams:[{customer_id,customer_name,role,
//   is_active}], active_customer_id}（wire-team-spaces-v2：一身份多队清单；
//   单队身份长度 1）；
// - POST /api/console/team/switch body {customer_id} → {active_customer_id}
//   （幂等；404 = 目标无效）；
// - PATCH /api/console/team/members/{sub} body {role:"editor"|"viewer"} →
//   更新后成员行（owner-only；409 = owner 行不可改）。
// 边界翻译纪律：wire 一律 snake_case（logto_sub/created_at/…），TS 侧一律
// camelCase（logtoSub/createdAt/…）——双向翻译收口在 api.ts 内，UI 不见下划线。

/** 团队角色三档（owner 全权 / editor 除团队管理与密钥物 / viewer 只读）。 */
export const TEAM_ROLES = ["owner", "editor", "viewer"] as const;

export type TeamRole = (typeof TEAM_ROLES)[number];

/** 可邀请席位两档（owner 不可经邀请产生）。 */
export const TEAM_INVITE_ROLES = ["editor", "viewer"] as const;

export type TeamInviteRole = (typeof TEAM_INVITE_ROLES)[number];

/** 本人的团队视角（GET /team 响应 me 段）。 */
export interface TeamMe {
  sub: string;
  role: TeamRole;
}

/** 团队成员行（UI 投影：wire snake_case 已在边界翻译）。 */
export interface TeamMember {
  logtoSub: string;
  role: TeamRole;
  /** 加入时间（ISO）。 */
  createdAt: string;
  /** 邀请人 sub（owner 首建行为 null）。 */
  invitedBySub: string | null;
}

/** 待用邀请行。 */
export interface TeamInvite {
  id: number;
  code: string;
  role: TeamInviteRole;
  /** 过期时间（ISO；24 小时窗）。 */
  expiresAt: string;
  createdAt: string;
}

/** 团队总览（名单 + 待用邀请 + 席位用量）。 */
export interface TeamOverview {
  me: TeamMe;
  members: TeamMember[];
  invites: TeamInvite[];
  /** 席位用量（wire-team-spaces-v2：档位席位；已用含全部成员）。 */
  seatsUsed: number;
  /** 席位上限（当前档位；超配 grandfather——不强制踢人，只停邀新）。 */
  seatsLimit: number;
}

/** 「我的团队」清单行（GET /api/console/teams 响应项；身份可属多队）。 */
export interface TeamMembership {
  customerId: number;
  customerName: string;
  role: TeamRole;
  isActive: boolean;
}

/** 我的团队清单信封（teams + 活跃队指针）。 */
export interface TeamsOut {
  teams: TeamMembership[];
  activeCustomerId: number;
}

/** 创建邀请响应（一次性码仅此一次明文回显）。 */
export interface TeamInviteCreated {
  id: number;
  code: string;
  role: TeamInviteRole;
  expiresAt: string;
}

/** wire role 值收窄（owner/editor/viewer 之外拒绝）。 */
function asTeamRole(raw: unknown): TeamRole | undefined {
  return raw === "owner" || raw === "editor" || raw === "viewer"
    ? raw
    : undefined;
}

/** wire 邀请角色收窄（仅 editor/viewer——owner 不经邀请产生）。 */
function asTeamInviteRole(raw: unknown): TeamInviteRole | undefined {
  return raw === "editor" || raw === "viewer" ? raw : undefined;
}

function asString(raw: unknown): string {
  return typeof raw === "string" ? raw : "";
}

/** wire members 段收窄翻译（snake_case → TeamMember[]；坏条目跳过）。 */
function parseTeamMembers(raw: unknown): TeamMember[] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((entry): TeamMember[] => {
    if (!entry || typeof entry !== "object") return [];
    const record = entry as Record<string, unknown>;
    const role = asTeamRole(record.role);
    const logtoSub = asString(record.logto_sub);
    if (!logtoSub || !role) return [];
    const invitedBy = asString(record.invited_by_sub);
    return [
      {
        logtoSub,
        role,
        createdAt: asString(record.created_at),
        invitedBySub: invitedBy || null,
      },
    ];
  });
}

/** wire invites 段收窄翻译（snake_case → TeamInvite[]；坏条目跳过）。 */
function parseTeamInvites(raw: unknown): TeamInvite[] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((entry): TeamInvite[] => {
    if (!entry || typeof entry !== "object") return [];
    const record = entry as Record<string, unknown>;
    const role = asTeamInviteRole(record.role);
    const code = asString(record.code);
    const id = record.id;
    if (typeof id !== "number" || !code || !role) return [];
    return [
      {
        id,
        code,
        role,
        expiresAt: asString(record.expires_at),
        createdAt: asString(record.created_at),
      },
    ];
  });
}

/** 团队总览（名单 + 待用邀请 + 席位用量；me 段 role 坏值按 viewer 最小权限
 *  回落；seats 坏值按 0 容错——契约字段 v2 起必有）。 */
export async function getTeam(): Promise<TeamOverview> {
  const body = await requestJson(`${API_BASE}/console/team`, {
    method: "GET",
  });
  const record = (body ?? {}) as Record<string, unknown>;
  const me = (record.me ?? {}) as Record<string, unknown>;
  return {
    me: {
      sub: asString(me.sub),
      role: asTeamRole(me.role) ?? "viewer",
    },
    members: parseTeamMembers(record.members),
    invites: parseTeamInvites(record.invites),
    seatsUsed: asSeatCount(record.seats_used),
    seatsLimit: asSeatCount(record.seats_limit),
  };
}

/** wire 席位数字段收窄（非负整数；坏值 0 容错）。 */
function asSeatCount(raw: unknown): number {
  return typeof raw === "number" && Number.isFinite(raw) && raw >= 0
    ? Math.floor(raw)
    : 0;
}

/** 生成一次性邀请码（owner-only；24 小时有效，绑定席位角色）。 */
export async function createTeamInvite(
  role: TeamInviteRole,
): Promise<TeamInviteCreated> {
  const body = await requestJson(`${API_BASE}/console/team/invites`, {
    method: "POST",
    // 请求边界翻译：wire 即 {role}（无 camelCase 字段，形状冻结）。
    body: { role },
  });
  const record = (body ?? {}) as Record<string, unknown>;
  const id = record.id;
  return {
    id: typeof id === "number" ? id : 0,
    code: asString(record.code),
    role: asTeamInviteRole(record.role) ?? role,
    expiresAt: asString(record.expires_at),
  };
}

/** 作废待用邀请（owner-only；{deleted:true}）。 */
export function revokeTeamInvite(
  id: number,
): Promise<{ deleted: boolean }> {
  return requestJson(`${API_BASE}/console/team/invites/${id}`, {
    method: "DELETE",
  }) as Promise<{ deleted: boolean }>;
}

/** 撤销成员（owner-only；owner 行 409、失存 404——detail 如实上抛）。 */
export function removeTeamMember(
  sub: string,
): Promise<{ deleted: boolean }> {
  return requestJson(
    `${API_BASE}/console/team/members/${encodeURIComponent(sub)}`,
    { method: "DELETE" },
  ) as Promise<{ deleted: boolean }>;
}

/** 「我的团队」清单（wire-team-spaces-v2：一身份可属多队；单队身份即
 *  teams 长度 1——UI 据此零打扰不渲染切换面；坏条目跳过）。 */
export async function getTeams(): Promise<TeamsOut> {
  const body = await requestJson(`${API_BASE}/console/teams`, {
    method: "GET",
  });
  const record = (body ?? {}) as Record<string, unknown>;
  const teams = Array.isArray(record.teams)
    ? record.teams.flatMap((entry): TeamMembership[] => {
        if (!entry || typeof entry !== "object") return [];
        const row = entry as Record<string, unknown>;
        const role = asTeamRole(row.role);
        const name = asString(row.customer_name);
        if (typeof row.customer_id !== "number" || !role || !name) return [];
        return [
          {
            customerId: row.customer_id,
            customerName: name,
            role,
            isActive: row.is_active === true,
          },
        ];
      })
    : [];
  const activeRaw = record.active_customer_id;
  return {
    teams,
    activeCustomerId:
      typeof activeRaw === "number"
        ? activeRaw
        : (teams.find((m) => m.isActive)?.customerId ?? teams[0]?.customerId ?? 0),
  };
}

/** 切换活跃队（幂等；404 = 目标无效——detail 如实上抛）。成功后调用方应
 *  整页刷新控制台数据（切换 = 切数据面，所有缓存面一并重建）。 */
export async function switchTeam(
  customerId: number,
): Promise<{ activeCustomerId: number }> {
  const body = await requestJson(`${API_BASE}/console/team/switch`, {
    method: "POST",
    // 请求边界翻译：wire 即 {customer_id}（snake_case 形状冻结）。
    body: { customer_id: customerId },
  });
  const active = (body as { active_customer_id?: unknown } | null)
    ?.active_customer_id;
  return {
    activeCustomerId: typeof active === "number" ? active : customerId,
  };
}

/** 就地变更成员角色 editor↔viewer（owner-only 后端门禁；owner 行 409、
 *  失存 404——detail 如实上抛；响应为更新后的成员行，边界翻译同名单行）。 */
export async function updateTeamMemberRole(
  sub: string,
  role: TeamInviteRole,
): Promise<TeamMember> {
  const body = await requestJsonAnyMethod(
    `${API_BASE}/console/team/members/${encodeURIComponent(sub)}`,
    "PATCH",
    { body: { role } },
  );
  const parsed = parseTeamMembers([body])[0];
  if (!parsed) {
    throw new ConsoleApiError(
      `invalid member row from PATCH /api/console/team/members/${sub}`,
    );
  }
  return parsed;
}

// ── 个人偏好（wire-personal-preferences web 面）──────────────────────────
// 契约（api 仓 preferences 路由，冻结形状；Bearer 登录身份，任意已映射身份
// 含 viewer 可读写自己的偏好）：
// - GET /api/console/me/preferences → 三键全量（未设=null，行不存在不 404）；
// - PUT 同路径：body 三键任意子集（缺键不动、显式 null 清空），成功响应 =
//   写后全量态（同 GET 形）；非法值 422 如实定位。
// 键面纪律：本契约键面冻结为 snake_case——wire 与 TS 侧同形（与 team/alerts
// 段的 camelCase 翻译纪律不同，不做边界改名）。

/** 界面语言偏好（localStorage 本地值的跨端同步锚点）。 */
export type PreferenceLocale = "zh" | "en";

/** 个人偏好全量态（GET 与 PUT 写后响应同形）。 */
export interface ConsolePreferences {
  ui_locale: PreferenceLocale | null;
  landing_tab: string | null;
  pinned_dashboard_id: number | null;
}

/** 偏好部分更新入参（缺键不动、显式 null 清空）。 */
export interface ConsolePreferencesPatch {
  ui_locale?: PreferenceLocale | null;
  landing_tab?: string | null;
  pinned_dashboard_id?: number | null;
}

/** 拉取本人偏好（三键全量；未映射身份 401 如实上抛）。 */
export function getPreferences(): Promise<ConsolePreferences> {
  return requestJson(`${API_BASE}/console/me/preferences`, {
    method: "GET",
  }) as Promise<ConsolePreferences>;
}

/** 部分更新本人偏好（返回写后全量态；非法值 422 如实上抛）。 */
export function putPreferences(
  patch: ConsolePreferencesPatch,
): Promise<ConsolePreferences> {
  return requestJsonAnyMethod(`${API_BASE}/console/me/preferences`, "PUT", {
    body: patch,
  }) as Promise<ConsolePreferences>;
}

// ── 告警执行：状态/事件/试运行（wire-alert-evaluation 任务 5 web 面）──────
// 契约（api 仓 alerts 路由，冻结形状；Bearer 登录身份，客户隔离）：
// - GET /api/v1/alerts/states → {"states": {"<artifact_id>": {state,
//   last_value, last_evaluated_at, last_error_at}}}——评估引擎按周期维护的
//   规则当前态（ok/firing）；从未评估的规则无键（前端按「未评估」呈现）；
// - GET /api/v1/alerts/events?artifact_id=&limit= → {"events":[{id,
//   artifact_id, kind, value, threshold, detail, created_at}]}——id 倒序，
//   kind 四枚举 firing/resolved/error/notify_failed；
// - POST /api/v1/artifacts/{id}/alert/test → 200 {value, would_fire}（试运行
//   零副作用：只算归约值与触发判定、不落事件不改状态）；422=spec 非法、
//   400=查询失败、非 alert_rule 404——detail 如实上抛（ConsoleApiError）。
// 边界翻译纪律与 team 段同：wire snake_case、TS camelCase，翻译收口在本文件。

/** 单条 alert_rule 的评估态（states map 值；snake→camel 已译）。 */
export interface AlertStateSummary {
  state: "ok" | "firing";
  /** 最近一次归约值（首评估前为 null）。 */
  lastValue: number | null;
  /** 最近评估时间（naive UTC ISO；未评估为 null）。 */
  lastEvaluatedAt: string | null;
  /** 最近评估出错时间（发生过 error 事件后非空；ok 态也可能带——如实呈现）。 */
  lastErrorAt: string | null;
}

/** artifact_id（字符串化）→ 评估态（从未评估的规则无键）。 */
export type AlertStatesMap = Record<string, AlertStateSummary>;

/** 告警事件（wire snake_case 已译 camelCase）。 */
export interface AlertEvent {
  id: number;
  artifactId: number;
  kind: "firing" | "resolved" | "error" | "notify_failed";
  value: number | null;
  threshold: number | null;
  /** 失败原因等补充说明（无则 null）。 */
  detail: string | null;
  createdAt: string;
}

/** 试运行结果：归约值 + 按当前阈值是否会触发（零副作用）。 */
export interface AlertTestResult {
  value: number;
  wouldFire: boolean;
}

/** wire 可空数值收窄（null/缺失/非有限数 → null）。 */
function asOptionalNumber(raw: unknown): number | null {
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

/** wire 可空时间串收窄（空串/缺失 → null）。 */
function asOptionalIso(raw: unknown): string | null {
  return typeof raw === "string" && raw ? raw : null;
}

/** wire 事件 kind 四枚举收窄。 */
function asAlertEventKind(
  raw: unknown,
): AlertEvent["kind"] | undefined {
  return raw === "firing" || raw === "resolved" || raw === "error" ||
    raw === "notify_failed"
    ? raw
    : undefined;
}

/** states map 单值收窄翻译（snake→camel；坏条目跳过）。 */
function parseAlertStateEntry(raw: unknown): AlertStateSummary | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const record = raw as Record<string, unknown>;
  if (record.state !== "ok" && record.state !== "firing") return undefined;
  return {
    state: record.state,
    lastValue: asOptionalNumber(record.last_value),
    lastEvaluatedAt: asOptionalIso(record.last_evaluated_at),
    lastErrorAt: asOptionalIso(record.last_error_at),
  };
}

/** 全量告警评估态（键保持 artifact_id 字符串、值译 camelCase）。 */
export async function getAlertStates(): Promise<AlertStatesMap> {
  const body = await requestJson(`${API_BASE}/v1/alerts/states`, {
    method: "GET",
  });
  const record = (body ?? {}) as Record<string, unknown>;
  const out: AlertStatesMap = {};
  if (record.states && typeof record.states === "object") {
    for (const [key, value] of Object.entries(
      record.states as Record<string, unknown>,
    )) {
      const parsed = parseAlertStateEntry(value);
      if (parsed) out[key] = parsed;
    }
  }
  return out;
}

/** 单条规则告警事件（id 倒序；解 {events:[…]} 信封并译 camelCase）。 */
export async function getAlertEvents(
  artifactId: number,
  limit?: number,
): Promise<AlertEvent[]> {
  const params = new URLSearchParams({ artifact_id: String(artifactId) });
  if (limit != null) params.set("limit", String(limit));
  const body = await requestJson(`${API_BASE}/v1/alerts/events?${params}`, {
    method: "GET",
  });
  const record = (body ?? {}) as Record<string, unknown>;
  const list = record.events;
  if (!Array.isArray(list)) return [];
  return list.flatMap((entry): AlertEvent[] => {
    if (!entry || typeof entry !== "object") return [];
    const row = entry as Record<string, unknown>;
    const kind = asAlertEventKind(row.kind);
    if (
      typeof row.id !== "number" ||
      typeof row.artifact_id !== "number" ||
      !kind
    ) {
      return [];
    }
    return [
      {
        id: row.id,
        artifactId: row.artifact_id,
        kind,
        value: asOptionalNumber(row.value),
        threshold: asOptionalNumber(row.threshold),
        detail: asOptionalIso(row.detail),
        createdAt: asOptionalIso(row.created_at) ?? "",
      },
    ];
  });
}

// ── 个人工作台聚合（wire-personal-workbench 任务 2.1 web 面）──────────────────
// 契约：GET /api/v1/workbench（Bearer 登录身份，只读，免费档可用零计量）→
// 六 section 单往返聚合；字段名逐字冻结（wire snake_case 原样保留，与
// CustomerDemand 同款直存风格——聚合面仅供工作台页呈现消费，不做 camel 翻译）。
// 边界：free_quota 仅免费档非 null（付费档 null）；demands.latest 无单为
// null；各 recent 列表 ≤5 条、可为空数组。响应仅含本客户数据（跨客户隔离
// 由服务端保证，与既有端点同纪律）。

/** 最近数据集行（datasets.recent 条目；storage_tier 为 "api" | "cell"）。 */
export interface WorkbenchDatasetRecent {
  id: string;
  name: string;
  storage_tier: string;
  row_count: number;
  size_bytes: number;
  updated_at: string;
}

/** 免费档数据集配额（与 GET /v1/datasets/quota 同语义嵌入；付费档 null）。
 *  max_* 为 `number | null`（wire-console-resilience：fail-closed 如实回
 *  null——消费方按「档位未知」降级，不抛错）。 */
export interface WorkbenchFreeQuota {
  used_datasets: number;
  max_datasets: number | null;
  max_rows_per_dataset: number | null;
}

/** 最近订阅源包行（packs.recent 条目；名称双语由静态定义下发）。 */
export interface WorkbenchPackRecent {
  pack_id: string;
  name_zh: string;
  name_en: string;
  subscribed_at: string;
}

/** 最近产物行（artifacts.recent 条目；status 为 draft/published/archived）。 */
export interface WorkbenchArtifactRecent {
  id: number;
  kind: string;
  title: string;
  status: string;
  updated_at: string;
}

/** 工作台聚合响应（字段名逐字冻结，见模块头注释）。 */
export interface WorkbenchSummary {
  datasets: {
    total: number;
    api_tier: number;
    cell_tier: number;
    recent: WorkbenchDatasetRecent[];
  };
  /** 免费档真实配额；付费档 null（呈现侧 null 即不渲染配额条）。 */
  free_quota: WorkbenchFreeQuota | null;
  packs: {
    subscribed: number;
    recent: WorkbenchPackRecent[];
  };
  artifacts: {
    active_total: number;
    // 六类展示口径（含表单 form）；服务端对清单外 kind 如实附加键，开放记录
    by_kind: Record<string, number>;
    recent: WorkbenchArtifactRecent[];
  };
  alerts: {
    alert_rules: number;
    firing: number;
    events_24h: number;
  };
  demands: {
    open: number;
    /** 最新一单摘要；无单为 null。 */
    latest: { code: string; status: string; updated_at: string } | null;
  };
}

/** 个人工作台聚合（六 section 单往返；错误走 ConsoleApiError 语义）。 */
export function fetchWorkbench(): Promise<WorkbenchSummary> {
  return requestJson(`${API_BASE}/v1/workbench`, {
    method: "GET",
  }) as Promise<WorkbenchSummary>;
}

/** 试运行（POST /artifacts/{id}/alert/test；零副作用——不落事件不改状态）。
 *  错误走既有 ConsoleApiError 语义：422 spec 非法 / 400 查询失败 / 404 非
 *  alert_rule——后端 detail 如实呈现。 */
export async function testAlertRule(
  artifactId: number,
): Promise<AlertTestResult> {
  const body = await requestJsonAnyMethod(
    `${API_BASE}/v1/artifacts/${artifactId}/alert/test`,
    "POST",
  );
  const record = (body ?? {}) as Record<string, unknown>;
  return {
    value: asOptionalNumber(record.value) ?? 0,
    wouldFire: record.would_fire === true,
  };
}

// ── 资产目录与数据集质量（wire-asset-catalog / wire-dataset-quality web 面）──
// 契约（api 仓 assets/quality 路由，Bearer 登录身份，跨客户 404 不泄露）：
// - GET /api/v1/assets?domain=&tag=&sensitivity=&source_kind=&freshness=&q=
//   &page=&page_size= → {items:[{id,name,source_kind,row_count,storage_tier,
//   created_at,asset:{domain,tags,description,cadence_hint,owner_note,
//   sensitivity,cadence_effective}}],total,page}——数据集生而入目录（零手工
//   注册），未补全条目 LEFT JOIN+COALESCE 默认值（敏感度=常规）；
// - GET  /api/v1/datasets/{id}/asset-meta → 六字段全量（未补全=默认值；
//   cadence_effective=生效节奏，api 层数据集自动取自指标注册表，可 null）；
// - PATCH 同路径：body 部分字段；敏感度三值枚举（422 回合法清单）、viewer
//   只读 403（统一依赖 require_console_customer 的 viewer 写拒绝）；每次
//   修改留痕（append-only asset_meta_events）；
// - GET /api/v1/datasets/{id}/asset-meta/events → {items:[{at,actor_sub,
//   before,after}]}（最近在前）；
// - GET /api/v1/quality/summary?dataset_ids=a,b,c → {statuses:{<id>:
//   {freshness:'fresh'|'stale'|'unknown',last_refresh_at,row_count,status}}}
//   （列表徽章用；缺失键=无徽章，端点失败前端静默不渲染徽章）；
// - GET /api/v1/datasets/{id}/quality → {latest:{computed_at,triggered_by,
//   status:'ok'|'failed'|'unsupported',row_count,pk_declared,
//   pk_unique_ratio,null_rates:[{column,null_rate,sampled_rows}],
//   sample_limit_hit,error_text}|null,history:[{computed_at,row_count,
//   null_rates}],freshness,cadence_hint}——呈现如实：失败/不支持/暂无三种
//   诚实态，绝不用旧值冒充当前值；
// - POST /api/v1/datasets/{id}/quality/refresh → 手动触发（快照或如实失败
//   记录，形状同 latest；owner/editor 可触发，viewer 403）。

/** 敏感度三级固定（D3：刻意不含「公开」——避免与公开目录红线撞车）。 */
export const ASSET_SENSITIVITIES = ["常规", "内部", "敏感"] as const;

export type AssetSensitivity = (typeof ASSET_SENSITIVITIES)[number];

/** 期望更新节奏三档（未声明=null——新鲜度不误判，显示「未知」）。 */
export const ASSET_CADENCE_HINTS = ["daily", "weekly", "monthly"] as const;

export type AssetCadenceHint = (typeof ASSET_CADENCE_HINTS)[number];

/** 资产元数据六字段（wire snake_case 直存——与 datasets 段同风格）。 */
export interface DatasetAssetMeta {
  dataset_id: string;
  domain: string | null;
  tags: string[];
  description: string | null;
  cadence_hint: AssetCadenceHint | null;
  owner_note: string | null;
  sensitivity: AssetSensitivity;
  /** 生效节奏（api 层数据集自动取自指标注册表更新频率；取不到 null）。 */
  cadence_effective: AssetCadenceHint | null;
}

/** 元数据部分更新入参（缺键不动；六字段任意子集）。 */
export interface DatasetAssetMetaPatch {
  domain?: string | null;
  tags?: string[];
  description?: string | null;
  cadence_hint?: AssetCadenceHint | null;
  owner_note?: string | null;
  sensitivity?: AssetSensitivity;
}

/** 目录条目（数据集投影 + asset 元数据段）。 */
export interface AssetCatalogItem {
  id: string;
  name: string;
  source_kind: DatasetSourceKind;
  row_count: number;
  /** api（免费档轻量存储）/ cell（引擎物化）；未下发给 null。 */
  storage_tier: string | null;
  created_at: string;
  asset: {
    domain: string | null;
    tags: string[];
    description: string | null;
    cadence_hint: AssetCadenceHint | null;
    owner_note: string | null;
    sensitivity: AssetSensitivity;
    cadence_effective: AssetCadenceHint | null;
  };
}

/** 目录筛选入参（全部可选；组合条件 AND）。 */
export interface AssetCatalogFilters {
  domain?: string;
  tag?: string;
  sensitivity?: AssetSensitivity;
  source_kind?: DatasetSourceKind;
  freshness?: QualityFreshnessValue;
  /** 名称与描述关键字搜索（服务端执行）。 */
  q?: string;
  page?: number;
  page_size?: number;
}

/** 目录分页结果（page 从 1 起）。 */
export interface AssetCatalogPage {
  items: AssetCatalogItem[];
  total: number;
  page: number;
}

/** 资产目录列表（多维筛选 + 搜索 + 分页；Bearer 客户隔离）。 */
export function fetchAssetCatalog(
  filters: AssetCatalogFilters = {},
): Promise<AssetCatalogPage> {
  const params = new URLSearchParams();
  if (filters.domain) params.set("domain", filters.domain);
  if (filters.tag) params.set("tag", filters.tag);
  if (filters.sensitivity) params.set("sensitivity", filters.sensitivity);
  if (filters.source_kind) params.set("source_kind", filters.source_kind);
  if (filters.freshness) params.set("freshness", filters.freshness);
  if (filters.q) params.set("q", filters.q);
  if (filters.page != null) params.set("page", String(filters.page));
  if (filters.page_size != null) {
    params.set("page_size", String(filters.page_size));
  }
  const query = params.size > 0 ? `?${params}` : "";
  return requestJson(`${API_BASE}/v1/assets${query}`, {
    method: "GET",
  }) as Promise<AssetCatalogPage>;
}

/** 单数据集资产元数据（未补全=全默认值：敏感度常规、无标签无域）。 */
export function fetchDatasetAssetMeta(
  datasetId: string,
): Promise<DatasetAssetMeta> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/asset-meta`,
    { method: "GET" },
  ) as Promise<DatasetAssetMeta>;
}

/** 补全/修改资产元数据（六字段任意子集；422 非法敏感度、403 viewer 只读
 *  如实上抛——ConsoleApiError 带 detail）。 */
export function updateDatasetAssetMeta(
  datasetId: string,
  patch: DatasetAssetMetaPatch,
): Promise<DatasetAssetMeta> {
  return requestJsonAnyMethod(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/asset-meta`,
    "PATCH",
    { body: patch },
  ) as Promise<DatasetAssetMeta>;
}

/** 元数据修改留痕条目（append-only；最近在前）。 */
export interface AssetMetaEvent {
  at: string;
  actor_sub: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

/** 最近修改留痕（最近在前）。 */
export function fetchAssetMetaEvents(
  datasetId: string,
): Promise<{ items: AssetMetaEvent[] }> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/asset-meta/events`,
    { method: "GET" },
  ) as Promise<{ items: AssetMetaEvent[] }>;
}

/** 新鲜度三态（wire-dataset-quality：按声明节奏判定；未声明=unknown 不误判）。 */
export type QualityFreshnessValue = "fresh" | "stale" | "unknown";

/** 列表徽章的汇总条目（summary 端点 statuses 值）。 */
export interface QualitySummaryEntry {
  freshness: QualityFreshnessValue;
  last_refresh_at: string | null;
  row_count: number;
  status: string;
}

/** dataset_id → 新鲜度汇总（缺失键=无徽章）。 */
export type QualitySummaryMap = Record<string, QualitySummaryEntry>;

/** 批量新鲜度汇总（列表徽章用；ids 逗号拼接）。 */
export function fetchQualitySummary(
  datasetIds: string[],
): Promise<{ statuses: QualitySummaryMap }> {
  const ids = datasetIds.filter(Boolean).join(",");
  const query = ids ? `?dataset_ids=${encodeURIComponent(ids)}` : "";
  return requestJson(`${API_BASE}/v1/quality/summary${query}`, {
    method: "GET",
  }) as Promise<{ statuses: QualitySummaryMap }>;
}

/** 单列样本空值率（null_rate 为 0..1 比率；sampled_rows=抽样行数）。 */
export interface QualityNullRate {
  column: string;
  null_rate: number;
  sampled_rows: number;
}

/** 质量快照（ok 携带五项指标；failed/unsupported 如实不带值冒充）。 */
export interface QualitySnapshot {
  computed_at: string;
  triggered_by: string;
  status: "ok" | "failed" | "unsupported";
  row_count: number;
  pk_declared: boolean;
  /** 0..1 唯一比例；未声明键列为 null（呈现「未声明键列」）。 */
  pk_unique_ratio: number | null;
  null_rates: QualityNullRate[];
  sample_limit_hit: boolean;
  error_text: string | null;
}

/** 历史条目（趋势用最小投影）。 */
export interface QualityHistoryEntry {
  computed_at: string;
  row_count: number;
  null_rates: QualityNullRate[];
}

/** 详情质量响应（freshness 容错解析：对象 {freshness,last_refresh_at} 或
 *  裸枚举字符串——并行后端两种下发形态都收）。 */
export interface DatasetQuality {
  latest: QualitySnapshot | null;
  history: QualityHistoryEntry[];
  freshness: { freshness: QualityFreshnessValue; last_refresh_at: string | null };
  cadence_hint: AssetCadenceHint | null;
}

/** 详情质量区数据（latest=null 即「暂无快照」诚实态）。 */
export function fetchDatasetQuality(
  datasetId: string,
): Promise<DatasetQuality> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/quality`,
    { method: "GET" },
  ) as Promise<DatasetQuality>;
}

/** 手动触发质量快照（返回新快照或如实失败记录，形状同 latest）。 */
export function refreshDatasetQuality(
  datasetId: string,
): Promise<QualitySnapshot> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/quality/refresh`,
    { method: "POST" },
  ) as Promise<QualitySnapshot>;
}

// ── 变换管线（wire-transform-pipelines 任务 5.5 web 客户端）────────────────
// 契约（api 仓管线路由，Bearer 登录身份，写操作 owner/editor、viewer 403）：
// - GET  /api/v1/pipelines → {items:[{id,name,description,current_version,
//   steps_count,base_dataset_id,base_dataset_name,schedule:{cron,enabled,
//   last_run_at,last_status}|null,last_run:{id,status,version,trigger,
//   started_at,finished_at}|null,updated_at}],total}；
// - POST /api/v1/pipelines {name,description?,base_dataset_id,steps:[{name,
//   op,params}]} → 管线详情（versions 仅 v1）；
// - PUT  /api/v1/pipelines/{id} {name?,description?,base_dataset_id?,steps?}
//   → 详情 + version_created?: number（步骤列表或基础数据集变更时按序追加
//   新版本并回号；仅名称/描述就地更新不动版本）；
// - GET  /api/v1/pipelines/{id} → {…,versions:[{version,steps:[{name,op,
//   params}],created_at,created_by}]}（append-only，历史版本原样可查）；
// - POST /api/v1/pipelines/{id}/run {version?}（缺省当前版本）→
//   {id,status:'success'|'failed',version,trigger,started_at,finished_at,
//   error_text,steps:[{seq,name,op,status:'success'|'failed'|'skipped'|
//   'quota_exhausted',output_dataset_id,output_dataset_name,row_count,
//   duration_ms,error_text}]}——失败中止后续（后续步骤无记录，绝不伪造）；
// - GET  /api/v1/pipelines/{id}/runs?page=&page_size= → {items:[{id,status,
//   version,trigger,started_at,finished_at,error_text}],total}（时间倒序）；
// - GET  /api/v1/pipelines/{id}/runs/{run_id} → 同 run 结果形状；
// - PUT/GET/DELETE /api/v1/pipelines/{id}/schedule → {cron,enabled,
//   last_run_at,last_status}（五字段 cron，naive UTC；删除幂等）；
//   POST …/schedule/pause / …/schedule/resume → 同形状。
// 步骤参数形状与既有原子操作端点一致（derive {filters?,sort?,limit?}；
// join {indicators,date_column,region_column?,default_entity?,filters?}；
// link {right_dataset_id,left_keys:[],right_keys:[]}）。

/** 步骤类型三值（derive=派生 / join=挂接指标 / link=关联数据集）。 */
export const PIPELINE_STEP_OPS = ["derive", "join", "link"] as const;

export type PipelineStepOp = (typeof PIPELINE_STEP_OPS)[number];

/** 步骤定义（版本体内逐条；params 与原子操作端点同形）。 */
export interface PipelineStepDef {
  name: string;
  op: PipelineStepOp;
  params: Record<string, unknown>;
}

/** 历史版本（append-only：步骤定义完整保存，历史不可改）。 */
export interface PipelineVersion {
  version: number;
  steps: PipelineStepDef[];
  created_at: string;
  created_by: string;
}

/** 管线级调度登记态（未挂接为 null）。 */
export interface PipelineScheduleInfo {
  /** 五字段 cron（naive UTC）。 */
  cron: string;
  enabled: boolean;
  last_run_at: string | null;
  /** 最近一次调度运行状态（success/failed，原样呈现）。 */
  last_status: string | null;
}

/** 运行摘要（列表项 / 历史条目；steps 在详情展开）。 */
export interface PipelineRunSummary {
  id: number;
  status: "success" | "failed";
  version: number;
  /** 触发方式：manual（手动）/ schedule（调度）。 */
  trigger: string;
  started_at: string;
  finished_at: string | null;
  error_text: string | null;
}

/** 逐步骤执行记录（两级留痕的第二级；失败后续步骤无记录）。 */
export interface PipelineStepRun {
  seq: number;
  name: string;
  op: PipelineStepOp;
  status: "success" | "failed" | "skipped" | "quota_exhausted";
  output_dataset_id: string | null;
  output_dataset_name: string | null;
  row_count: number | null;
  duration_ms: number | null;
  error_text: string | null;
}

/** 运行详情（摘要 + 逐步骤记录）。 */
export interface PipelineRunDetail extends PipelineRunSummary {
  steps: PipelineStepRun[];
}

/** 管线列表项（列表页一屏）。 */
export interface PipelineListItem {
  id: number;
  name: string;
  description: string | null;
  current_version: number;
  steps_count: number;
  base_dataset_id: string;
  base_dataset_name: string;
  schedule: PipelineScheduleInfo | null;
  /** 最近一次运行（任意触发方式；从未运行为 null）。 */
  last_run: PipelineRunSummary | null;
  updated_at: string;
}

/** 管线列表分页结果。 */
export interface PipelinePage {
  items: PipelineListItem[];
  total: number;
}

/** 管线详情（含全部历史版本）。 */
export interface PipelineDetail {
  id: number;
  name: string;
  description: string | null;
  base_dataset_id: string;
  base_dataset_name: string;
  current_version: number;
  updated_at: string;
  versions: PipelineVersion[];
}

/** 保存结果：详情 + version_created（仅步骤/基础集变更产生新版本时出现）。 */
export interface PipelineUpdateResult extends PipelineDetail {
  version_created?: number;
}

/** 创建入参（v1 步骤链线性：首步吃基础数据集）。 */
export interface PipelineCreateInput {
  name: string;
  description?: string;
  base_dataset_id: string;
  steps: PipelineStepDef[];
}

/** 部分更新入参（缺键不动；steps/base_dataset_id 变更 → 新版本）。 */
export interface PipelineUpdateInput {
  name?: string;
  description?: string | null;
  base_dataset_id?: string;
  steps?: PipelineStepDef[];
}

/** 本企业管线列表。 */
export function fetchPipelines(): Promise<PipelinePage> {
  return requestJson(`${API_BASE}/v1/pipelines`, {
    method: "GET",
  }) as Promise<PipelinePage>;
}

/** 新建管线（首版 v1 由 steps 直接产生）。 */
export function createPipeline(
  payload: PipelineCreateInput,
): Promise<PipelineDetail> {
  return requestJson(`${API_BASE}/v1/pipelines`, {
    method: "POST",
    body: payload,
  }) as Promise<PipelineDetail>;
}

/** 保存修改（仅名称/描述就地更新；步骤或基础集变更追加新版本）。 */
export function updatePipeline(
  id: number,
  payload: PipelineUpdateInput,
): Promise<PipelineUpdateResult> {
  return requestJsonAnyMethod(`${API_BASE}/v1/pipelines/${id}`, "PUT", {
    body: payload,
  }) as Promise<PipelineUpdateResult>;
}

/** 管线详情（版本 append-only，历史原样可查）。 */
export function fetchPipeline(id: number): Promise<PipelineDetail> {
  return requestJson(`${API_BASE}/v1/pipelines/${id}`, {
    method: "GET",
  }) as Promise<PipelineDetail>;
}

/** 立即运行（缺省当前版本；返回 run + 逐步骤记录）。 */
export function runPipeline(
  id: number,
  version?: number,
): Promise<PipelineRunDetail> {
  return requestJson(`${API_BASE}/v1/pipelines/${id}/run`, {
    method: "POST",
    body: version != null ? { version } : {},
  }) as Promise<PipelineRunDetail>;
}

/** 运行历史（时间倒序分页）。 */
export function fetchPipelineRuns(
  id: number,
  page = 1,
  pageSize = 10,
): Promise<{ items: PipelineRunSummary[]; total: number }> {
  return requestJson(
    `${API_BASE}/v1/pipelines/${id}/runs?page=${page}&page_size=${pageSize}`,
    { method: "GET" },
  ) as Promise<{ items: PipelineRunSummary[]; total: number }>;
}

/** 单次运行详情（含逐步骤记录）。 */
export function fetchPipelineRun(
  id: number,
  runId: number,
): Promise<PipelineRunDetail> {
  return requestJson(`${API_BASE}/v1/pipelines/${id}/runs/${runId}`, {
    method: "GET",
  }) as Promise<PipelineRunDetail>;
}

/** 查看管线调度登记态（未挂接：cron 字段缺省 null 视为未登记）。 */
export function getPipelineSchedule(
  id: number,
): Promise<PipelineScheduleInfo | { cron: null; enabled: false }> {
  return requestJson(`${API_BASE}/v1/pipelines/${id}/schedule`, {
    method: "GET",
  }) as Promise<PipelineScheduleInfo | { cron: null; enabled: false }>;
}

/** 挂接/更新管线调度（五字段 cron，naive UTC）。 */
export function putPipelineSchedule(
  id: number,
  cron: string,
): Promise<PipelineScheduleInfo> {
  return requestJsonAnyMethod(
    `${API_BASE}/v1/pipelines/${id}/schedule`,
    "PUT",
    { body: { cron } },
  ) as Promise<PipelineScheduleInfo>;
}

/** 删除管线调度（删除后回到未挂接态）。 */
export function deletePipelineSchedule(
  id: number,
): Promise<{ removed: boolean }> {
  return requestJson(`${API_BASE}/v1/pipelines/${id}/schedule`, {
    method: "DELETE",
  }) as Promise<{ removed: boolean }>;
}

/** 暂停调度（到点不触发；手动运行不受影响）。 */
export function pausePipelineSchedule(
  id: number,
): Promise<PipelineScheduleInfo> {
  return requestJson(`${API_BASE}/v1/pipelines/${id}/schedule/pause`, {
    method: "POST",
  }) as Promise<PipelineScheduleInfo>;
}

/** 恢复调度（继续到点自动运行）。 */
export function resumePipelineSchedule(
  id: number,
): Promise<PipelineScheduleInfo> {
  return requestJson(`${API_BASE}/v1/pipelines/${id}/schedule/resume`, {
    method: "POST",
  }) as Promise<PipelineScheduleInfo>;
}

// ── 数据开放与市场（wire-dataset-marketplace web 面）────────────────────
// 契约（api 仓 assets/market 切片，proposal 冻结形状；Bearer 登录身份，
// owner/editor 可设可见性、viewer 403）：
// - PUT /api/v1/datasets/{id}/visibility body {visibility, access_mode?,
//   grants?} → {visibility, access_mode, published_at, published_version}
//   ——四档任选主端点；敏感度=敏感 时设 market/directed 由服务端 422 拒收；
// - POST .../market/publish|unpublish ——薄封装（publish=market、
//   unpublish=private），一键到市场/回私有；
// - GET/POST/DELETE /api/v1/datasets/{id}/grants ——定向授权增删查；
// - GET /api/v1/market/datasets?q=&domain=&tag=&limit=&offset= →
//   {items:[卡片],total}——SQL 层只查 visibility='market'（默认私有，
//   未开放永不出现）；卡片=元数据+体量/时间，绝无数据值；
// - GET /api/v1/market/datasets/{id} → 卡片 + columns（列 schema {name,
//   type}）+ column_count——详情面同样零数据值；
// - POST/DELETE /api/v1/market/datasets/{id}/subscribe ——open 模式订阅
//   即得；approval 模式 POST 由服务端转为发起申请（409 已在途）；
// - POST /api/v1/market/datasets/{id}/access-requests ——显式发起申请；
//   GET /api/v1/datasets/{id}/access-requests ——发布方查看（pending 在前）；
//   POST .../access-requests/{rid}/approve|reject ——发布方裁决。

/** 可见性四档阶梯（private 默认 → link → directed → market）。 */
export const DATASET_VISIBILITIES = [
  "private",
  "link",
  "directed",
  "market",
] as const;

export type DatasetVisibility = (typeof DATASET_VISIBILITIES)[number];

/** 取数方式（仅 market/directed 档有意义）：订阅即得 / 需发布方批准。 */
export const DATASET_ACCESS_MODES = ["open", "approval"] as const;

export type DatasetAccessMode = (typeof DATASET_ACCESS_MODES)[number];

/** 可见性设置响应（published_at/published_version 仅开放后非 null）。 */
export interface DatasetVisibilityState {
  visibility: DatasetVisibility;
  access_mode: DatasetAccessMode;
  published_at: string | null;
  published_version: string | null;
}

/** 可见性设置入参（grants 仅 directed 档用：customer_id 数组全量替换）。 */
export interface DatasetVisibilityInput {
  visibility: DatasetVisibility;
  access_mode?: DatasetAccessMode;
  /** directed 档的被授权客户名单（全量替换；其他档忽略）。 */
  grants?: string[];
}

/** 当前可见性态（GET /visibility——四档 + 开放模式 + 发布锚）。 */
export function fetchDatasetVisibility(
  datasetId: string,
): Promise<DatasetVisibilityState> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/visibility`,
    { method: "GET" },
  ) as Promise<DatasetVisibilityState>;
}

/** 设置数据集可见性（四档任选；幂等——同值重设不重复留痕）。 */
export function updateDatasetVisibility(
  datasetId: string,
  payload: DatasetVisibilityInput,
): Promise<DatasetVisibilityState> {
  return requestJsonAnyMethod(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/visibility`,
    "PUT",
    { body: payload },
  ) as Promise<DatasetVisibilityState>;
}

/** 一键发布到市场（=PUT visibility market；敏感档服务端 422 拒收）。 */
export function publishDatasetToMarket(
  datasetId: string,
): Promise<DatasetVisibilityState> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/market/publish`,
    { method: "POST" },
  ) as Promise<DatasetVisibilityState>;
}

/** 撤回开放：回私有（publish 的唯一补救动作，随时可做）。 */
export function unpublishDatasetFromMarket(
  datasetId: string,
): Promise<DatasetVisibilityState> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/market/unpublish`,
    { method: "POST" },
  ) as Promise<DatasetVisibilityState>;
}

/** 定向授权行（directed 档被授权客户）。 */
export interface DatasetGrant {
  customer_id: string;
  granted_at: string;
}

/** 定向授权名单（directed 档；无行 = 无人可见——诚实处理）。 */
export function fetchDatasetGrants(
  datasetId: string,
): Promise<DatasetGrant[]> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/grants`,
    { method: "GET" },
  ) as Promise<DatasetGrant[]>;
}

/** 增定向授权（幂等；重复授权不落第二行）。 */
export function addDatasetGrant(
  datasetId: string,
  customerId: string,
): Promise<{ granted: boolean }> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/grants`,
    { method: "POST", body: { customer_id: customerId } },
  ) as Promise<{ granted: boolean }>;
}

/** 删定向授权（幂等）。 */
export function removeDatasetGrant(
  datasetId: string,
  customerId: string,
): Promise<{ deleted: boolean }> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/grants/${encodeURIComponent(customerId)}`,
    { method: "DELETE" },
  ) as Promise<{ deleted: boolean }>;
}

/** 市场卡片（列表项）：元数据 + 体量/时间；绝无列 schema 或数据值。 */
export interface MarketDatasetCard {
  id: string;
  name: string;
  source_kind: DatasetSourceKind;
  storage_tier: string | null;
  row_count: number;
  updated_at: string | null;
  description: string;
  domain: string;
  tags: string[];
  cadence_hint: AssetCadenceHint | null;
  owner_note: string;
  sensitivity: AssetSensitivity;
  visibility: DatasetVisibility;
  access_mode: DatasetAccessMode;
  published_at: string | null;
  published_version: string | null;
  /** 当前查看者是否已订阅（发布方视角恒 false）。 */
  subscribed: boolean;
  /** 访问来源标注：market_subscription / directed_grant / access_request / own。 */
  access_source: string | null;
}

/** 市场详情：卡片 + 列 schema（{name,type} 白名单）——零数据值。 */
export interface MarketDatasetDetail extends MarketDatasetCard {
  columns: DatasetColumn[];
  column_count: number;
}

/** 市场列表筛选入参（全部可选；组合 AND）。 */
export interface MarketDatasetFilters {
  q?: string;
  domain?: string;
  tag?: string;
  limit?: number;
  offset?: number;
}

/** 市场列表分页结果。 */
export interface MarketDatasetPage {
  items: MarketDatasetCard[];
  total: number;
}

/** 市场浏览列表（仅登录客户面；未开放数据永不出现）。 */
export function fetchMarketDatasets(
  filters: MarketDatasetFilters = {},
): Promise<MarketDatasetPage> {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.domain) params.set("domain", filters.domain);
  if (filters.tag) params.set("tag", filters.tag);
  if (filters.limit != null) params.set("limit", String(filters.limit));
  if (filters.offset != null) params.set("offset", String(filters.offset));
  const query = params.size > 0 ? `?${params}` : "";
  return requestJson(`${API_BASE}/v1/market/datasets${query}`, {
    method: "GET",
  }) as Promise<MarketDatasetPage>;
}

/** 市场详情（元数据 + 列 schema + 行数；不含任何数据值）。 */
export function fetchMarketDataset(
  datasetId: string,
): Promise<MarketDatasetDetail> {
  return requestJson(
    `${API_BASE}/v1/market/datasets/${encodeURIComponent(datasetId)}`,
    { method: "GET" },
  ) as Promise<MarketDatasetDetail>;
}

/** 订阅响应（幂等：重复操作仍 200 返回终态）。 */
export interface MarketSubscriptionResult {
  dataset_id: string;
  subscribed: boolean;
}

/** 订阅市场数据集（open 模式订阅即得；approval 模式服务端转申请）。 */
export function subscribeMarketDataset(
  datasetId: string,
): Promise<MarketSubscriptionResult> {
  return requestJson(
    `${API_BASE}/v1/market/datasets/${encodeURIComponent(datasetId)}/subscribe`,
    { method: "POST" },
  ) as Promise<MarketSubscriptionResult>;
}

/** 退订市场数据集（幂等；退订后数据面访问权下一次查询即失效）。 */
export function unsubscribeMarketDataset(
  datasetId: string,
): Promise<MarketSubscriptionResult> {
  return requestJson(
    `${API_BASE}/v1/market/datasets/${encodeURIComponent(datasetId)}/unsubscribe`,
    { method: "DELETE" },
  ) as Promise<MarketSubscriptionResult>;
}

/** 准入申请状态（pending → approved|rejected）。 */
export type AccessRequestStatus = "pending" | "approved" | "rejected";

/** 一条准入申请（发布方视角与申请人视角同形）。 */
export interface DatasetAccessRequest {
  id: number;
  dataset_id: string;
  requester_customer_id: string;
  status: AccessRequestStatus;
  created_at: string;
  decided_at: string | null;
}

/** 显式发起准入申请（approval 模式；同数据集重复申请复用原行）。 */
export function requestMarketAccess(
  datasetId: string,
): Promise<DatasetAccessRequest> {
  return requestJson(
    `${API_BASE}/v1/market/datasets/${encodeURIComponent(datasetId)}/access-requests`,
    { method: "POST" },
  ) as Promise<DatasetAccessRequest>;
}

/** 发布方查看本数据集的准入申请（pending 在前，其余按时间倒序）。 */
export function fetchDatasetAccessRequests(
  datasetId: string,
): Promise<DatasetAccessRequest[]> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/access-requests`,
    { method: "GET" },
  ) as Promise<DatasetAccessRequest[]>;
}

/** 批准入入申请（批准后申请方获得数据面访问权）。 */
export function approveDatasetAccessRequest(
  datasetId: string,
  requestId: number,
): Promise<DatasetAccessRequest> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/access-requests/${requestId}/approve`,
    { method: "POST" },
  ) as Promise<DatasetAccessRequest>;
}

/** 拒绝准入申请。 */
export function rejectDatasetAccessRequest(
  datasetId: string,
  requestId: number,
): Promise<DatasetAccessRequest> {
  return requestJson(
    `${API_BASE}/v1/datasets/${encodeURIComponent(datasetId)}/access-requests/${requestId}/reject`,
    { method: "POST" },
  ) as Promise<DatasetAccessRequest>;
}
