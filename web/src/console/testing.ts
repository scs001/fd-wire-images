// 测试专用：控制台 API 的内存假后端（语义对齐 api 仓 fd_wire_api 路由：
// Authorization: Bearer <ID token> 身份（解码 sub 即客户标识）、创建即跑
// 一轮循环、消息 append-only、产物草稿列表；wire-customer-local-data 任务组 3
// 追加 数据集/上传/修正/同步/连接器 路由；wire-self-serve-launch 追加
// /api/console/{auth/config,session,usage} 路由——纯内存、零网络）。
import type {
  ApiDetailItem,
  ArtifactStatus,
  ChatMessage,
  Connector,
  ConnectorManifest,
  Conversation,
  ConversationDetail,
  Correction,
  CorrectionInput,
  CorrectionOp,
  CustomerDemand,
  Dataset,
  DatasetColumn,
  DatasetQuota,
  DraftArtifact,
  MessageRole,
  PackIndicator,
  PipelineGraph,
  PipelineRunDetail,
  PipelineRunSummary,
  PipelineScheduleInfo,
  PipelineStepDef,
  PipelineStepRun,
  RefreshSchedule,
  SyncRecord,
  UploadPreview,
  WorkbenchSummary,
} from "./api";
import { saveSession, type ConsoleSession } from "./auth";

export type FakeResponse = {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
};

export type FetchCall = {
  method: string;
  url: URL;
  headers: Record<string, string>;
  body?: unknown;
  /** 非 JSON 请求体（multipart 上传的 FormData）原样保留，便于断言文件字段。 */
  formData?: FormData;
};

/** 数据集 + 可选存储层字段（wire-free-tier-data-gate：api 侧免费档响应带
 *  storage_tier，组件容错读取；Dataset 接口本体保持不动避免与并行改动冲突）。 */
type DatasetWithTier = Dataset & { storage_tier?: string };

interface StoredConversation {
  meta: Conversation;
  messages: ChatMessage[];
  artifacts: DraftArtifact[];
}

/** 生命周期产物种子（wire-artifacts-persistence；versions 不给时按 spec 造 v1）。 */
export type LifecycleArtifactSeed = {
  id: number;
  kind: string;
  title: string;
  status?: ArtifactStatus;
  version?: number;
  origin_conversation_id?: number | null;
  customer_id?: string;
  spec?: Record<string, unknown>;
  versions?: Array<{
    spec: Record<string, unknown>;
    operator?: string | null;
    created_at?: string;
  }>;
  created_at?: string;
  updated_at?: string;
};

function jsonResponse(body: unknown, status = 200): FakeResponse {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

function headersOf(init?: RequestInit): Record<string, string> {
  if (!init?.headers) return {};
  const record: Record<string, string> = {};
  new Headers(init.headers as HeadersInit).forEach((v, k) => {
    record[k.toLowerCase()] = v;
  });
  return record;
}

export interface ConsoleBackendOptions {
  /** 创建对话（带首条消息）时，除 user 外追加的回复消息序列。 */
  createReply?: Array<{ role: MessageRole; content: string }>;
  /** 续聊 POST 的回复消息序列（默认一条 assistant 回声）。 */
  chatReply?: Array<{ role: MessageRole; content: string }>;
  /** 强制 POST /ai/conversations 返回该状态（如 429 每日限额）。 */
  failCreateStatus?: number;
  failCreateDetail?: string;
  /** /api/console/auth/config 下发体（缺省 configured=true 的可用配置；false=阻塞态）。 */
  authConfig?: { configured: boolean };
  /** 强制 auth/config 返回该状态（配置端点故障态）。 */
  failAuthConfigStatus?: number;
  /** 会话建立返回的客户 plan_code（免费档门禁测试用，默认 basic）。 */
  planCode?: string;
  /** 会话建立返回的客户名（默认 演示客户）。 */
  customerName?: string;
  /** 会话建立下发的明文密钥（仅开户那次；缺省 null）。 */
  keyOnce?: string | null;
  /** 强制 POST /api/console/session 返回 502 failed（开户链路断言）。 */
  sessionFailReason?: string;
  /** GET /api/console/usage 响应体覆盖（用量页测试）。 */
  usage?: {
    period: string;
    plan_code: string;
    included_lines: number;
    used_lines: number;
    remaining_lines: number;
  };
  /** 强制 /api/console/usage 返回该状态。 */
  failUsageStatus?: number;
  /** 种子数据集（带 customer_id 的仅对同客户可见）。 */
  datasets?: Dataset[];
  /** 种子连接器（带 customer_id 的仅对同客户可见）。 */
  connectors?: Connector[];
  /** 数据集预览行：dataset id → {columns, rows}；缺 __row_id 的行自动补 `hash:seed-<n>`。 */
  datasetRows?: Record<
    string,
    { columns: DatasetColumn[]; rows: Array<Record<string, unknown>> }
  >;
  /** 上传响应注入（默认 2 列 / 120 行 / within_limit=true）。 */
  uploadPreview?: Partial<UploadPreview>;
  /** 强制上传失败（如 400 行数超限）。 */
  failUploadStatus?: number;
  failUploadDetail?: string;
  /** GET /api/v1/datasets/quota 响应体覆盖（免费档配额条测试；缺省按档位与
   *  当前数据集数推导：free=3 集 × 10,000 行）。 */
  datasetQuota?: DatasetQuota;
  /** 强制 /api/v1/datasets/quota 返回该状态（配额端点未就绪态）。 */
  failQuotaStatus?: number;
  /** 强制 POST /api/v1/datasets 返回该状态（如免费档建集触限 400）。 */
  failCreateDatasetStatus?: number;
  failCreateDatasetDetail?: string;
  /** 配对令牌有效期秒数（默认 900 = 15 分钟）。 */
  pairingTokenTtlSeconds?: number;
  /** 种子需求单（带 customer_id 的仅对同客户可见；wire-ux-consolidation 4.3）。 */
  demands?: CustomerDemand[];
  /** 强制 POST /api/v1/demands 返回该状态（如 422 校验回显）；detail 可为
   *  字符串或 FastAPI 校验错误数组形状（wire-demand-generation-orchestration
   *  冻结契约注入）。 */
  failDemandCreateStatus?: number;
  failDemandCreateDetail?: string | ApiDetailItem[];
  /** 连接器下载清单（wire-connector-distribution 3.1）：GET
   *  /api/v1/downloads/connector/manifest.json 的响应体注入（缺省 404——
   *  端点未配置态；含 available=false 条目即「部分 unavailable」态）。 */
  connectorManifest?: ConnectorManifest;
  /** 强制 manifest 端点返回该状态（不可达/网络错态）。 */
  failManifestStatus?: number;
  /** 种子源包（wire-source-packs 3.3）：indicator_count 按 indicators 长度推导。 */
  packs?: FakePackSeed[];
  /** 预置订阅包 id（缺省归属 demo-customer；订阅面 Bearer 身份隔离）。 */
  packSubscriptions?: string[];
  /** 强制源包公开端点（列表/详情）返回该状态。 */
  failPacksStatus?: number;
  /** 强制订阅/退订端点返回该状态（如 401 会话失效态）。 */
  failPackSubscribeStatus?: number;
  /** 种子实体（wire-entity-layer 3.x）：datasetId 引用种子数据集；pkColumns
   *  校验只在创建端点做（种子信任）。 */
  entities?: FakeEntitySeed[];
  /** 种子实体关系（fromEntityId/toEntityId 引用种子实体 id）。 */
  entityRelationships?: FakeEntityRelationshipSeed[];
  /** 强制 GET /api/v1/entities 与 /api/v1/entities/graph 返回该状态。 */
  failEntitiesStatus?: number;
  /** 强制 POST /api/v1/entities 返回该状态（如 422 逐条回显注入）。 */
  failEntityCreateStatus?: number;
  failEntityCreateDetail?: string | ApiDetailItem[];
  /** 强制 POST /api/v1/entities/{id}/relationships 返回该状态。 */
  failEntityRelationshipCreateStatus?: number;
  failEntityRelationshipCreateDetail?: string | ApiDetailItem[];
  /** 强制 POST /api/v1/entities/{id}/relationships/{rel_id}/materialize
   *  返回该状态（wire-entity-link：403 档位闸 / 422 列漂移逐条 / 502 旧
   *  cell 无 link 工具等注入）。 */
  failEntityMaterializeStatus?: number;
  failEntityMaterializeDetail?: string | ApiDetailItem[];
  /** 种子域动作类型（wire-entity-write-surface）：实体级客户声明类型；
   *  内置两行由假后端按 api 语义合成注入（builtin=true、负 id、entity_id/
   *  dataset_id/时间戳 null）——客户面只读。 */
  domainActions?: FakeDomainActionSeed[];
  /** 种子域动作实例（type_id 可指向内置负 id；result_ref 即声明结果物 id）。 */
  domainActionInstances?: FakeDomainActionInstanceSeed[];
  /** 强制域动作读面（类型/台账三端点）返回该状态。 */
  failDomainActionsStatus?: number;
  /** 种子定时刷新调度（wire-refresh-scheduler 4.1；带 customer_id 的仅同客户可见）。 */
  refreshSchedules?: Array<RefreshSchedule & { customer_id?: string }>;
  /** 强制 POST /api/v1/refresh-schedules 返回该状态（创建失败态）。 */
  failScheduleCreateStatus?: number;
  failScheduleCreateDetail?: string;
  /** 管线图响应体覆盖（wire-transform-pipeline-view 2.2；不传则按种子数据集
   *  组装：derived 的 source_ref 解析父 key 成边、BFS 深度、环/孤儿计 anomalies）。 */
  pipelineGraph?: PipelineGraph;
  /** 强制 GET /api/v1/pipeline/graph 返回该状态（错误态测试）。 */
  failPipelineGraphStatus?: number;
  failPipelineGraphDetail?: string;
  /** 强制 POST /api/v1/artifacts（空白建板，wire-dashboard-blank-create）
   *  返回该状态（如 422 校验回显）。 */
  failArtifactCreateStatus?: number;
  failArtifactCreateDetail?: string;
  /** 强制 PATCH /api/v1/datasets/{id}/sync-settings 返回该状态
   *  （wire-incremental-sync：422 校验回显等失败态）。 */
  failSyncSettingsStatus?: number;
  failSyncSettingsDetail?: string;
  /** 种子团队成员（wire-team-spaces 任务 5）：bearer sub 命中种子行即取该
   *  角色（GET me 段）；未命中回落 owner（现状单 owner 语义）。 */
  teamMembers?: FakeTeamMemberSeed[];
  /** 种子待用邀请（owner 视角列表）。 */
  teamInvites?: FakeTeamInviteSeed[];
  /** 强制 GET /api/console/team 返回该状态（403 无权态 / 故障态注入）。 */
  failTeamStatus?: number;
  failTeamDetail?: string;
  /** 种子「我的团队」清单（wire-team-spaces-v2）：无种子 = 单队身份
   *  （teams 长度 1——切换面/顶栏徽标零渲染）。 */
  teams?: FakeTeamSeed[];
  /** 活跃队 customer_id（缺省首个种子；POST switch 原地更新）。 */
  activeTeamId?: number;
  /** 席位用量覆盖（wire-team-spaces-v2）：used 缺省 = 名单行数、limit
   *  缺省 8；POST invites 在 used ≥ limit 时 409（detail 含档位/席位/
   *  升级指引——前端原样呈现）。 */
  teamSeats?: { used?: number; limit?: number };
  /** 强制 GET /api/console/teams 返回该状态（顶栏徽标静默降级测试）。 */
  failTeamsStatus?: number;
  /** 种子告警评估状态（wire-alert-evaluation 任务 5）：键 = artifact_id
   *  字符串化，仅对同客户可见的 alert_rule 产物生效。 */
  alertStates?: Record<string, FakeAlertStateSeed>;
  /** 种子告警事件（wire snake_case 形状；artifact_id 引用种子产物）。 */
  alertEvents?: FakeAlertEventSeed[];
  /** 试运行返回值（POST /artifacts/{id}/alert/test；缺省 {value:1,
   *  would_fire:false}——试运行零副作用，不写 states/events）。 */
  alertTest?: { value: number; would_fire: boolean };
  /** 强制 GET /api/v1/alerts/states 返回该状态（徽标降级态测试）。 */
  failAlertStatesStatus?: number;
  /** 强制 GET /api/v1/alerts/events 返回该状态（事件列表失败态）。 */
  failAlertEventsStatus?: number;
  /** 强制 POST /api/v1/artifacts/{id}/alert/test 返回该状态（422 spec 非法 /
   *  400 查询失败——detail 如实上抛）。 */
  failAlertTestStatus?: number;
  failAlertTestDetail?: string;
  /** 种子个人偏好（wire-personal-preferences）：键 = Bearer sub（per-sub
   *  一人一行）；未命中 sub 的 GET 回三键全 null。 */
  preferences?: Record<string, FakePreferencesSeed>;
  /** 强制 GET/PUT /api/console/me/preferences 返回该状态（偏好端点故障态：
   *  控制台启动不阻塞、置顶操作行内报错）。 */
  failPreferencesStatus?: number;
  failPreferencesDetail?: string;
  /** 工作台聚合响应体覆盖（wire-personal-workbench 任务 2.4）：缺省空聚合
   *  （各段零值、free_quota=null、demands.latest=null——「无数据」如实呈现）。 */
  workbench?: WorkbenchSummary;
  /** 强制 GET /api/v1/workbench 返回该状态（工作台错误态测试）。 */
  failWorkbenchStatus?: number;
  /** 种子资产元数据（wire-asset-catalog 5.x；键 = dataset id，未种子条目
   *  LEFT JOIN+COALESCE 默认值：敏感度常规、无域无标签）。 */
  assetMeta?: Record<string, FakeAssetMetaSeed>;
  /** 种子资产元数据留痕（键 = dataset id；最近在前——PATCH 会续记在最前）。 */
  assetMetaEvents?: Record<string, FakeAssetMetaEventSeed[]>;
  /** 强制 GET /api/v1/assets 返回该状态（目录端点故障态）。 */
  failAssetsStatus?: number;
  /** 强制 GET /api/v1/datasets/{id}/asset-meta 返回该状态。 */
  failAssetMetaStatus?: number;
  /** 强制 PATCH /api/v1/datasets/{id}/asset-meta 返回该状态（422 非法敏感度
   *  / 403 viewer 只读等注入；detail 可为字符串或 FastAPI 校验数组形状）。 */
  failAssetMetaPatchStatus?: number;
  failAssetMetaPatchDetail?: string | ApiDetailItem[];
  /** 种子数据集质量（wire-dataset-quality；键 = dataset id，缺省=暂无快照
   *  诚实空态 latest=null）。 */
  quality?: Record<string, FakeQualitySeed>;
  /** 强制 GET /api/v1/quality/summary 返回该状态（徽章静默降级测试）。 */
  failQualitySummaryStatus?: number;
  /** 强制 GET /api/v1/datasets/{id}/quality 返回该状态。 */
  failQualityStatus?: number;
  /** 强制 POST /api/v1/datasets/{id}/quality/refresh 返回该状态。 */
  failQualityRefreshStatus?: number;
  failQualityRefreshDetail?: string;
  /** 种子管线（wire-transform-pipelines 5.x）：versions 按序即 v1..vN；
   *  runs 为预置运行历史；failAt 注入运行失败点（后续步骤不产生记录）。 */
  pipelines?: FakePipelineSeed[];
  /** 强制 GET /api/v1/pipelines 返回该状态（列表故障态）。 */
  failPipelinesStatus?: number;
  /** 强制 GET /api/v1/pipelines/{id} 返回该状态（详情故障态）。 */
  failPipelineDetailStatus?: number;
  /** 强制 POST/PUT /api/v1/pipelines 返回该状态（保存失败态，如 422）。 */
  failPipelineSaveStatus?: number;
  failPipelineSaveDetail?: string;
  /** 强制 POST /api/v1/pipelines/{id}/run 返回该状态。 */
  failPipelineRunStatus?: number;
  failPipelineRunDetail?: string;
  /** 强制 GET /api/v1/pipelines/{id}/runs 返回该状态（历史故障态）。 */
  failPipelineHistoryStatus?: number;
  /** 强制调度端点（PUT/DELETE/pause/resume）返回该状态。 */
  failPipelineScheduleStatus?: number;
  /** run 端点响应延迟毫秒数（in-flight 禁用态观察用；缺省立即返回）。 */
  pipelineRunDelayMs?: number;
  /** 数据集可见性种子（wire-dataset-marketplace；键 = dataset id；缺行 =
   *  默认 private + open——「不主动选就永不开放」的假后端镜像）。 */
  datasetVisibility?: Record<string, FakeDatasetVisibilitySeed>;
  /** 定向授权种子（键 = dataset id；值 = 被授权 customer_id 列表）。 */
  datasetGrants?: Record<string, string[]>;
  /** 数据集级访问授权种子（wire-dataset-acl；键 = dataset id）。缺行时按
   *  数据集自身的 acl_mode（缺省 team）与空名单初始化。 */
  datasetAcl?: Record<
    string,
    {
      mode?: "team" | "restricted";
      grants?: Array<{ logto_sub: string; permission: "view" | "edit" }>;
    }
  >;
  /** 强制 PUT /datasets/{id}/access 返回该状态（400/422 如实回显测试）。 */
  failDatasetAclPutStatus?: number;
  failDatasetAclPutDetail?: string;
  /** 市场浏览面入参覆盖（缺省按 datasetVisibility 推导：仅 market 档进列表，
   *  归属过滤豁免——市场面可见**其他客户**已发布的条目）。 */
  failMarketListStatus?: number;
  /** 强制市场列表端点返回该状态。 */
  failVisibilityPutStatus?: number;
  failVisibilityPutDetail?: string | ApiDetailItem[];
  /** 强制 PUT /visibility 返回该状态（如敏感档 422 拒收）。 */
  failMarketSubscribeStatus?: number;
  /** 强制订阅端点返回该状态。 */
  failAccessRequestStatus?: number;
  /** 强制 POST access-requests 返回该状态。 */
  failAccessRequestsListStatus?: number;
  /** 强制 GET access-requests 返回该状态（发布方查看）。 */
  failAccessDecideStatus?: number;
  /** 强制 approve/reject 返回该状态。 */
  /** 预置市场订阅（wire snake_case：customerId → dataset id 列表）。 */
  marketSubscriptions?: Record<string, string[]>;
  /** 预置准入申请（customerId+datasetId 唯一键在假后端内合成）。 */
  accessRequests?: FakeAccessRequestSeed[];
}

/** 源包种子（wire-source-packs）：假后端按 indicators 分页切片出详情。 */
export type FakePackSeed = {
  id: string;
  name_zh: string;
  name_en: string;
  desc: string;
  indicators: PackIndicator[];
};

/** 实体种子（wire-entity-layer）：datasetId 引用种子数据集。 */
export type FakeEntitySeed = {
  id: number;
  name: string;
  datasetId: string;
  pkColumns: string[];
  description?: string | null;
  /** 带.customer_id 的仅同客户可见（跨企业 404 不泄露）。 */
  customer_id?: string;
};

/** 实体关系种子：fromEntityId/toEntityId 引用实体 id。 */
export type FakeEntityRelationshipSeed = {
  id: number;
  fromEntityId: number;
  fromColumn: string;
  toEntityId: number;
  toColumn: string;
  kind: "n:1" | "1:1" | "m:n";
  label?: string | null;
  /** m:n 中间集三键（wire-entity-mn-materialize；非 m:n 恒缺省）。 */
  junctionDatasetId?: string | null;
  junctionSourceColumn?: string | null;
  junctionTargetColumn?: string | null;
  customer_id?: string;
};

/** 域动作类型种子（wire-entity-write-surface）：entityId 引用种子实体；
 *  datasetId 缺省取实体绑定数据集；带 customer_id 的仅同客户可见。 */
export type FakeDomainActionSeed = {
  id: number;
  entityId: number;
  name: string;
  risk?: string;
  fields?: Array<{
    name: string;
    label?: string;
    input?: string;
    required?: boolean;
    options?: string[];
  }>;
  effectKind?: string;
  datasetId?: string;
  enabled?: boolean;
  customer_id?: string;
};

/** 域动作实例种子：wire snake_case 形状直存；status 缺省待确认。 */
export type FakeDomainActionInstanceSeed = {
  id: number;
  typeId: number;
  proposalId?: number | null;
  proposerSub?: string;
  channel?: string;
  sourceRef?: string | null;
  status?: string;
  confirmedBy?: string | null;
  correctionId?: number | null;
  /** metadata 效果（内置声明）成功回填的实体/关系 id 字符串。 */
  resultRef?: string | null;
  error?: string | null;
  customer_id?: string;
};

/** 实体假后端行（getEntities 断言面）。 */
export type FakeEntityRow = {
  id: number;
  customer_id?: string;
  name: string;
  datasetId: string;
  pkColumns: string[];
  description: string | null;
};

/** 关系假后端行（getEntityRelationships 断言面）。 */
export type FakeEntityRelationshipRow = {
  id: number;
  customer_id?: string;
  fromEntityId: number;
  fromColumn: string;
  toEntityId: number;
  toColumn: string;
  kind: "n:1" | "1:1" | "m:n";
  label: string | null;
  /** m:n 中间集三键（wire-entity-mn-materialize；非 m:n 恒 null）。 */
  junctionDatasetId: string | null;
  junctionSourceColumn: string | null;
  junctionTargetColumn: string | null;
};

/** 团队成员种子（wire-team-spaces）：logtoSub 即 Bearer sub 匹配键。 */
export type FakeTeamMemberSeed = {
  logtoSub: string;
  role: "owner" | "editor" | "viewer";
  /** 加入时间（缺省固定 2026-10-01 09:00）。 */
  createdAt?: string;
  /** 邀请人 sub（owner 首建行为 null）。 */
  invitedBySub?: string | null;
};

/** 工作台空聚合缺省体（wire-personal-workbench）：各段零值、recent 空数组、
 *  free_quota / demands.latest 为 null——「无数据」如实呈现，绝不编造计数。 */
const EMPTY_WORKBENCH: WorkbenchSummary = {
  datasets: { total: 0, api_tier: 0, cell_tier: 0, recent: [] },
  free_quota: null,
  packs: { subscribed: 0, recent: [] },
  artifacts: {
    active_total: 0,
    by_kind: {
      dashboard: 0,
      composite_indicator: 0,
      research_report: 0,
      alert_rule: 0,
      refresh_schedule: 0,
    },
    recent: [],
  },
  alerts: { alert_rules: 0, firing: 0, events_24h: 0 },
  demands: { open: 0, latest: null },
};

/** 待用邀请种子（缺省码 wsi-seed{id}、24 小时后过期）。 */
export type FakeTeamInviteSeed = {
  id: number;
  code?: string;
  role: "editor" | "viewer";
  /** 过期时间 ISO（缺省 now + 24h——倒计时口径与真实时钟一致）。 */
  expiresAt?: string;
  createdAt?: string;
};

/** 团队成员假后端行（getTeamMembers 断言面；camelCase 内存态）。 */
export type FakeTeamMemberRow = {
  logtoSub: string;
  role: "owner" | "editor" | "viewer";
  createdAt: string;
  invitedBySub: string | null;
};

/** 「我的团队」种子行（wire-team-spaces-v2）：customer_id 数值键；is_active
 *  由假后端按活跃队指针推导（POST switch 原地更新）。 */
export type FakeTeamSeed = {
  customer_id: number;
  customer_name: string;
  role: "owner" | "editor" | "viewer";
};

/** 待用邀请假后端行（getTeamInvites 断言面）。 */
export type FakeTeamInviteRow = {
  id: number;
  code: string;
  role: "editor" | "viewer";
  expiresAt: string;
  createdAt: string;
};

/** 告警评估状态种子（wire-alert-evaluation）：wire snake_case 形状直存。 */
export type FakeAlertStateSeed = {
  state: "ok" | "firing";
  last_value?: number | null;
  last_evaluated_at?: string | null;
  last_error_at?: string | null;
  /** 带 customer_id 的仅同客户可见。 */
  customer_id?: string;
};

/** 告警事件种子（wire-alert-evaluation）：wire snake_case 形状，
 *  artifact_id 引用种子产物（须 kind=alert_rule 才会被端点放行）。 */
export type FakeAlertEventSeed = {
  id: number;
  artifact_id: number;
  kind: "firing" | "resolved" | "error" | "notify_failed";
  value?: number | null;
  threshold?: number | null;
  detail?: string | null;
  created_at?: string;
  /** 带 customer_id 的仅同客户可见。 */
  customer_id?: string;
};

/** 个人偏好种子（wire-personal-preferences）：wire snake_case 形状直存
 *  （键面冻结，不做 camel 翻译）；缺省键 = null（未设）。 */
export type FakePreferencesSeed = {
  ui_locale?: "zh" | "en" | null;
  landing_tab?: string | null;
  pinned_dashboard_id?: number | null;
};

/** 资产元数据种子（wire-asset-catalog）：未补全字段按默认值（常规/无标签）。 */
export type FakeAssetMetaSeed = {
  domain?: string | null;
  tags?: string[];
  description?: string | null;
  cadence_hint?: "daily" | "weekly" | "monthly" | null;
  owner_note?: string | null;
  sensitivity?: "常规" | "内部" | "敏感";
  /** 生效节奏（api 层数据集来自指标注册表）。 */
  cadence_effective?: "daily" | "weekly" | "monthly" | null;
  customer_id?: string;
};

/** 资产元数据留痕种子（wire 形状：at/actor_sub/before/after）。 */
export type FakeAssetMetaEventSeed = {
  at: string;
  actor_sub: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
};

/** 数据集质量种子（wire-dataset-quality）：latest/history/freshness 按契约
 *  形状直存（latest=null 即「暂无快照」诚实态）。 */
export type FakeQualitySeed = {
  latest?: Record<string, unknown> | null;
  history?: Array<Record<string, unknown>>;
  /** 对象 {freshness,last_refresh_at} 或裸枚举字符串（两种下发形态都收）。 */
  freshness?: unknown;
  cadence_hint?: "daily" | "weekly" | "monthly" | null;
};

/** 管线运行失败注入（wire-transform-pipelines）：seq 命中的步骤按 kind 失败
 *  并中止后续步骤（后续步骤绝不产生记录——不伪造 skipped）。 */
export type FakePipelineFailAt = {
  seq: number;
  kind: "failed" | "quota_exhausted" | "unsupported";
  /** 覆盖默认错误文案（如实呈现面）。 */
  error?: string;
};

/** 预置运行历史种子（steps 缺省按该版本步骤生成全 success 记录）。 */
export type FakePipelineRunSeed = {
  id: number;
  status: "success" | "failed";
  version: number;
  trigger: "manual" | "schedule";
  started_at: string;
  finished_at?: string | null;
  error_text?: string | null;
  steps?: PipelineStepRun[];
};

/** 数据集可见性种子（wire-dataset-marketplace）：四档 + 开放模式 + 发布锚。 */
export type FakeDatasetVisibilitySeed = {
  visibility?: "private" | "link" | "directed" | "market";
  access_mode?: "open" | "approval";
  published_at?: string | null;
  published_version?: string | null;
};

/** 准入申请种子（wire-dataset-marketplace）：requester + dataset 唯一。 */
export type FakeAccessRequestSeed = {
  id: number;
  dataset_id: string;
  requester_customer_id: string;
  status?: "pending" | "approved" | "rejected";
  created_at?: string;
};

/** 管线种子（wire-transform-pipelines 5.x）：versions 按序即 v1..vN（每版
 *  完整步骤定义）；schedule 已挂接态；failAt 对后续 run 生效。 */
export type FakePipelineSeed = {
  id: number;
  name: string;
  description?: string | null;
  base_dataset_id: string;
  versions?: Array<{
    steps: PipelineStepDef[];
    created_at?: string;
    created_by?: string;
  }>;
  schedule?: PipelineScheduleInfo | null;
  runs?: FakePipelineRunSeed[];
  failAt?: FakePipelineFailAt;
  customer_id?: string;
};

/** 安装假后端为 global fetch；返回调用记录与状态注入接口。 */
export function installConsoleBackend(
  conversations: Conversation[],
  options: ConsoleBackendOptions = {},
): {
  calls: FetchCall[];
  fetchImpl: typeof fetch;
  addMessage: (
    conversationId: number,
    role: MessageRole,
    content: string,
  ) => ChatMessage;
  addArtifact: (artifact: Omit<DraftArtifact, "customer_id" | "status">) => void;
  addLifecycleArtifact: (seed: LifecycleArtifactSeed) => void;
  addDataset: (dataset: Dataset) => void;
  addConnector: (connector: Connector) => void;
  addRows: (
    datasetId: string,
    columns: DatasetColumn[],
    rows: Array<Record<string, unknown>>,
  ) => void;
  addCorrection: (correction: Correction) => void;
  addSync: (sync: SyncRecord) => void;
  addRefreshSchedule: (seed: RefreshSchedule & { customer_id?: string }) => void;
  getDatasets: () => Dataset[];
  getAssetMeta: (datasetId: string) => Record<string, unknown>;
  getAssetMetaEvents: (datasetId: string) => FakeAssetMetaEventSeed[];
  getCorrections: (datasetId: string) => Correction[];
  getPackSubscriptions: (customerId?: string) => string[];
    getEntities: () => FakeEntityRow[];
    getEntityRelationships: () => FakeEntityRelationshipRow[];
    getTeamMembers: () => FakeTeamMemberRow[];
    getTeamInvites: () => FakeTeamInviteRow[];
    /** 「我的团队」清单断言面（wire-team-spaces-v2；wire snake_case 形状）。 */
    getTeams: () => Array<{
      customer_id: number;
      customer_name: string;
      role: "owner" | "editor" | "viewer";
      is_active: boolean;
    }>;
    /** 活跃队指针断言面（POST switch 后变化）。 */
    getActiveTeamId: () => number;
    getAlertEvents: () => FakeAlertEventSeed[];
    getAlertStates: () => Record<string, FakeAlertStateSeed>;
    getPreferences: (sub?: string) => Required<FakePreferencesSeed>;
    /** 管线断言面：当前内存态（版本/调度）。 */
    getPipelines: () => Array<{
      id: number;
      name: string;
      current_version: number;
      versions: Array<{ version: number; steps: PipelineStepDef[] }>;
      schedule: PipelineScheduleInfo | null;
    }>;
    /** 运行历史断言面（倒序摘要）。 */
    getPipelineRuns: (pipelineId: number) => PipelineRunSummary[];
    addAlertEvent: (seed: FakeAlertEventSeed) => void;
    /** 可见性行断言面（wire-dataset-marketplace）。 */
    getDatasetVisibility: (datasetId: string) => {
      visibility: "private" | "link" | "directed" | "market";
      access_mode: "open" | "approval";
      published_at: string | null;
      published_version: string | null;
    };
    /** 定向授权名单断言面。 */
    getDatasetGrants: (datasetId: string) => string[];
    /** 数据集级访问授权断言面（wire-dataset-acl；wire 形状）。 */
    getDatasetAccess: (datasetId: string) => {
      mode: "team" | "restricted";
      grants: Array<{ logto_sub: string; permission: "view" | "edit" }>;
    };
    /** 市场订阅断言面（customerId → dataset ids；缺省种子首客户）。 */
    getMarketSubscriptions: (cid?: string) => string[];
    /** 准入申请断言面。 */
    getAccessRequests: () => Array<{
      id: number;
      dataset_id: string;
      requester_customer_id: string;
      status: string;
      created_at: string;
      decided_at: string | null;
    }>;
    /** doc 草稿层断言面（wire-rich-text-docs 6.2）：无行回 null。 */
    getDocDraft: (
      artifactId: number,
    ) => { text: string; updated_at: string } | null;
  } {
  const store = new Map<number, StoredConversation>();
  let nextConversationId = 1;
  let nextMessageId = 1;
  let nextArtifactId = 1;
  for (const meta of conversations) {
    nextConversationId = Math.max(nextConversationId, meta.id + 1);
    store.set(meta.id, { meta, messages: [], artifacts: [] });
  }

  // ── 数据集假后端状态（我的数据） ──
  const datasets = new Map<string, Dataset>();
  let nextDatasetSeq = 1;
  for (const dataset of options.datasets ?? []) datasets.set(dataset.id, { ...dataset });
  while (datasets.has(`ds-${nextDatasetSeq}`)) nextDatasetSeq += 1;
  const rowsStore = new Map<
    string,
    { columns: DatasetColumn[]; rows: Array<Record<string, unknown>> }
  >();
  const anchorRows = (rows: Array<Record<string, unknown>>): Array<Record<string, unknown>> =>
    rows.map((row, i) => ({ ...row, __row_id: row.__row_id ?? `hash:seed-${i + 1}` }));
  for (const [id, data] of Object.entries(options.datasetRows ?? {})) {
    rowsStore.set(id, { columns: data.columns, rows: anchorRows(data.rows) });
  }
  const uploads = new Map<string, UploadPreview>();
  const connectors = new Map<string, Connector>();
  for (const connector of options.connectors ?? []) {
    connectors.set(connector.id, { ...connector });
  }
  const corrections = new Map<string, Correction[]>();
  const keysStore = new Map<
    string,
    {
      id: string;
      customer_id?: string;
      label: string;
      fingerprint: string;
      dataset_ids: string[];
      status: string;
      created_at: string;
      revoked_at: string | null;
      last_used_at: string | null;
    }
  >();
  const syncs = new Map<string, SyncRecord[]>();
  // ── 需求单假后端状态（我的需求 / 提交表单）──
  const demandsStore = new Map<number, CustomerDemand>();
  let nextDemandId = 1;
  for (const demand of options.demands ?? []) {
    demandsStore.set(demand.id, { ...demand });
    nextDemandId = Math.max(nextDemandId, demand.id + 1);
  }
  let nextUploadSeq = 1;
  let nextCorrectionId = 1;
  let nextTokenSeq = 1;
  // ── 源包假后端状态（wire-source-packs）：订阅按 Bearer 客户隔离 ──
  const packsStore = new Map<string, FakePackSeed>();
  for (const pack of options.packs ?? []) packsStore.set(pack.id, pack);
  const packSubscriptions = new Map<string, string[]>();
  if (options.packSubscriptions?.length) {
    packSubscriptions.set("demo-customer", [...options.packSubscriptions]);
  }

  // ── 实体语义层假后端状态（wire-entity-layer）：信封形状逐端点对齐生产
  //    契约（GET 列表 {entities:[…]}、graph {nodes,edges}、创建/关系建为裸
  //    项 201——教训：假后端形状漂移会掩盖客户端解包缺失）。──
  const entitiesStore = new Map<number, FakeEntityRow>();
  let nextEntityId = 1;
  for (const seed of options.entities ?? []) {
    entitiesStore.set(seed.id, {
      id: seed.id,
      customer_id: seed.customer_id,
      name: seed.name,
      datasetId: seed.datasetId,
      pkColumns: [...seed.pkColumns],
      description: seed.description ?? null,
    });
    nextEntityId = Math.max(nextEntityId, seed.id + 1);
  }
  const entityRelationshipsStore = new Map<number, FakeEntityRelationshipRow>();
  let nextEntityRelationshipId = 1;
  for (const seed of options.entityRelationships ?? []) {
    entityRelationshipsStore.set(seed.id, {
      id: seed.id,
      customer_id: seed.customer_id,
      fromEntityId: seed.fromEntityId,
      fromColumn: seed.fromColumn,
      toEntityId: seed.toEntityId,
      toColumn: seed.toColumn,
      kind: seed.kind,
      label: seed.label ?? null,
      junctionDatasetId: seed.junctionDatasetId ?? null,
      junctionSourceColumn: seed.junctionSourceColumn ?? null,
      junctionTargetColumn: seed.junctionTargetColumn ?? null,
    });
    nextEntityRelationshipId = Math.max(nextEntityRelationshipId, seed.id + 1);
  }
  // ── 域动作读面假后端状态（wire-entity-write-surface）：客户声明类型按
  //    实体挂载；内置两行只读合成注入（负 id、builtin=true）；实例台账
  //    result_ref 直存（内置声明执行回填实体/关系 id）。──
  const domainActionTypesStore = new Map<number, FakeDomainActionSeed>();
  for (const seed of options.domainActions ?? []) {
    domainActionTypesStore.set(seed.id, { ...seed });
  }
  const domainActionInstancesStore = new Map<
    number,
    FakeDomainActionInstanceSeed
  >();
  for (const seed of options.domainActionInstances ?? []) {
    domainActionInstancesStore.set(seed.id, { ...seed });
  }

  // ── 定时刷新调度假后端状态（wire-refresh-scheduler 4.1）：带 customer_id
  //    的种子仅同客户可见；创建校验语义对齐 api（类型/地板/配额）。──
  type StoredSchedule = RefreshSchedule & { customer_id?: string };
  const schedulesStore = new Map<number, StoredSchedule>();
  let nextScheduleId = 1;
  for (const seed of options.refreshSchedules ?? []) {
    schedulesStore.set(seed.id, { ...seed });
    nextScheduleId = Math.max(nextScheduleId, seed.id + 1);
  }

  // ── 团队空间假后端状态（wire-team-spaces 任务 5）：成员按 logtoSub 键存；
  //    GET me 段按 bearer sub 命中种子行，未命中回落 owner（现状单 owner
  //    语义——首登开户即 owner）。邀请码 wsi- 前缀、24 小时窗。──
  const teamMembersStore = new Map<string, FakeTeamMemberRow>();
  for (const seed of options.teamMembers ?? []) {
    teamMembersStore.set(seed.logtoSub, {
      logtoSub: seed.logtoSub,
      role: seed.role,
      createdAt: seed.createdAt ?? "2026-10-01T09:00:00",
      invitedBySub: seed.invitedBySub ?? null,
    });
  }
  const teamInvitesStore = new Map<number, FakeTeamInviteRow>();
  let nextTeamInviteId = 1;
  for (const seed of options.teamInvites ?? []) {
    teamInvitesStore.set(seed.id, {
      id: seed.id,
      code: seed.code ?? `wsi-seed${seed.id}`,
      role: seed.role,
      expiresAt:
        seed.expiresAt ?? new Date(Date.now() + 24 * 3600_000).toISOString(),
      createdAt: seed.createdAt ?? "2026-10-12T08:00:00",
    });
    nextTeamInviteId = Math.max(nextTeamInviteId, seed.id + 1);
  }

  // ── 我的团队假后端状态（wire-team-spaces-v2）：种子清单直存（is_active
  //    按活跃队指针推导）；无种子 = 单队身份（teams 长度 1——UI 切换面/
  //    顶栏徽标零渲染）。席位 used 缺省按名单行数、limit 缺省 8；POST
  //    invites 满/超配一律 409（grandfather：不踢人只停邀新）。──
  const teamSeedsStore: FakeTeamSeed[] = [...(options.teams ?? [])];
  let activeTeamId =
    options.activeTeamId ?? teamSeedsStore[0]?.customer_id ?? 1;

  // ── 告警执行假后端状态（wire-alert-evaluation 任务 5）：states map /
  //    事件列表 snake_case wire 形状直存；试运行返回可配置（零副作用——
  //    不回写 states/events，语义对齐 api 侧 test 端点）。──
  const alertStatesStore = new Map<string, FakeAlertStateSeed>();
  for (const [key, seed] of Object.entries(options.alertStates ?? {})) {
    alertStatesStore.set(key, { ...seed });
  }
  const alertEventsStore = new Map<number, FakeAlertEventSeed>();
  let nextAlertEventId = 1;
  for (const seed of options.alertEvents ?? []) {
    alertEventsStore.set(seed.id, { ...seed });
    nextAlertEventId = Math.max(nextAlertEventId, seed.id + 1);
  }

  // ── 个人偏好假后端状态（wire-personal-preferences）：per-sub 一人一行，
  //    wire snake_case 三键直存；PUT 部分更新（缺键不动、null 清空）+ 422
  //    校验（locale 枚举 / 九页签 / 置顶须为本客户未归档 dashboard——
  //    跨客户与失存同文案不泄露）；GET 对置顶指针重验自愈（产物失存/
  //    归档/他企 → null 且清列，与 api 侧读时自愈语义一致）。──
  const preferencesStore = new Map<string, Required<FakePreferencesSeed>>();
  for (const [sub, seed] of Object.entries(options.preferences ?? {})) {
    preferencesStore.set(sub, {
      ui_locale: seed.ui_locale ?? null,
      landing_tab: seed.landing_tab ?? null,
      pinned_dashboard_id: seed.pinned_dashboard_id ?? null,
    });
  }

  // ── 资产目录/质量假后端状态（wire-asset-catalog / wire-dataset-quality）：
  //    元数据懒建行（首次 PATCH 才落行，GET 缺行回默认值——LEFT JOIN+COALESCE
  //    同语义）；留痕 append-only（PATCH 续记最前）；质量种子直存。──
  type StoredAssetMeta = Required<
    Omit<FakeAssetMetaSeed, "customer_id">
  > & { customer_id?: string };
  const assetMetaStore = new Map<string, StoredAssetMeta>();
  for (const [id, seed] of Object.entries(options.assetMeta ?? {})) {
    assetMetaStore.set(id, {
      customer_id: seed.customer_id,
      domain: seed.domain ?? null,
      tags: seed.tags ? [...seed.tags] : [],
      description: seed.description ?? null,
      cadence_hint: seed.cadence_hint ?? null,
      owner_note: seed.owner_note ?? null,
      sensitivity: seed.sensitivity ?? "常规",
      cadence_effective: seed.cadence_effective ?? null,
    });
  }
  const assetEventsStore = new Map<string, FakeAssetMetaEventSeed[]>();
  for (const [id, seeds] of Object.entries(options.assetMetaEvents ?? {})) {
    assetEventsStore.set(id, seeds.map((event) => ({ ...event })));
  }
  const qualityStore = new Map<string, FakeQualitySeed>();
  for (const [id, seed] of Object.entries(options.quality ?? {})) {
    qualityStore.set(id, { ...seed });
  }

  // ── 数据开放与市场假后端状态（wire-dataset-marketplace）：可见性行懒建
  //    （缺行 = private + open——默认私有镜像）、grant 按数据集全量替换、
  //    订阅与申请按 customer_id 隔离；市场列表仅放行 visibility='market'。──
  type StoredVisibility = Required<
    Omit<FakeDatasetVisibilitySeed, "published_at" | "published_version">
  > & { published_at: string | null; published_version: string | null };
  const visibilityStore = new Map<string, StoredVisibility>();
  const visibilityOf = (id: string): StoredVisibility => {
    const row = visibilityStore.get(id);
    if (row) return row;
    const fresh: StoredVisibility = {
      visibility: "private",
      access_mode: "open",
      published_at: null,
      published_version: null,
    };
    visibilityStore.set(id, fresh);
    return fresh;
  };
  for (const [id, seed] of Object.entries(options.datasetVisibility ?? {})) {
    visibilityStore.set(id, {
      visibility: seed.visibility ?? "private",
      access_mode: seed.access_mode ?? "open",
      published_at: seed.published_at ?? null,
      published_version: seed.published_version ?? null,
    });
  }
  const grantsStore = new Map<string, string[]>();
  for (const [id, list] of Object.entries(options.datasetGrants ?? {})) {
    grantsStore.set(id, [...list]);
  }
  // ── 数据集级访问授权（wire-dataset-acl）：mode + 成员授权表（wire 形状）。
  //    缺种子行时按数据集 acl_mode（缺省 team）与空名单初始化（GET 懒建）。──
  const aclModeStore = new Map<string, "team" | "restricted">();
  const aclGrantsStore = new Map<
    string,
    Array<{ logto_sub: string; permission: "view" | "edit" }>
  >();
  const aclOf = (
    id: string,
  ): {
    mode: "team" | "restricted";
    grants: Array<{ logto_sub: string; permission: "view" | "edit" }>;
  } => {
    if (!aclModeStore.has(id)) {
      const seed = options.datasetAcl?.[id];
      const datasetMode = datasets.get(id)?.acl_mode;
      aclModeStore.set(
        id,
        seed?.mode ??
          (datasetMode === "restricted" ? "restricted" : "team"),
      );
      aclGrantsStore.set(id, [...(seed?.grants ?? [])]);
    }
    return {
      mode: aclModeStore.get(id)!,
      grants: [...(aclGrantsStore.get(id) ?? [])],
    };
  };
  const marketSubscriptions = new Map<string, string[]>();
  for (const [cid, list] of Object.entries(options.marketSubscriptions ?? {})) {
    marketSubscriptions.set(cid, [...list]);
  }
  const accessRequestsStore = new Map<
    number,
    Required<FakeAccessRequestSeed> & { decided_at: string | null }
  >();
  let nextAccessRequestId = 1;
  for (const seed of options.accessRequests ?? []) {
    accessRequestsStore.set(seed.id, {
      id: seed.id,
      dataset_id: seed.dataset_id,
      requester_customer_id: seed.requester_customer_id,
      status: seed.status ?? "pending",
      created_at: seed.created_at ?? "2026-10-13T08:00:00",
      decided_at: null,
    });
    nextAccessRequestId = Math.max(nextAccessRequestId, seed.id + 1);
  }

  // ── 变换管线索度假后端状态（wire-transform-pipelines 5.x）：版本
  //    append-only（PUT 改步骤/基础集 → 追加新版本回号）、运行两级留痕
  //    （run + step，失败中止后续不伪造记录）、输出槽位稳定（同
  //    （管线,版本,步骤序号）→ 同一 dataset id，重跑原地刷新语义）、
  //    调度独立态（pause/resume/delete）；写操作 viewer 403。──
  interface StoredPipelineVersion {
    version: number;
    steps: PipelineStepDef[];
    created_at: string;
    created_by: string;
  }
  interface StoredPipeline {
    id: number;
    customer_id?: string;
    name: string;
    description: string | null;
    base_dataset_id: string;
    created_at: string;
    updated_at: string;
    versions: StoredPipelineVersion[];
    schedule: PipelineScheduleInfo | null;
    failAt?: FakePipelineFailAt;
  }
  type StoredPipelineRun = PipelineRunDetail;
  const pipelinesStore = new Map<number, StoredPipeline>();
  const pipelineRunsStore = new Map<number, StoredPipelineRun[]>();
  let nextPipelineId = 1;
  let nextPipelineRunId = 1;
  const pipelineRunBase = Date.parse("2026-10-13T09:00:00Z");
  let pipelineRunClock = 0;
  for (const seed of options.pipelines ?? []) {
    const versionSeeds = seed.versions ?? [];
    const versions: StoredPipelineVersion[] = (versionSeeds.length
      ? versionSeeds
      : [{ steps: [] }]
    ).map((entry, i) => ({
      version: i + 1,
      steps: entry.steps.map((step) => ({
        name: step.name,
        op: step.op,
        params: { ...step.params },
      })),
      created_at: entry.created_at ?? `2026-10-12T10:${String(i).padStart(2, "0")}:00`,
      created_by: entry.created_by ?? "demo-customer",
    }));
    pipelinesStore.set(seed.id, {
      id: seed.id,
      customer_id: seed.customer_id,
      name: seed.name,
      description: seed.description ?? null,
      base_dataset_id: seed.base_dataset_id,
      created_at: "2026-10-12T09:00:00",
      updated_at: "2026-10-13T08:00:00",
      versions,
      schedule: seed.schedule ? { ...seed.schedule } : null,
      failAt: seed.failAt,
    });
    nextPipelineId = Math.max(nextPipelineId, seed.id + 1);
    // 预置运行历史：steps 缺省按对应版本步骤生成全 success 记录。
    if (seed.runs?.length) {
      const runs: StoredPipelineRun[] = seed.runs.map((run) => ({
        id: run.id,
        status: run.status,
        version: run.version,
        trigger: run.trigger,
        started_at: run.started_at,
        finished_at: run.finished_at ?? null,
        error_text: run.error_text ?? null,
        steps:
          run.steps ??
          (versions.find((v) => v.version === run.version)?.steps ?? []).map(
            (step, i) =>
              simulateStep(seed.id, run.version, step, i + 1, 100),
          ),
      }));
      pipelineRunsStore.set(
        seed.id,
        runs.sort((a, b) => b.id - a.id),
      );
      nextPipelineRunId = Math.max(
        nextPipelineRunId,
        ...seed.runs.map((run) => run.id + 1),
      );
    }
  }
  /** 单步骤 success 记录（输出槽位 id 稳定：pl-{管线}-v{版本}-s{序号}）。 */
  function simulateStep(
    pipelineId: number,
    version: number,
    step: PipelineStepDef,
    seq: number,
    rows: number,
  ): PipelineStepRun {
    return {
      seq,
      name: step.name,
      op: step.op,
      status: "success",
      output_dataset_id: `pl-${pipelineId}-v${version}-s${seq}`,
      output_dataset_name: `${step.name}（v${version} 第${seq}步输出）`,
      row_count: Math.max(0, rows - (seq - 1) * 10),
      duration_ms: 120 * seq,
      error_text: null,
    };
  }
  /** 本客户可见管线（未钉 customer_id 的种子对任何登录客户可见）。 */
  function ownPipeline(id: number, cid: string): StoredPipeline | undefined {
    const pipeline = pipelinesStore.get(id);
    if (!pipeline) return undefined;
    return pipeline.customer_id && pipeline.customer_id !== cid
      ? undefined
      : pipeline;
  }
  const isViewer = (cid: string): boolean =>
    teamMembersStore.get(cid)?.role === "viewer";
  const viewerDenied = () =>
    jsonResponse({ detail: "团队成员权限不足（viewer 只读）" }, 403);
  /** 步骤定义深比较（PUT 版本判定：顺序/类型/参数任一不同即变更）。 */
  function stepsEqual(
    a: PipelineStepDef[],
    b: PipelineStepDef[],
  ): boolean {
    if (a.length !== b.length) return false;
    const norm = (steps: PipelineStepDef[]) =>
      steps.map((s) => ({
        name: s.name,
        op: s.op,
        params: s.params && typeof s.params === "object" ? s.params : {},
      }));
    return JSON.stringify(norm(a)) === JSON.stringify(norm(b));
  }
  /** 步骤入参校验（形状与原子操作端点同口径；非法 422）。 */
  const validateSteps = (steps: unknown): string | null => {
    if (!Array.isArray(steps) || steps.length === 0) {
      return "steps 须为非空数组";
    }
    for (const step of steps) {
      if (!step || typeof step !== "object") return "步骤须为对象";
      const record = step as { name?: unknown; op?: unknown; params?: unknown };
      if (typeof record.name !== "string" || !record.name.trim()) {
        return "步骤名称不能为空";
      }
      if (
        record.op !== "derive" &&
        record.op !== "join" &&
        record.op !== "link" &&
        record.op !== "filter" &&
        record.op !== "project" &&
        record.op !== "aggregate" &&
        record.op !== "union" &&
        record.op !== "window" &&
        record.op !== "sql"
      ) {
        return "步骤类型只能是：derive / join / link / filter / project / aggregate / union / window / sql";
      }
      if (
        record.params !== undefined &&
        (typeof record.params !== "object" || record.params === null)
      ) {
        return "步骤参数须为对象";
      }
    }
    return null;
  };
  /** 管线详情响应体（版本 append-only 全量）。 */
  const pipelineDetailOut = (pipeline: StoredPipeline): Record<string, unknown> => ({
    id: pipeline.id,
    name: pipeline.name,
    description: pipeline.description,
    base_dataset_id: pipeline.base_dataset_id,
    base_dataset_name:
      datasets.get(pipeline.base_dataset_id)?.name ?? pipeline.base_dataset_id,
    current_version: pipeline.versions.length,
    updated_at: pipeline.updated_at,
    versions: pipeline.versions.map((v) => ({
      version: v.version,
      steps: v.steps,
      created_at: v.created_at,
      created_by: v.created_by,
    })),
  });
  /** 列表项（schedule/last_run 按当前态与运行历史推导）。 */
  const pipelineListItemOut = (pipeline: StoredPipeline): unknown => {
    const runs = pipelineRunsStore.get(pipeline.id) ?? [];
    const lastRun = runs[0];
    return {
      id: pipeline.id,
      name: pipeline.name,
      description: pipeline.description,
      current_version: pipeline.versions.length,
      steps_count: pipeline.versions[pipeline.versions.length - 1]?.steps.length ?? 0,
      base_dataset_id: pipeline.base_dataset_id,
      base_dataset_name:
        datasets.get(pipeline.base_dataset_id)?.name ?? pipeline.base_dataset_id,
      schedule: pipeline.schedule
        ? { ...pipeline.schedule }
        : null,
      last_run: lastRun
        ? {
            id: lastRun.id,
            status: lastRun.status,
            version: lastRun.version,
            trigger: lastRun.trigger,
            started_at: lastRun.started_at,
            finished_at: lastRun.finished_at,
          }
        : null,
      updated_at: pipeline.updated_at,
    };
  };
  /** 新鲜度收窄（缺省 unknown——未声明节奏不误判）。 */
  const freshnessOf = (
    datasetId: string,
  ): { freshness: "fresh" | "stale" | "unknown"; last_refresh_at: string | null } => {
    const raw = qualityStore.get(datasetId)?.freshness;
    if (raw === "fresh" || raw === "stale" || raw === "unknown") {
      return { freshness: raw, last_refresh_at: null };
    }
    if (raw && typeof raw === "object") {
      const record = raw as { freshness?: unknown; last_refresh_at?: unknown };
      const value = record.freshness;
      return {
        freshness:
          value === "fresh" || value === "stale" || value === "unknown"
            ? value
            : "unknown",
        last_refresh_at:
          typeof record.last_refresh_at === "string" && record.last_refresh_at
            ? record.last_refresh_at
            : null,
      };
    }
    return { freshness: "unknown", last_refresh_at: null };
  };

  /** 追加告警事件（刷新/评估推进断言面：列表重拉后可见）。 */
  function addAlertEvent(seed: Omit<FakeAlertEventSeed, "created_at"> &
    Partial<Pick<FakeAlertEventSeed, "created_at">>): void {
    alertEventsStore.set(seed.id, {
      created_at: "2026-10-12T09:00:00",
      ...seed,
    });
    nextAlertEventId = Math.max(nextAlertEventId, seed.id + 1);
  }

  function addRefreshSchedule(seed: StoredSchedule): void {
    schedulesStore.set(seed.id, { ...seed });
    nextScheduleId = Math.max(nextScheduleId, seed.id + 1);
  }

  function addDataset(dataset: Dataset): void {
    datasets.set(dataset.id, dataset);
    while (datasets.has(`ds-${nextDatasetSeq}`)) nextDatasetSeq += 1;
  }

  function addConnector(connector: Connector): void {
    connectors.set(connector.id, connector);
  }

  function addRows(
    datasetId: string,
    columns: DatasetColumn[],
    rows: Array<Record<string, unknown>>,
  ): void {
    rowsStore.set(datasetId, { columns, rows: anchorRows(rows) });
  }

  function addCorrection(correction: Correction): void {
    const list = corrections.get(correction.dataset_id) ?? [];
    list.push(correction);
    corrections.set(correction.dataset_id, list);
    nextCorrectionId = Math.max(nextCorrectionId, correction.id + 1);
  }

  function addSync(sync: SyncRecord): void {
    const list = syncs.get(sync.dataset_id) ?? [];
    list.push(sync);
    syncs.set(sync.dataset_id, list);
  }

  const chatReply =
    options.chatReply ?? [{ role: "assistant", content: "收到，已按你的意图处理。" }];

  function addMessage(
    conversationId: number,
    role: MessageRole,
    content: string,
  ): ChatMessage {
    const stored = store.get(conversationId)!;
    const message: ChatMessage = {
      id: nextMessageId++,
      conversation_id: conversationId,
      role,
      content,
      created_at: "2026-10-04T09:00:00",
    };
    stored.messages.push(message);
    return message;
  }

  function addArtifact(artifact: Omit<DraftArtifact, "customer_id" | "status">): void {
    const stored = store.get(artifact.conversation_id)!;
    stored.artifacts.push({
      ...artifact,
      id: artifact.id || nextArtifactId++,
      customer_id: stored.meta.customer_id,
      status: "draft",
    });
    nextArtifactId = Math.max(nextArtifactId, artifact.id + 1);
  }

  // ── 产物生命周期假后端状态（wire-artifacts-persistence）──
  interface StoredArtifactVersion {
    version: number;
    spec: Record<string, unknown>;
    operator: string | null;
    created_at: string;
  }

  interface StoredLifecycleArtifact {
    id: number;
    customer_id?: string;
    kind: string;
    title: string;
    status: ArtifactStatus;
    version: number;
    origin_conversation_id: number | null;
    status_history: Array<{
      status: string;
      at: string;
      by: string;
      note: string | null;
    }>;
    created_at: string;
    updated_at: string;
    versions: StoredArtifactVersion[];
  }

  const lifecycleArtifacts = new Map<number, StoredLifecycleArtifact>();
  let nextLifecycleArtifactId = 1;

  // ── doc 草稿层假后端状态（wire-rich-text-docs 任务 2.1/2.2，任务 6.2 的
  //    console 测试面）：一对一连产物（覆盖写、绝不进版本链——design D3 的
  //    硬约束在假后端同规）。缺省即无行（GET 返回 text=null，前端据此回退
  //    当前版本 spec.text）。
  const draftStore = new Map<number, { text: string; updated_at: string }>();
  // 正文上限（对齐 api 缺省 FD_WIRE_DOC_MAX_BYTES=262144；测试注入超限用）。
  const DRAFT_MAX_BYTES = 262144;

  function addLifecycleArtifact(seed: LifecycleArtifactSeed): void {
    const specList = seed.versions ?? [{ spec: seed.spec ?? {} }];
    const versions: StoredArtifactVersion[] = specList.map((entry, i) => ({
      version: i + 1,
      spec: entry.spec,
      operator: entry.operator ?? `console:demo-customer`,
      created_at: entry.created_at ?? `2026-10-04T09:${String(10 + i).padStart(2, "0")}:00`,
    }));
    const status = seed.status ?? "draft";
    lifecycleArtifacts.set(seed.id, {
      id: seed.id,
      customer_id: seed.customer_id,
      kind: seed.kind,
      title: seed.title,
      status,
      version: seed.version ?? versions.length,
      origin_conversation_id: seed.origin_conversation_id ?? null,
      status_history: [
        { status, at: "2026-10-04T09:00:00", by: "console:demo-customer", note: "seed" },
      ],
      created_at: seed.created_at ?? "2026-10-04T09:00:00",
      updated_at: seed.updated_at ?? "2026-10-04T09:30:00",
      versions,
    });
    nextLifecycleArtifactId = Math.max(nextLifecycleArtifactId, seed.id + 1);
  }

  /** 生命周期产物当前 spec（最新版本行）。 */
  function currentLifecycleSpec(artifact: StoredLifecycleArtifact): Record<string, unknown> {
    const row = artifact.versions.find((v) => v.version === artifact.version);
    return row?.spec ?? {};
  }

  function lifecycleDetail(artifact: StoredLifecycleArtifact): unknown {
    return {
      id: artifact.id,
      customer_id: artifact.customer_id ?? "demo-customer",
      kind: artifact.kind,
      title: artifact.title,
      status: artifact.status,
      version: artifact.version,
      origin_conversation_id: artifact.origin_conversation_id,
      created_at: artifact.created_at,
      updated_at: artifact.updated_at,
      spec: currentLifecycleSpec(artifact),
      status_history: artifact.status_history,
    };
  }

  /** 本客户可见的生命周期产物（未钉 customer_id 的种子对任何登录客户可见）。 */
  function ownLifecycleArtifact(
    id: number,
    customerId: string,
  ): StoredLifecycleArtifact | undefined {
    const artifact = lifecycleArtifacts.get(id);
    if (!artifact) return undefined;
    return artifact.customer_id && artifact.customer_id !== customerId
      ? undefined
      : artifact;
  }

  const calls: FetchCall[] = [];

  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit): Promise<FakeResponse> => {
    const url = new URL(String(input), "http://console.test");
    const method = (init?.method ?? "GET").toUpperCase();
    const headers = headersOf(init);
    const rawBody = init?.body;
    const isForm = typeof FormData !== "undefined" && rawBody instanceof FormData;
    calls.push({
      method,
      url,
      headers,
      body: typeof rawBody === "string" ? JSON.parse(rawBody) : undefined,
      formData: isForm ? rawBody : undefined,
    });

    const path = url.pathname;

    // ── 控制台登录/会话/用量（wire-self-serve-launch 4.4 / 6.2 契约）──
    if (path === "/api/console/auth/config" && method === "GET") {
      if (options.failAuthConfigStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `auth config ${options.failAuthConfigStatus}` },
            options.failAuthConfigStatus,
          ),
        );
      }
      return Promise.resolve(
        jsonResponse(
          options.authConfig ?? {
            configured: true,
            issuer: "https://auth.test/oidc",
            client_id: "test-client",
            scopes: ["openid", "profile", "email", "roles", "offline_access"],
            organization_required: false,
          },
        ),
      );
    }
    if (path === "/api/console/session" && method === "POST") {
      if (options.sessionFailReason) {
        return Promise.resolve(
          jsonResponse(
            { status: "failed", reason: options.sessionFailReason },
            502,
          ),
        );
      }
      return Promise.resolve(
        jsonResponse({
          status: "ready",
          customer: {
            id: 1,
            name: options.customerName ?? "演示客户",
            plan_code: options.planCode ?? "basic",
          },
          key_once: options.keyOnce ?? null,
        }),
      );
    }

    // ── 连接器下载清单（wire-connector-distribution 3.1：公开端点，置于
    //    Bearer 校验之前——语义对齐 api 侧 downloads 路由；缺省 404 即
    //    「FD_WIRE_DOWNLOADS_DIR 未配置」诚实降级态）──
    if (path === "/api/v1/downloads/connector/manifest.json" && method === "GET") {
      if (options.failManifestStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `downloads ${options.failManifestStatus}` },
            options.failManifestStatus,
          ),
        );
      }
      if (!options.connectorManifest) {
        return Promise.resolve(
          jsonResponse(
            { detail: "连接器下载目录未配置（FD_WIRE_DOWNLOADS_DIR 为空）" },
            404,
          ),
        );
      }
      return Promise.resolve(jsonResponse(options.connectorManifest));
    }

    // ── 源包公开面（wire-source-packs 2.1：列表/详情匿名可读，置于 Bearer
    //    校验之前；subscriptions 需登录，先于 /{id} 详情正则匹配以免撞名）──
    if (path === "/api/v1/packs" && method === "GET") {
      if (options.failPacksStatus) {
        return Promise.resolve(
          jsonResponse({ detail: `packs ${options.failPacksStatus}` }, options.failPacksStatus),
        );
      }
      return Promise.resolve(
        jsonResponse(
          [...packsStore.values()].map((pack) => ({
            id: pack.id,
            name_zh: pack.name_zh,
            name_en: pack.name_en,
            desc: pack.desc,
            indicator_count: pack.indicators.length,
          })),
        ),
      );
    }
    if (path === "/api/v1/packs/subscriptions" && method === "GET") {
      const bearerSub = subOfBearer(headers);
      if (!bearerSub) {
        return Promise.resolve(
          jsonResponse({ detail: "缺少 Authorization: Bearer <ID token>（登录会话）" }, 401),
        );
      }
      return Promise.resolve(jsonResponse([...(packSubscriptions.get(bearerSub) ?? [])]));
    }
    const packDetailMatch = path.match(/^\/api\/v1\/packs\/([^/]+)$/);
    if (packDetailMatch && method === "GET") {
      if (options.failPacksStatus) {
        return Promise.resolve(
          jsonResponse({ detail: `packs ${options.failPacksStatus}` }, options.failPacksStatus),
        );
      }
      const pack = packsStore.get(decodeURIComponent(packDetailMatch[1]!));
      if (!pack) {
        return Promise.resolve(jsonResponse({ detail: "pack not found" }, 404));
      }
      const page = Math.max(1, Number(url.searchParams.get("page") ?? "1") || 1);
      const pageSize = Math.max(
        1,
        Number(url.searchParams.get("page_size") ?? "20") || 20,
      );
      const start = (page - 1) * pageSize;
      return Promise.resolve(
        jsonResponse({
          id: pack.id,
          name_zh: pack.name_zh,
          name_en: pack.name_en,
          desc: pack.desc,
          items: pack.indicators.slice(start, start + pageSize),
          page,
          page_size: pageSize,
          total: pack.indicators.length,
        }),
      );
    }

    const customerId = subOfBearer(headers);
    if (!customerId) {
      return Promise.resolve(
        jsonResponse(
          { detail: "缺少 Authorization: Bearer <ID token>（登录会话）" },
          401,
        ),
      );
    }

    // ── 工作台聚合（wire-personal-workbench 任务 2.4）：GET /api/v1/workbench
    //    → 六 section 单往返；缺省空聚合（各段零值如实），响应体可整体注入；
    //    Bearer 登录身份（无会话 401，语义对齐 api 侧 require_console_customer_id）。──
    if (path === "/api/v1/workbench" && method === "GET") {
      if (options.failWorkbenchStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `workbench ${options.failWorkbenchStatus}` },
            options.failWorkbenchStatus,
          ),
        );
      }
      return Promise.resolve(
        jsonResponse(options.workbench ?? EMPTY_WORKBENCH),
      );
    }

    // ── 源包订阅面（wire-source-packs 2.2：登录；重复订阅/退订幂等 200；
    //    未知包 404；按 Bearer 客户隔离）──
    const packSubMatch = path.match(/^\/api\/v1\/packs\/([^/]+)\/(subscribe|unsubscribe)$/);
    if (packSubMatch) {
      if (options.failPackSubscribeStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `pack subscribe ${options.failPackSubscribeStatus}` },
            options.failPackSubscribeStatus,
          ),
        );
      }
      const packId = decodeURIComponent(packSubMatch[1]!);
      if (!packsStore.has(packId)) {
        return Promise.resolve(jsonResponse({ detail: "pack not found" }, 404));
      }
      const subscribing = packSubMatch[2] === "subscribe";
      const mine = [...(packSubscriptions.get(customerId) ?? [])];
      const at = mine.indexOf(packId);
      if (subscribing && at < 0) mine.push(packId);
      if (!subscribing && at >= 0) mine.splice(at, 1);
      packSubscriptions.set(customerId, mine);
      return Promise.resolve(
        jsonResponse({ pack_id: packId, subscribed: subscribing }),
      );
    }


    if (path === "/api/console/usage" && method === "GET") {
      if (options.failUsageStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `usage ${options.failUsageStatus}` },
            options.failUsageStatus,
          ),
        );
      }
      const planCode = options.planCode ?? "basic";
      return Promise.resolve(
        jsonResponse(
          options.usage ?? {
            period: "2026-10",
            plan_code: planCode,
            included_lines: 100_000,
            used_lines: 123,
            remaining_lines: 99_877,
          },
        ),
      );
    }

    // ── 个人偏好（wire-personal-preferences）：per-sub 三键；GET 重验置顶
    //    自愈（失存/归档/他企 → null 且清列）；PUT 部分更新 + 422 校验
    //    （跨客户与失存同文案不泄露）。──
    const preferenceRow = (): Required<FakePreferencesSeed> => {
      const row = preferencesStore.get(customerId);
      if (row) return row;
      const fresh: Required<FakePreferencesSeed> = {
        ui_locale: null,
        landing_tab: null,
        pinned_dashboard_id: null,
      };
      return fresh;
    };
    /** 置顶指针重验：产物存在 + kind=dashboard + 同客户 + 未归档。 */
    const pinnedStillValid = (id: number): boolean => {
      const artifact = ownLifecycleArtifact(id, customerId);
      return !!artifact && artifact.kind === "dashboard" && artifact.status !== "archived";
    };
    if (path === "/api/console/me/preferences" && method === "GET") {
      if (options.failPreferencesStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failPreferencesDetail ??
                `preferences ${options.failPreferencesStatus}`,
            },
            options.failPreferencesStatus,
          ),
        );
      }
      const row = preferenceRow();
      // 读时自愈：置顶产物被归档/删除/跨客户 → 返回 null 且清列（不留死链）。
      if (
        row.pinned_dashboard_id != null &&
        !pinnedStillValid(row.pinned_dashboard_id)
      ) {
        row.pinned_dashboard_id = null;
        preferencesStore.set(customerId, row);
      }
      return Promise.resolve(jsonResponse({ ...row }));
    }
    if (path === "/api/console/me/preferences" && method === "PUT") {
      if (options.failPreferencesStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failPreferencesDetail ??
                `preferences ${options.failPreferencesStatus}`,
            },
            options.failPreferencesStatus,
          ),
        );
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        ui_locale?: unknown;
        landing_tab?: unknown;
        pinned_dashboard_id?: unknown;
      };
      const CONSOLE_TAB_IDS = [
        "workbench",
        "chat",
        "artifacts",
        "datasets",
        "entities",
        "packs",
        "demands",
        "usage",
        "keys",
        "team",
      ];
      if (
        payload.ui_locale !== undefined &&
        payload.ui_locale !== null &&
        payload.ui_locale !== "zh" &&
        payload.ui_locale !== "en"
      ) {
        return Promise.resolve(
          jsonResponse({ detail: "ui_locale 只能是 zh 或 en" }, 422),
        );
      }
      if (
        payload.landing_tab !== undefined &&
        payload.landing_tab !== null &&
        (typeof payload.landing_tab !== "string" ||
          !CONSOLE_TAB_IDS.includes(payload.landing_tab))
      ) {
        return Promise.resolve(
          jsonResponse(
            { detail: `landing_tab 必须是十个控制台页签之一（当前 ${String(payload.landing_tab)}）` },
            422,
          ),
        );
      }
      if (
        payload.pinned_dashboard_id !== undefined &&
        payload.pinned_dashboard_id !== null
      ) {
        const id = payload.pinned_dashboard_id;
        if (typeof id !== "number" || !Number.isInteger(id)) {
          return Promise.resolve(
            jsonResponse({ detail: "pinned_dashboard_id 必须是整数" }, 422),
          );
        }
        // 跨客户与失存同文案（不泄露他户存在性）：仅校验可见性 + kind + 未归档。
        if (!pinnedStillValid(id)) {
          return Promise.resolve(
            jsonResponse(
              {
                detail:
                  "置顶看板不存在或不可用（须为本客户未归档的看板产物）",
              },
              422,
            ),
          );
        }
      }
      const row = preferenceRow();
      if (payload.ui_locale !== undefined) {
        row.ui_locale = (payload.ui_locale as "zh" | "en" | null) ?? null;
      }
      if (payload.landing_tab !== undefined) {
        row.landing_tab = (payload.landing_tab as string | null) ?? null;
      }
      if (payload.pinned_dashboard_id !== undefined) {
        row.pinned_dashboard_id =
          (payload.pinned_dashboard_id as number | null) ?? null;
      }
      preferencesStore.set(customerId, row);
      // PUT 响应 = 写后全量态（同 GET 形）。
      return Promise.resolve(jsonResponse({ ...row }));
    }

    // ── 团队空间（wire-team-spaces 任务 5 + v2）：管理端点全部 owner-only
    //    （spec「团队管理端点 MUST owner-only」——editor/viewer 一律 403
    //    detail「团队成员权限不足（团队管理仅 owner）」，与真后端文案逐字
    //    一致）；撤销/改角色 owner 行 409；席位满/超配邀新 409（detail 含
    //    档位/席位/升级指引）；teams 清单/switch 三端点全员可读可切；
    //    信封形状逐字段对齐冻结契约（snake_case）。──
    const ownTeamRole = (): "owner" | "editor" | "viewer" =>
      teamMembersStore.get(customerId)?.role ?? "owner";
    /** 名单行（bearer 未入种子时补合成 owner 行——单 owner 现状）。 */
    const teamMemberRows = (): FakeTeamMemberRow[] => {
      const rows = [...teamMembersStore.values()];
      if (!teamMembersStore.has(customerId)) {
        rows.unshift({
          logtoSub: customerId,
          role: "owner",
          createdAt: "2026-10-01T09:00:00",
          invitedBySub: null,
        });
      }
      return rows;
    };
    const teamInviteOut = (row: FakeTeamInviteRow): unknown => ({
      id: row.id,
      code: row.code,
      role: row.role,
      expires_at: row.expiresAt,
      created_at: row.createdAt,
    });
    const teamForbidden = () =>
      Promise.resolve(
        jsonResponse(
          { detail: "团队成员权限不足（团队管理仅 owner）" },
          403,
        ),
      );
    /** 席位用量（v2）：used 缺省 = 名单行数；limit 缺省 8。 */
    const seatsUsed = (): number =>
      options.teamSeats?.used ?? teamMemberRows().length;
    const seatsLimit = (): number => options.teamSeats?.limit ?? 8;

    if (path === "/api/console/team" && method === "GET") {
      if (options.failTeamStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: options.failTeamDetail ?? `team ${options.failTeamStatus}` },
            options.failTeamStatus,
          ),
        );
      }
      if (ownTeamRole() !== "owner") return teamForbidden();
      return Promise.resolve(
        jsonResponse({
          me: { sub: customerId, role: ownTeamRole() },
          members: teamMemberRows().map((row) => ({
            logto_sub: row.logtoSub,
            role: row.role,
            created_at: row.createdAt,
            ...(row.invitedBySub ? { invited_by_sub: row.invitedBySub } : {}),
          })),
          invites: [...teamInvitesStore.values()].map(teamInviteOut),
          seats_used: seatsUsed(),
          seats_limit: seatsLimit(),
        }),
      );
    }
    if (path === "/api/console/team/invites" && method === "POST") {
      if (ownTeamRole() !== "owner") return teamForbidden();
      // 满/超配一律 409（grandfather：不踢人只停邀新；detail 含档位/席位/
      // 升级指引——前端原样呈现）。
      if (seatsUsed() >= seatsLimit()) {
        return Promise.resolve(
          jsonResponse(
            {
              detail: `席位已满（当前 ${options.planCode ?? "basic"} 档含 ${seatsLimit()} 席、已用 ${seatsUsed()} 席）——移除成员或升级档位后才能邀请新成员`,
            },
            409,
          ),
        );
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as { role?: string };
      if (payload.role !== "editor" && payload.role !== "viewer") {
        return Promise.resolve(
          jsonResponse({ detail: "邀请角色只能是 editor 或 viewer" }, 422),
        );
      }
      const id = nextTeamInviteId++;
      const row: FakeTeamInviteRow = {
        id,
        code: `wsi-inv${id}`,
        role: payload.role,
        expiresAt: new Date(Date.now() + 24 * 3600_000).toISOString(),
        createdAt: new Date().toISOString(),
      };
      teamInvitesStore.set(id, row);
      // 201 响应冻结为 {id,code,role,expires_at}（无 created_at——列表才有）。
      return Promise.resolve(
        jsonResponse(
          {
            id: row.id,
            code: row.code,
            role: row.role,
            expires_at: row.expiresAt,
          },
          201,
        ),
      );
    }
    const teamInviteMatch = path.match(/^\/api\/console\/team\/invites\/(\d+)$/);
    if (teamInviteMatch && method === "DELETE") {
      if (ownTeamRole() !== "owner") return teamForbidden();
      const id = Number(teamInviteMatch[1]);
      if (!teamInvitesStore.has(id)) {
        return Promise.resolve(jsonResponse({ detail: "invite not found" }, 404));
      }
      teamInvitesStore.delete(id);
      return Promise.resolve(jsonResponse({ deleted: true }));
    }
    const teamMemberMatch = path.match(/^\/api\/console\/team\/members\/([^/]+)$/);
    if (teamMemberMatch && method === "DELETE") {
      if (ownTeamRole() !== "owner") return teamForbidden();
      const sub = decodeURIComponent(teamMemberMatch[1]!);
      const target = teamMemberRows().find((row) => row.logtoSub === sub);
      if (!target) {
        return Promise.resolve(
          jsonResponse({ detail: "成员不存在或不属于本团队" }, 404),
        );
      }
      if (target.role === "owner") {
        return Promise.resolve(
          jsonResponse(
            { detail: "所有者（owner）不可被撤销——团队必须至少保留一名所有者" },
            409,
          ),
        );
      }
      teamMembersStore.delete(sub);
      return Promise.resolve(jsonResponse({ deleted: true }));
    }
    if (teamMemberMatch && method === "PATCH") {
      // 角色就地变更 editor↔viewer（v2）：owner-only；owner 行 409；失存 404。
      if (ownTeamRole() !== "owner") return teamForbidden();
      const sub = decodeURIComponent(teamMemberMatch[1]!);
      const target = teamMembersStore.get(sub);
      if (!target) {
        return Promise.resolve(
          jsonResponse({ detail: "成员不存在或不属于本团队" }, 404),
        );
      }
      if (target.role === "owner") {
        return Promise.resolve(
          jsonResponse(
            { detail: "所有者（owner）角色不可变更——团队必须至少保留一名所有者" },
            409,
          ),
        );
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as { role?: string };
      if (payload.role !== "editor" && payload.role !== "viewer") {
        return Promise.resolve(
          jsonResponse(
            { detail: "成员角色只能是 editor 或 viewer（owner 不可经变更产生）" },
            422,
          ),
        );
      }
      target.role = payload.role;
      teamMembersStore.set(sub, target);
      // 响应 = 更新后成员行（与名单行同形，snake_case）。
      return Promise.resolve(
        jsonResponse({
          logto_sub: target.logtoSub,
          role: target.role,
          created_at: target.createdAt,
          ...(target.invitedBySub
            ? { invited_by_sub: target.invitedBySub }
            : {}),
        }),
      );
    }
    if (path === "/api/console/teams" && method === "GET") {
      // 「我的团队」清单（v2）：全员可读（单队身份长度 1）。
      if (options.failTeamsStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `teams ${options.failTeamsStatus}` },
            options.failTeamsStatus,
          ),
        );
      }
      const rows = teamSeedsStore.length
        ? teamSeedsStore.map((seed) => ({
            customer_id: seed.customer_id,
            customer_name: seed.customer_name,
            role: seed.role,
            is_active: seed.customer_id === activeTeamId,
          }))
        : [
            {
              customer_id: 1,
              customer_name: options.customerName ?? "演示客户",
              role: ownTeamRole(),
              is_active: true,
            },
          ];
      return Promise.resolve(
        jsonResponse({ teams: rows, active_customer_id: activeTeamId }),
      );
    }
    if (path === "/api/console/team/switch" && method === "POST") {
      // 切换活跃队（v2）：幂等；目标无效 404（不泄露他户存在性）。
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        customer_id?: unknown;
      };
      const targetId = payload.customer_id;
      const known = teamSeedsStore.some(
        (seed) => seed.customer_id === targetId,
      ) || (teamSeedsStore.length === 0 && targetId === 1);
      if (typeof targetId !== "number" || !known) {
        return Promise.resolve(
          jsonResponse({ detail: "目标团队无效或不可用" }, 404),
        );
      }
      activeTeamId = targetId;
      return Promise.resolve(jsonResponse({ active_customer_id: targetId }));
    }

    // ── 需求单客户面（wire-ux-consolidation 3.2/4.1/4.3：POST/GET
    //    /api/v1/demands，Bearer 身份隔离——种子带 customer_id 的仅同客户可见）──
    if (path === "/api/v1/demands" && method === "GET") {
      const mine = [...demandsStore.values()]
        .filter((d) => d.customer_id === customerId)
        .sort((a, b) => b.id - a.id);
      return Promise.resolve(jsonResponse(mine));
    }
    if (path === "/api/v1/demands" && method === "POST") {
      if (options.failDemandCreateStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failDemandCreateDetail ??
                `demand create ${options.failDemandCreateStatus}`,
            },
            options.failDemandCreateStatus,
          ),
        );
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        description?: string;
        contact_email?: string;
        company?: string;
        indicator_code?: string;
        source_urls?: unknown;
        public_data_asserted?: unknown;
      };
      if (!payload.description || !payload.description.trim()) {
        return Promise.resolve(
          jsonResponse({ detail: "需求描述不能为空（或全为空白字符）" }, 422),
        );
      }
      if (!payload.contact_email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(payload.contact_email)) {
        return Promise.resolve(
          jsonResponse({ detail: "联系邮箱格式不合法（应为 name@domain.tld 形态）" }, 422),
        );
      }
      // wire-demand-generation-orchestration 冻结契约镜像：source_urls ≥1 条
      // http(s) 公开地址（拒内嵌账密）、public_data_asserted 必须 true——
      // 422 detail 为 FastAPI 标准错误数组（非法条目按索引定位 loc）。
      const sourceUrls = Array.isArray(payload.source_urls)
        ? payload.source_urls.filter((u): u is string => typeof u === "string")
        : [];
      const detailItems: ApiDetailItem[] = [];
      if (sourceUrls.length === 0) {
        detailItems.push({
          type: "too_short",
          loc: ["body", "source_urls"],
          msg: "List should have at least 1 item after validation, not 0",
        });
      }
      sourceUrls.forEach((url, index) => {
        let parsed: URL | null = null;
        try {
          parsed = new URL(url);
        } catch {
          parsed = null;
        }
        const embeddedCredentials =
          /^[a-z][a-z0-9+.-]*:\/\/[^/@\s]+:[^/@\s]+@/i.test(url);
        if (
          !parsed ||
          (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
          embeddedCredentials
        ) {
          detailItems.push({
            type: "url_scheme",
            loc: ["body", "source_urls", index],
            msg: "Input should be a valid http(s) URL without embedded credentials",
          });
        }
      });
      if (payload.public_data_asserted !== true) {
        detailItems.push({
          type: "greater_than",
          loc: ["body", "public_data_asserted"],
          msg: "Input should be true",
        });
      }
      if (detailItems.length > 0) {
        return Promise.resolve(jsonResponse({ detail: detailItems }, 422));
      }
      const id = nextDemandId++;
      const nowIso = "2026-10-10T08:00:00";
      const oneLine = payload.description.replace(/\s+/g, " ").trim();
      const created: CustomerDemand = {
        id,
        code: `DE-20261010-${String(id).padStart(2, "0")}`,
        customer_id: customerId,
        title: oneLine.slice(0, 200),
        intent: payload.description,
        contact_email: payload.contact_email,
        company: payload.company ?? null,
        status: "submitted",
        status_history: [
          {
            status: "submitted",
            at: "2026-10-10T08:00:00+00:00",
            by: `customer:${customerId}`,
            note: "需求单提交" + (payload.indicator_code ? `（目录锚点：${payload.indicator_code}）` : ""),
          },
        ],
        rejection_reason: null,
        created_at: nowIso,
        updated_at: nowIso,
      };
      demandsStore.set(id, created);
      return Promise.resolve(jsonResponse(created, 201));
    }

    /** 本客户可见性：带 customer_id 的种子仅同客户可见（跨企业 404）。 */
    const ownDataset = (id: string): Dataset | undefined => {
      const dataset = datasets.get(id);
      if (!dataset) return undefined;
      return dataset.customer_id && dataset.customer_id !== customerId
        ? undefined
        : dataset;
    };

    /** 数据集的资产元数据（缺行=默认值；带 customer_id 的种子仅同客户可见
     *  ——与数据集可见性同纪律）。 */
    const assetMetaOf = (datasetId: string): StoredAssetMeta => {
      const row = assetMetaStore.get(datasetId);
      if (!row || (row.customer_id && row.customer_id !== customerId)) {
        return {
          domain: null,
          tags: [],
          description: null,
          cadence_hint: null,
          owner_note: null,
          sensitivity: "常规",
          cadence_effective: null,
        };
      }
      return row;
    };

    // ── 实体语义层（wire-entity-layer 3.1/3.2 + wire-entity-link 3.2）：
    //    七端点语义对齐 api 仓 entities 路由——GET 列表 {entities} 信封、
    //    POST 201 裸实体项、PUT、DELETE 级联（回带 deleted_relationships）、
    //    关系建删、关系物化（201 DatasetOut / 403 档位闸 / 422 列漂移）、
    //    graph {nodes,edges}；校验矩阵：422 逐条（FastAPI 数组形状）、409 重名/
    //    重复绑定/四元组重复、404 失存或他企不泄露；目标列 ∈ 目标主键纪律。 ──
    const ownEntity = (id: number): FakeEntityRow | undefined => {
      const row = entitiesStore.get(id);
      if (!row) return undefined;
      return row.customer_id && row.customer_id !== customerId
        ? undefined
        : row;
    };
    const ownEntityRows = (): FakeEntityRow[] =>
      [...entitiesStore.values()].filter(
        (row) => !row.customer_id || row.customer_id === customerId,
      );
    const ownEntityRelationshipRows = (): FakeEntityRelationshipRow[] =>
      [...entityRelationshipsStore.values()].filter(
        (row) => !row.customer_id || row.customer_id === customerId,
      );
    /** 实体项信封（snake_case + dataset 摘要；storage_tier 容错读取）。 */
    const entityOut = (row: FakeEntityRow): unknown => {
      const dataset = datasets.get(row.datasetId);
      return {
        id: row.id,
        name: row.name,
        description: row.description,
        pk_columns: row.pkColumns,
        dataset: dataset
          ? {
              id: dataset.id,
              key: dataset.key,
              name: dataset.name,
              row_count: dataset.row_count,
              storage_tier: (dataset as DatasetWithTier).storage_tier ?? null,
            }
          : {
              id: row.datasetId,
              key: row.datasetId,
              name: row.datasetId,
              row_count: 0,
              storage_tier: null,
            },
      };
    };
    const relationshipOut = (row: FakeEntityRelationshipRow): unknown => ({
      id: row.id,
      from_entity_id: row.fromEntityId,
      from_column: row.fromColumn,
      to_entity_id: row.toEntityId,
      to_column: row.toColumn,
      kind: row.kind,
      label: row.label,
      junction_dataset_id: row.junctionDatasetId,
      junction_source_column: row.junctionSourceColumn,
      junction_target_column: row.junctionTargetColumn,
    });

    if (path === "/api/v1/entities" && method === "GET") {
      if (options.failEntitiesStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `entities ${options.failEntitiesStatus}` },
            options.failEntitiesStatus,
          ),
        );
      }
      return Promise.resolve(
        jsonResponse({ entities: ownEntityRows().map(entityOut) }),
      );
    }
    if (path === "/api/v1/entities" && method === "POST") {
      if (options.failEntityCreateStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failEntityCreateDetail ??
                `entity create ${options.failEntityCreateStatus}`,
            },
            options.failEntityCreateStatus,
          ),
        );
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        name?: string;
        dataset_id?: string;
        pk_columns?: unknown;
        description?: string;
      };
      const name = typeof payload.name === "string" ? payload.name.trim() : "";
      const detailItems: ApiDetailItem[] = [];
      if (!name) {
        detailItems.push({
          type: "string_too_short",
          loc: ["body", "name"],
          msg: "业务名不能为空（或全为空白字符）",
        });
      } else if (name.length > 100) {
        detailItems.push({
          type: "too_long",
          loc: ["body", "name"],
          msg: "业务名长度不能超过 100 字符",
        });
      }
      const dataset = payload.dataset_id
        ? ownDataset(payload.dataset_id)
        : undefined;
      if (!dataset) {
        // 失存/他企数据集一律 404——不泄露存在性（隔离纪律）。
        return Promise.resolve(
          jsonResponse({ detail: "数据集不存在或不属于本企业" }, 404),
        );
      }
      const pkColumns = Array.isArray(payload.pk_columns)
        ? payload.pk_columns.filter((c): c is string => typeof c === "string")
        : [];
      if (pkColumns.length === 0) {
        detailItems.push({
          type: "too_short",
          loc: ["body", "pk_columns"],
          msg: "主键列至少需要一列",
        });
      }
      const duplicated = [
        ...new Set(pkColumns.filter((c, i) => pkColumns.indexOf(c) !== i)),
      ];
      if (duplicated.length > 0) {
        detailItems.push({
          type: "value_error",
          loc: ["body", "pk_columns"],
          msg: `主键列重复：${duplicated.join("、")}`,
        });
      }
      const unknownColumns = pkColumns.filter(
        (c) => !dataset.columns.some((col) => col.name === c),
      );
      if (unknownColumns.length > 0) {
        detailItems.push({
          type: "value_error",
          loc: ["body", "pk_columns"],
          msg: `主键列不在数据集「${dataset.name}」的列清单中：${unknownColumns.join("、")}`,
        });
      }
      if (detailItems.length > 0) {
        return Promise.resolve(jsonResponse({ detail: detailItems }, 422));
      }
      if (ownEntityRows().some((row) => row.name === name)) {
        return Promise.resolve(
          jsonResponse(
            { detail: `业务名「${name}」已存在——实体名在本企业内唯一` },
            409,
          ),
        );
      }
      if (ownEntityRows().some((row) => row.datasetId === dataset.id)) {
        return Promise.resolve(
          jsonResponse(
            {
              detail: `数据集「${dataset.name}」已绑定实体——每数据集至多一个实体声明`,
            },
            409,
          ),
        );
      }
      const created: FakeEntityRow = {
        id: nextEntityId++,
        customer_id: customerId,
        name,
        datasetId: dataset.id,
        pkColumns,
        description: payload.description?.trim() || null,
      };
      entitiesStore.set(created.id, created);
      return Promise.resolve(jsonResponse(entityOut(created), 201));
    }
    // graph 先于 /{id} 路由匹配（"graph" 非数字不冲突，防御性先判）。
    if (path === "/api/v1/entities/graph" && method === "GET") {
      if (options.failEntitiesStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `entities ${options.failEntitiesStatus}` },
            options.failEntitiesStatus,
          ),
        );
      }
      return Promise.resolve(
        jsonResponse({
          nodes: ownEntityRows().map(entityOut),
          edges: ownEntityRelationshipRows().map(relationshipOut),
        }),
      );
    }
    // ── 域动作读面（wire-entity-write-surface）：实体级类型 / 全量类型（客户
    //    声明 + 内置两行合成注入）/ 实例台账。内置行与 api 逐字段同形：
    //    builtin=true、负 id（-1 entity-declare、-2 entity-relation-declare）、
    //    entity_id/dataset_id/created_at/updated_at 为 null、risk=write。──
    const builtinDomainActionRows = (): unknown[] => [
      {
        id: -1,
        entity_id: null,
        name: "entity-declare",
        risk: "write",
        form_schema: {
          fields: [
            { name: "business_name", input: "text", required: true },
            { name: "dataset_key", input: "text", required: true },
            { name: "pk_columns", input: "text", required: true },
            { name: "display_column", input: "text", required: false },
            { name: "column_notes", input: "text", required: false },
          ],
        },
        effect: { kind: "metadata", op: "entity.create" },
        enabled: true,
        dataset_id: null,
        pk_columns_json: [],
        created_at: null,
        updated_at: null,
        builtin: true,
      },
      {
        id: -2,
        entity_id: null,
        name: "entity-relation-declare",
        risk: "write",
        form_schema: {
          fields: [
            { name: "source_entity", input: "text", required: true },
            { name: "source_column", input: "text", required: true },
            { name: "target_entity", input: "text", required: true },
            { name: "target_column", input: "text", required: true },
            {
              name: "kind",
              input: "select",
              required: true,
              options: ["n:1", "1:1", "1:n", "m:n"],
            },
            { name: "junction_dataset_key", input: "text", required: false },
            { name: "junction_source_column", input: "text", required: false },
            { name: "junction_target_column", input: "text", required: false },
          ],
        },
        effect: { kind: "metadata", op: "relationship.create" },
        enabled: true,
        dataset_id: null,
        pk_columns_json: [],
        created_at: null,
        updated_at: null,
        builtin: true,
      },
    ];
    const domainActionTypeOut = (seed: FakeDomainActionSeed): unknown => ({
      id: seed.id,
      entity_id: seed.entityId,
      name: seed.name,
      risk: seed.risk ?? "write",
      form_schema: {
        fields: (seed.fields ?? []).map((field) => ({
          name: field.name,
          ...(field.label ? { label: field.label } : {}),
          input: field.input ?? "text",
          required: field.required ?? false,
          ...(field.options ? { options: field.options } : {}),
        })),
      },
      effect: { kind: seed.effectKind ?? "insert", value_mapping: {} },
      enabled: seed.enabled ?? true,
      dataset_id:
        seed.datasetId ?? entitiesStore.get(seed.entityId)?.datasetId ?? null,
      pk_columns_json: [],
      created_at: "2026-10-01T09:00:00",
      updated_at: "2026-10-01T09:00:00",
      builtin: false,
    });
    /** 本客户可见的客户声明类型（未钉 customer_id 的种子对任何登录客户可见）。 */
    const ownDomainActionTypes = (): FakeDomainActionSeed[] =>
      [...domainActionTypesStore.values()].filter(
        (seed) => !seed.customer_id || seed.customer_id === customerId,
      );
    const domainActionInstanceOut = (
      seed: FakeDomainActionInstanceSeed,
    ): unknown => ({
      id: seed.id,
      type_id: seed.typeId,
      proposal_id: seed.proposalId ?? null,
      proposer_sub: seed.proposerSub ?? "fleet-agent",
      channel: seed.channel ?? "mcp",
      source_ref: seed.sourceRef ?? null,
      args_json: {},
      status: seed.status ?? "awaiting_confirmation",
      confirmed_by: seed.confirmedBy ?? null,
      correction_id: seed.correctionId ?? null,
      result_ref: seed.resultRef ?? null,
      error: seed.error ?? null,
      created_at: "2026-10-01T09:00:00",
      updated_at: "2026-10-01T09:00:00",
    });
    const failDomainActions = () =>
      Promise.resolve(
        jsonResponse(
          { detail: `domain actions ${options.failDomainActionsStatus}` },
          options.failDomainActionsStatus!,
        ),
      );
    const entityActionsMatch = path.match(/^\/api\/v1\/entities\/(\d+)\/actions$/);
    if (entityActionsMatch && method === "GET") {
      if (options.failDomainActionsStatus) return failDomainActions();
      const entity = ownEntity(Number(entityActionsMatch[1]));
      if (!entity) {
        return Promise.resolve(
          jsonResponse({ detail: "entity not found" }, 404),
        );
      }
      return Promise.resolve(
        jsonResponse({
          actions: ownDomainActionTypes()
            .filter((seed) => seed.entityId === entity.id)
            .map(domainActionTypeOut),
        }),
      );
    }
    if (path === "/api/v1/domain-actions" && method === "GET") {
      if (options.failDomainActionsStatus) return failDomainActions();
      return Promise.resolve(
        jsonResponse({
          actions: [
            ...ownDomainActionTypes().map(domainActionTypeOut),
            ...builtinDomainActionRows(),
          ],
        }),
      );
    }
    if (path === "/api/v1/domain-actions/instances" && method === "GET") {
      if (options.failDomainActionsStatus) return failDomainActions();
      return Promise.resolve(
        jsonResponse({
          instances: [...domainActionInstancesStore.values()]
            .filter(
              (seed) =>
                !seed.customer_id || seed.customer_id === customerId,
            )
            .map(domainActionInstanceOut),
        }),
      );
    }

    // 物化连接（wire-entity-link 3.2）：第七端点——POST /entities/{id}/
    //    relationships/{rel_id}/materialize → 201 DatasetOut（source_kind=
    //    "derived"、storage_tier="cell"、右列撞名 right_ 前缀、行数=左集）；
    //    错误矩阵：404 关系/实体/数据集失存、403 双 cell 档位闸、422 列漂移
    //    逐条指名（FastAPI 数组形状）。
    const entityRelMaterializeMatch = path.match(/^\/api\/v1\/entities\/(\d+)\/relationships\/(\d+)\/materialize$/);
    if (entityRelMaterializeMatch && method === "POST") {
      if (options.failEntityMaterializeStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failEntityMaterializeDetail ??
                `entity materialize ${options.failEntityMaterializeStatus}`,
            },
            options.failEntityMaterializeStatus,
          ),
        );
      }
      const entity = ownEntity(Number(entityRelMaterializeMatch[1]));
      const rel = entityRelationshipsStore.get(
        Number(entityRelMaterializeMatch[2]),
      );
      if (
        !entity ||
        !rel ||
        (rel.customer_id && rel.customer_id !== customerId) ||
        (rel.fromEntityId !== entity.id && rel.toEntityId !== entity.id)
      ) {
        // 关系失存 / 他企 / 不以路径实体为端点：一律 404（不泄露）。
        return Promise.resolve(
          jsonResponse({ detail: "relationship not found" }, 404),
        );
      }
      const fromEntity = ownEntity(rel.fromEntityId);
      const toEntity = ownEntity(rel.toEntityId);
      const leftDataset = fromEntity
        ? datasets.get(fromEntity.datasetId)
        : undefined;
      const rightDataset = toEntity
        ? datasets.get(toEntity.datasetId)
        : undefined;
      if (!fromEntity || !toEntity || !leftDataset || !rightDataset) {
        return Promise.resolve(
          jsonResponse(
            { detail: "relationship dataset not found" },
            404,
          ),
        );
      }
      // 档位双闸：任一端非 cell 档 → 403（PLAN_TIER_REQUIRED 语义如实）。
      if (
        (leftDataset as DatasetWithTier).storage_tier !== "cell" ||
        (rightDataset as DatasetWithTier).storage_tier !== "cell"
      ) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                "物化连接需要两端数据集均为 cell 档（引擎物化存储）——当前套餐档位不足（PLAN_TIER_REQUIRED）",
            },
            403,
          ),
        );
      }
      // 列漂移防线（声明时已校验；此处防窗口）：422 逐条指名。
      const drift: ApiDetailItem[] = [];
      if (!leftDataset.columns.some((c) => c.name === rel.fromColumn)) {
        drift.push({
          type: "value_error",
          loc: ["relationship", "from_column"],
          msg: `源列 ${rel.fromColumn} 不在数据集「${leftDataset.name}」的当前列清单中（声明后列漂移）`,
        });
      }
      if (!rightDataset.columns.some((c) => c.name === rel.toColumn)) {
        drift.push({
          type: "value_error",
          loc: ["relationship", "to_column"],
          msg: `目标列 ${rel.toColumn} 不在数据集「${rightDataset.name}」的当前列清单中（声明后列漂移）`,
        });
      }
      if (drift.length > 0) {
        return Promise.resolve(jsonResponse({ detail: drift }, 422));
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        name?: string;
      };
      const linkName =
        (typeof payload.name === "string" ? payload.name.trim() : "") ||
        `${fromEntity.name}×${toEntity.name} 连接`;
      // 连接列计划（引擎 LEFT JOIN 语义）：左列全保留 + 右列撞名 right_ 前缀。
      const leftNames = new Set(leftDataset.columns.map((c) => c.name));
      const joinedColumns: DatasetColumn[] = [
        ...leftDataset.columns,
        ...rightDataset.columns.map((c) => ({
          name: leftNames.has(c.name) ? `right_${c.name}` : c.name,
          type: c.type,
        })),
      ];
      const nowIso = "2026-10-05T12:00:00";
      const linkId = `ds-${nextDatasetSeq++}`;
      const isBridge = rel.kind === "m:n";
      const linked: Dataset = {
        id: linkId,
        key: `ds_${linkId.replace(/-/g, "_")}`,
        name: linkName,
        source_kind: "derived",
        connector_id: null,
        source_ref: leftDataset.key,
        columns: joinedColumns,
        // m:n 桥展开：行数=展开对数（假后端按可计数口径合成 4 对）
        row_count: isBridge ? 4 : leftDataset.row_count,
        size_bytes: leftDataset.size_bytes + rightDataset.size_bytes,
        sync_cron: null,
        sync_status: "ok",
        paused_reason: null,
        sync_mode: "snapshot",
        watermark_column: null,
        watermark_value: null,
        created_at: nowIso,
        updated_at: nowIso,
        customer_id: customerId,
      };
      // 连接子集必为 cell 档（引擎物化存储）。
      (linked as DatasetWithTier).storage_tier = "cell";
      if (isBridge) {
        // m:n 诚实计数（wire-entity-mn-materialize）：估算=桥行数、实数=
        // 展开对数、孤儿桥=被 INNER 丢弃的失存桥行（合成为 6/4/2）。
        linked.materialization = { estimate_rows: 6, rows: 4, orphan_dropped: 2 };
      }
      datasets.set(linkId, linked);
      return Promise.resolve(jsonResponse(linked, 201));
    }
    // 注意：下方使用局部 const 匹配变量（`match` 在更晚处 let 声明——
    // 提前赋值会触发 TDZ ReferenceError）。
    const entityRelDeleteMatch = path.match(
      /^\/api\/v1\/entities\/(\d+)\/relationships\/(\d+)$/,
    );
    if (entityRelDeleteMatch && method === "DELETE") {
      const entity = ownEntity(Number(entityRelDeleteMatch[1]));
      const rel = entityRelationshipsStore.get(Number(entityRelDeleteMatch[2]));
      if (
        !entity ||
        !rel ||
        (rel.customer_id && rel.customer_id !== customerId) ||
        (rel.fromEntityId !== entity.id && rel.toEntityId !== entity.id)
      ) {
        // rel 不存在 / 他企 / 不以该实体为端点：一律 404。
        return Promise.resolve(
          jsonResponse({ detail: "relationship not found" }, 404),
        );
      }
      entityRelationshipsStore.delete(rel.id);
      return Promise.resolve(jsonResponse({ deleted: true }));
    }
    const entityRelMatch = path.match(
      /^\/api\/v1\/entities\/(\d+)\/relationships$/,
    );
    if (entityRelMatch && method === "POST") {
      if (options.failEntityRelationshipCreateStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failEntityRelationshipCreateDetail ??
                `relationship create ${options.failEntityRelationshipCreateStatus}`,
            },
            options.failEntityRelationshipCreateStatus,
          ),
        );
      }
      const entity = ownEntity(Number(entityRelMatch[1]));
      if (!entity) {
        return Promise.resolve(
          jsonResponse({ detail: "entity not found" }, 404),
        );
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        to_entity_id?: number;
        from_column?: string;
        to_column?: string;
        kind?: string;
        label?: string;
        junction_dataset_id?: string;
        junction_source_column?: string;
        junction_target_column?: string;
      };
      const target =
        payload.to_entity_id != null ? ownEntity(payload.to_entity_id) : undefined;
      if (target == null) {
        return Promise.resolve(
          jsonResponse(
            {
              detail: [
                {
                  type: "value_error",
                  loc: ["body", "to_entity_id"],
                  msg: "目标实体不存在或不属于本企业",
                },
              ],
            },
            422,
          ),
        );
      }
      const sourceDataset = datasets.get(entity.datasetId);
      const relDetail: ApiDetailItem[] = [];
      if (
        !payload.from_column ||
        !sourceDataset?.columns.some((c) => c.name === payload.from_column)
      ) {
        relDetail.push({
          type: "value_error",
          loc: ["body", "from_column"],
          msg: `源列不在实体「${entity.name}」数据集的列清单中`,
        });
      }
      if (!payload.to_column || !target.pkColumns.includes(payload.to_column)) {
        relDetail.push({
          type: "value_error",
          loc: ["body", "to_column"],
          msg: "关系目标必须是目标实体的主键列",
        });
      }
      if (payload.kind === "m:n") {
        // m:n（wire-entity-mn-materialize）：经中间数据集——中间集属主 +
        // 两桥列 ∈ 中间集列快照（值域校验沿列快照存在性口径）。
        const junction = payload.junction_dataset_id
          ? datasets.get(payload.junction_dataset_id)
          : undefined;
        if (
          !payload.junction_dataset_id ||
          !payload.junction_source_column ||
          !payload.junction_target_column
        ) {
          relDetail.push({
            type: "value_error",
            loc: ["body", "junction_dataset_id"],
            msg: "m:n 关系必须指定中间数据集与两列",
          });
        } else if (!junction || junction.customer_id !== customerId) {
          relDetail.push({
            type: "value_error",
            loc: ["body", "junction_dataset_id"],
            msg: "中间数据集不存在",
          });
        } else {
          for (const [field, col] of [
            ["junction_source_column", payload.junction_source_column],
            ["junction_target_column", payload.junction_target_column],
          ] as const) {
            if (!junction.columns.some((c) => c.name === col)) {
              relDetail.push({
                type: "value_error",
                loc: ["body", field],
                msg: `${field} ${col} 不在中间数据集列清单中`,
              });
            }
          }
        }
      } else if (payload.kind !== "n:1" && payload.kind !== "1:1") {
        relDetail.push({
          type: "enum",
          loc: ["body", "kind"],
          msg: "kind 只能是 n:1、1:1 或 m:n",
        });
      }
      if (relDetail.length > 0) {
        return Promise.resolve(jsonResponse({ detail: relDetail }, 422));
      }
      if (
        ownEntityRelationshipRows().some(
          (r) =>
            r.fromEntityId === entity.id &&
            r.fromColumn === payload.from_column &&
            r.toEntityId === target.id &&
            r.toColumn === payload.to_column,
        )
      ) {
        return Promise.resolve(
          jsonResponse(
            { detail: "该关系已存在（源实体/源列/目标实体/目标列四元组唯一）" },
            409,
          ),
        );
      }
      const created: FakeEntityRelationshipRow = {
        id: nextEntityRelationshipId++,
        customer_id: customerId,
        fromEntityId: entity.id,
        fromColumn: payload.from_column!,
        toEntityId: target.id,
        toColumn: payload.to_column!,
        kind: payload.kind as FakeEntityRelationshipRow["kind"],
        label: payload.label?.trim() || null,
        junctionDatasetId:
          payload.kind === "m:n" ? (payload.junction_dataset_id ?? null) : null,
        junctionSourceColumn:
          payload.kind === "m:n" ? (payload.junction_source_column ?? null) : null,
        junctionTargetColumn:
          payload.kind === "m:n" ? (payload.junction_target_column ?? null) : null,
      };
      entityRelationshipsStore.set(created.id, created);
      return Promise.resolve(jsonResponse(relationshipOut(created), 201));
    }
    const entityMatch = path.match(/^\/api\/v1\/entities\/(\d+)$/);
    if (entityMatch) {
      const entity = ownEntity(Number(entityMatch[1]));
      if (!entity) {
        return Promise.resolve(
          jsonResponse({ detail: "entity not found" }, 404),
        );
      }
      if (method === "PUT") {
        const payload = (calls[calls.length - 1]!.body ?? {}) as {
          name?: string;
          pk_columns?: unknown;
          description?: string | null;
        };
        const putDetail: ApiDetailItem[] = [];
        let nextName = entity.name;
        if (payload.name !== undefined) {
          const trimmed =
            typeof payload.name === "string" ? payload.name.trim() : "";
          if (!trimmed) {
            putDetail.push({
              type: "string_too_short",
              loc: ["body", "name"],
              msg: "业务名不能为空（或全为空白字符）",
            });
          } else if (trimmed.length > 100) {
            putDetail.push({
              type: "too_long",
              loc: ["body", "name"],
              msg: "业务名长度不能超过 100 字符",
            });
          } else {
            nextName = trimmed;
          }
        }
        let nextPk = entity.pkColumns;
        if (payload.pk_columns !== undefined) {
          const cols = Array.isArray(payload.pk_columns)
            ? payload.pk_columns.filter(
                (c): c is string => typeof c === "string",
              )
            : [];
          if (cols.length === 0) {
            putDetail.push({
              type: "too_short",
              loc: ["body", "pk_columns"],
              msg: "主键列至少需要一列",
            });
          }
          const dataset = datasets.get(entity.datasetId);
          const unknown = cols.filter(
            (c) => !dataset?.columns.some((col) => col.name === c),
          );
          if (unknown.length > 0) {
            putDetail.push({
              type: "value_error",
              loc: ["body", "pk_columns"],
              msg: `主键列不在数据集列清单中：${unknown.join("、")}`,
            });
          }
          nextPk = cols;
        }
        if (putDetail.length > 0) {
          return Promise.resolve(jsonResponse({ detail: putDetail }, 422));
        }
        if (
          nextName !== entity.name &&
          ownEntityRows().some((row) => row.id !== entity.id && row.name === nextName)
        ) {
          return Promise.resolve(
            jsonResponse(
              { detail: `业务名「${nextName}」已存在——实体名在本企业内唯一` },
              409,
            ),
          );
        }
        entity.name = nextName;
        entity.pkColumns = nextPk;
        if (payload.description !== undefined) {
          entity.description =
            payload.description == null || !String(payload.description).trim()
              ? null
              : String(payload.description).trim();
        }
        return Promise.resolve(jsonResponse(entityOut(entity)));
      }
      if (method === "DELETE") {
        // 级联：先删以该实体为端点的关系（数量如实回带），再删实体。
        const incident = ownEntityRelationshipRows().filter(
          (r) => r.fromEntityId === entity.id || r.toEntityId === entity.id,
        );
        for (const rel of incident) entityRelationshipsStore.delete(rel.id);
        entitiesStore.delete(entity.id);
        return Promise.resolve(
          jsonResponse({
            deleted: true,
            deleted_relationships: incident.length,
          }),
        );
      }
    }

    if (path === "/api/v1/ai/conversations" && method === "POST") {
      if (options.failCreateStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failCreateDetail ??
                `console api ${options.failCreateStatus}`,
            },
            options.failCreateStatus,
          ),
        );
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        title?: string;
        message?: string;
      };
      const id = nextConversationId++;
      const meta: Conversation = {
        id,
        customer_id: customerId,
        title: payload.title ?? "新对话",
        created_at: "2026-10-04T10:00:00",
      };
      store.set(id, { meta, messages: [], artifacts: [] });
      if (payload.message) addMessage(id, "user", payload.message);
      for (const reply of options.createReply ?? chatReply) {
        addMessage(id, reply.role, reply.content);
      }
      const stored = store.get(id)!;
      const detail: ConversationDetail = { ...meta, messages: stored.messages };
      return Promise.resolve(jsonResponse(detail, 201));
    }

    if (path === "/api/v1/ai/conversations" && method === "GET") {
      const mine = [...store.values()]
        .filter((s) => s.meta.customer_id === customerId)
        .map((s) => s.meta)
        .sort((a, b) => b.id - a.id);
      return Promise.resolve(jsonResponse(mine));
    }

    let match = path.match(/^\/api\/v1\/ai\/conversations\/(\d+)\/messages$/);
    if (match) {
      const id = Number(match[1]);
      const stored = store.get(id);
      if (!stored || stored.meta.customer_id !== customerId) {
        return Promise.resolve(jsonResponse({ detail: "conversation not found" }, 404));
      }
      if (method === "GET") {
        return Promise.resolve(jsonResponse(stored.messages));
      }
      if (method === "POST") {
        const payload = calls[calls.length - 1]!.body as { content: string };
        const appended: ChatMessage[] = [addMessage(id, "user", payload.content)];
        for (const reply of chatReply) {
          appended.push(addMessage(id, reply.role, reply.content));
        }
        return Promise.resolve(jsonResponse(appended));
      }
    }

    match = path.match(/^\/api\/v1\/ai\/conversations\/(\d+)\/artifacts$/);
    if (match && method === "GET") {
      const id = Number(match[1]);
      const stored = store.get(id);
      if (!stored || stored.meta.customer_id !== customerId) {
        return Promise.resolve(jsonResponse({ detail: "conversation not found" }, 404));
      }
      return Promise.resolve(jsonResponse(stored.artifacts));
    }

    // ── 产物生命周期（wire-artifacts-persistence）：列表/详情/PUT 版本化/
    //    版本历史/发布/归档/恢复/草稿导入——语义对齐 api 仓 artifacts 路由 ──
    let lifecycleTick = 0;
    const lifecycleNow = () => `2026-10-11T0${8 + (lifecycleTick++ % 4)}:00:00`;

    if (path === "/api/v1/artifacts/from-draft" && method === "POST") {
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        conversation_id?: number;
        draft_id?: number;
      };
      const stored = store.get(payload.conversation_id ?? -1);
      const draft = stored?.artifacts.find((d) => d.id === payload.draft_id);
      if (!stored || stored.meta.customer_id !== customerId || !draft) {
        return Promise.resolve(
          jsonResponse(
            { detail: `conversation ${payload.conversation_id} 无草稿 ${payload.draft_id}` },
            404,
          ),
        );
      }
      const id = nextLifecycleArtifactId++;
      const created: StoredLifecycleArtifact = {
        id,
        customer_id: customerId,
        kind: draft.kind,
        title: String(draft.spec.title ?? draft.kind),
        status: "draft",
        version: 1,
        origin_conversation_id: stored.meta.id,
        status_history: [
          {
            status: "draft",
            at: lifecycleNow(),
            by: `console:${customerId}`,
            note: `从会话 ${stored.meta.id} 草稿导入`,
          },
        ],
        created_at: lifecycleNow(),
        updated_at: lifecycleNow(),
        versions: [
          { version: 1, spec: draft.spec, operator: `console:${customerId}`, created_at: lifecycleNow() },
        ],
      };
      lifecycleArtifacts.set(id, created);
      return Promise.resolve(jsonResponse(lifecycleDetail(created), 201));
    }

    // ── 空白建板（wire-dashboard-blank-create 1.2）/ 空白建表单
    //    （wire-form-builder 1.2）/ 空白建告警规则（wire-alert-evaluation）/
    //    空白建文档（wire-rich-text-docs 任务 6.2）：
    //    POST /api/v1/artifacts {kind, title} → 201 ArtifactDetail（draft v1）。
    //    kind 白名单 (dashboard, form, alert_rule, doc)——dashboard spec = 最小
    //    空板 {title, spec:{title, panels:[]}}；form spec = 最小空表单；
    //    alert_rule spec = 规则模板壳（占位 dataset_id；信封形状逐端点对齐
    //    生产）；doc spec = 空正文最小文档 {title, spec:{format:"markdown",
    //    text:""}}（任务 6.2 只加 doc，不改 record-view 既有行为——它本不在
    //    此 mock 白名单内）；title 非空，非法 422 带 detail，语义对齐 api 仓。 ──
    if (path === "/api/v1/artifacts" && method === "POST") {
      if (options.failArtifactCreateStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failArtifactCreateDetail ??
                `artifact create ${options.failArtifactCreateStatus}`,
            },
            options.failArtifactCreateStatus,
          ),
        );
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        kind?: string;
        title?: string;
      };
      if (
        payload.kind !== "dashboard" &&
        payload.kind !== "form" &&
        payload.kind !== "alert_rule" &&
        payload.kind !== "doc"
      ) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                `kind 白名单仅支持 dashboard/form/alert_rule/doc（当前 ${String(payload.kind)}）` +
                "——其他类型仍由 AI 草稿转正产生",
            },
            422,
          ),
        );
      }
      const title = typeof payload.title === "string" ? payload.title.trim() : "";
      if (!title) {
        return Promise.resolve(
          jsonResponse({ detail: "title 不能为空（或全为空白字符）" }, 422),
        );
      }
      const id = nextLifecycleArtifactId++;
      const kind = payload.kind;
      const blankSpec =
        kind === "dashboard"
          ? { title, spec: { title, panels: [] } }
          : kind === "form"
            ? { title, spec: { title, datasetId: null, fields: [] } }
            : kind === "doc"
              ? { title, spec: { format: "markdown", text: "" } }
              : {
                  title,
                  spec: {
                    query: { dataset_id: "<数据集id>", columns: ["<列名>"] },
                    aggregate: { func: "count" },
                    condition: { op: "gt", value: 0 },
                    intervalMinutes: 30,
                  },
                };
      const created: StoredLifecycleArtifact = {
        id,
        customer_id: customerId,
        kind,
        title,
        status: "draft",
        version: 1,
        origin_conversation_id: null,
        status_history: [
          {
            status: "draft",
            at: lifecycleNow(),
            by: `console:${customerId}`,
            note: "空白新建",
          },
        ],
        created_at: lifecycleNow(),
        updated_at: lifecycleNow(),
        versions: [
          { version: 1, spec: blankSpec, operator: `console:${customerId}`, created_at: lifecycleNow() },
        ],
      };
      lifecycleArtifacts.set(id, created);
      return Promise.resolve(jsonResponse(lifecycleDetail(created), 201));
    }

    if (path === "/api/v1/artifacts" && method === "GET") {
      const kind = url.searchParams.get("kind");
      const status = url.searchParams.get("status");
      const mine = [...lifecycleArtifacts.values()]
        .filter((a) => !a.customer_id || a.customer_id === customerId)
        .filter((a) => !kind || a.kind === kind)
        .filter((a) => !status || a.status === status)
        .sort((a, b) => (a.updated_at < b.updated_at ? 1 : a.updated_at > b.updated_at ? -1 : b.id - a.id));
      return Promise.resolve(
        jsonResponse(
          mine.map(({ versions: _v, status_history: _h, ...summary }) => summary),
        ),
      );
    }

    match = path.match(/^\/api\/v1\/artifacts\/(\d+)$/);
    if (match) {
      const artifact = ownLifecycleArtifact(Number(match[1]), customerId);
      if (!artifact) {
        return Promise.resolve(jsonResponse({ detail: "artifact not found" }, 404));
      }
      if (method === "GET") {
        return Promise.resolve(jsonResponse(lifecycleDetail(artifact)));
      }
      if (method === "PUT") {
        if (artifact.status === "archived") {
          return Promise.resolve(
            jsonResponse(
              {
                detail:
                  `产物 ${artifact.id} 已归档（archived），不可编辑——请先恢复（restore）为草稿再修改。`,
              },
              409,
            ),
          );
        }
        const payload = (calls[calls.length - 1]!.body ?? {}) as { spec?: unknown };
        const spec = payload.spec as Record<string, unknown> | undefined;
        const title = spec?.title;
        const inner = spec?.spec;
        if (!spec || typeof title !== "string" || !title.trim()) {
          return Promise.resolve(
            jsonResponse({ detail: "spec 缺少非空 title" }, 422),
          );
        }
        if (typeof inner !== "object" || inner === null || Object.keys(inner).length === 0) {
          return Promise.resolve(
            jsonResponse({ detail: "spec.spec 必须是非空 JSON 对象" }, 422),
          );
        }
        artifact.version += 1;
        artifact.versions.push({
          version: artifact.version,
          spec,
          operator: `console:${customerId}`,
          created_at: lifecycleNow(),
        });
        artifact.title = title.trim();
        artifact.updated_at = lifecycleNow();
        return Promise.resolve(jsonResponse(lifecycleDetail(artifact)));
      }
    }

    // ── doc 草稿层（wire-rich-text-docs 任务 2.2/6.2）：GET/PUT
    //    /api/v1/artifacts/{id}/draft——仅 doc kind（他 kind 422）；PUT 覆盖写
    //    一行绝不进版本链（design D3）；archived 拒写 409；越权/不存在 404
    //    （不泄露存在性）；超限 422 载明上限且零半写。──
    match = path.match(/^\/api\/v1\/artifacts\/(\d+)\/draft$/);
    if (match) {
      const artifact = ownLifecycleArtifact(Number(match[1]), customerId);
      if (!artifact) {
        return Promise.resolve(jsonResponse({ detail: "artifact not found" }, 404));
      }
      if (artifact.kind !== "doc") {
        return Promise.resolve(
          jsonResponse(
            {
              detail: `产物 ${artifact.id} 是 ${artifact.kind}，仅文档（doc）有草稿层。`,
            },
            422,
          ),
        );
      }
      if (method === "GET") {
        const row = draftStore.get(artifact.id);
        return Promise.resolve(
          jsonResponse(
            row
              ? { text: row.text, updated_at: row.updated_at }
              : { text: null, updated_at: null },
          ),
        );
      }
      if (method === "PUT") {
        if (artifact.status === "archived") {
          return Promise.resolve(
            jsonResponse(
              {
                detail:
                  `产物 ${artifact.id} 已归档（archived），不可编辑——请先恢复（restore）为草稿再修改。`,
              },
              409,
            ),
          );
        }
        const payload = (calls[calls.length - 1]!.body ?? {}) as { text?: unknown };
        if (typeof payload.text !== "string") {
          return Promise.resolve(
            jsonResponse(
              { detail: "spec.spec.text 必须是字符串（Markdown 源文本）" },
              422,
            ),
          );
        }
        const bytes = new TextEncoder().encode(payload.text).length;
        if (bytes > DRAFT_MAX_BYTES) {
          return Promise.resolve(
            jsonResponse(
              {
                detail:
                  `正文超过上限 ${DRAFT_MAX_BYTES} 字节（Markdown 源文本按 UTF-8 计）`,
              },
              422,
            ),
          );
        }
        const updatedAt = lifecycleNow();
        draftStore.set(artifact.id, { text: payload.text, updated_at: updatedAt });
        return Promise.resolve(
          jsonResponse({ text: payload.text, updated_at: updatedAt }),
        );
      }
    }

    match = path.match(/^\/api\/v1\/artifacts\/(\d+)\/versions$/);
    if (match && method === "GET") {
      const artifact = ownLifecycleArtifact(Number(match[1]), customerId);
      if (!artifact) {
        return Promise.resolve(jsonResponse({ detail: "artifact not found" }, 404));
      }
      const rows = [...artifact.versions]
        .sort((a, b) => b.version - a.version)
        .map(({ spec: _s, ...summary }) => summary);
      return Promise.resolve(jsonResponse(rows));
    }

    match = path.match(/^\/api\/v1\/artifacts\/(\d+)\/versions\/(\d+)$/);
    if (match && method === "GET") {
      const artifact = ownLifecycleArtifact(Number(match[1]), customerId);
      const row = artifact?.versions.find((v) => v.version === Number(match![2]));
      if (!artifact || !row) {
        return Promise.resolve(jsonResponse({ detail: "version not found" }, 404));
      }
      return Promise.resolve(jsonResponse(row));
    }

    match = path.match(/^\/api\/v1\/artifacts\/(\d+)\/(publish|archive|restore)$/);
    if (match && method === "POST") {
      const artifact = ownLifecycleArtifact(Number(match[1]), customerId);
      if (!artifact) {
        return Promise.resolve(jsonResponse({ detail: "artifact not found" }, 404));
      }
      const action = match[2] as "publish" | "archive" | "restore";
      const transitions: Record<string, Partial<Record<typeof action, ArtifactStatus>>> = {
        draft: { publish: "published", archive: "archived" },
        published: { archive: "archived" },
        archived: { restore: "draft" },
      };
      const target = transitions[artifact.status]?.[action];
      if (!target) {
        const hints: Record<string, string> = {
          publish:
            "合法路径：draft --publish--> published（已发布或已归档的产物请先恢复为草稿）",
          archive: "合法路径：draft/published --archive--> archived",
          restore: "合法路径：archived --restore--> draft（仅归档产物可恢复）",
        };
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                `产物 ${artifact.id} 当前状态 ${artifact.status} 不允许 ${action}——` +
                hints[action],
            },
            409,
          ),
        );
      }
      artifact.status = target;
      artifact.updated_at = lifecycleNow();
      artifact.status_history.push({
        status: target,
        at: lifecycleNow(),
        by: `console:${customerId}`,
        note: `${action} → ${target}`,
      });
      return Promise.resolve(jsonResponse(lifecycleDetail(artifact)));
    }

    // ── 告警执行（wire-alert-evaluation 任务 5）：states map / 事件列表
    //    （id 倒序 + artifact_id/limit 过滤）/ 试运行（可配置返回 + 可注失败，
    //    零副作用）——语义对齐 api 仓 alerts 路由（Bearer 客户隔离；states
    //    仅放行同客户可见的 alert_rule 产物，非 alert_rule 产物 test 404）。──
    if (path === "/api/v1/alerts/states" && method === "GET") {
      if (options.failAlertStatesStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `alert states ${options.failAlertStatesStatus}` },
            options.failAlertStatesStatus,
          ),
        );
      }
      const states: Record<string, unknown> = {};
      for (const [key, seed] of alertStatesStore) {
        const artifact = ownLifecycleArtifact(Number(key), customerId);
        if (!artifact || artifact.kind !== "alert_rule") continue;
        if (seed.customer_id && seed.customer_id !== customerId) continue;
        states[key] = {
          state: seed.state,
          last_value: seed.last_value ?? null,
          last_evaluated_at: seed.last_evaluated_at ?? null,
          last_error_at: seed.last_error_at ?? null,
        };
      }
      return Promise.resolve(jsonResponse({ states }));
    }

    if (path === "/api/v1/alerts/events" && method === "GET") {
      if (options.failAlertEventsStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `alert events ${options.failAlertEventsStatus}` },
            options.failAlertEventsStatus,
          ),
        );
      }
      const artifactId = url.searchParams.get("artifact_id");
      const rawLimit = Number(url.searchParams.get("limit") ?? "50");
      const limit = Number.isFinite(rawLimit) && rawLimit > 0 ? rawLimit : 50;
      const mine = [...alertEventsStore.values()]
        .filter((e) => !e.customer_id || e.customer_id === customerId)
        .filter(
          (e) =>
            artifactId == null || artifactId === "" ||
            String(e.artifact_id) === artifactId,
        )
        .sort((a, b) => b.id - a.id)
        .slice(0, limit);
      return Promise.resolve(
        jsonResponse({
          events: mine.map((e) => ({
            id: e.id,
            artifact_id: e.artifact_id,
            kind: e.kind,
            value: e.value ?? null,
            threshold: e.threshold ?? null,
            detail: e.detail ?? null,
            created_at: e.created_at ?? "2026-10-12T08:00:00",
          })),
        }),
      );
    }

    match = path.match(/^\/api\/v1\/artifacts\/(\d+)\/alert\/test$/);
    if (match && method === "POST") {
      if (options.failAlertTestStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failAlertTestDetail ??
                `alert test ${options.failAlertTestStatus}`,
            },
            options.failAlertTestStatus,
          ),
        );
      }
      const artifact = ownLifecycleArtifact(Number(match[1]), customerId);
      if (!artifact || artifact.kind !== "alert_rule") {
        return Promise.resolve(jsonResponse({ detail: "artifact not found" }, 404));
      }
      const result = options.alertTest ?? { value: 1, would_fire: false };
      return Promise.resolve(jsonResponse(result));
    }

    // ── 我的数据：数据集列表 / 建档 / 详情 / 预览 ──
    if (path === "/api/v1/datasets" && method === "GET") {
      const mine = [...datasets.values()].filter(
        (d) => !d.customer_id || d.customer_id === customerId,
      );
      return Promise.resolve(jsonResponse(mine));
    }

    if (path === "/api/v1/datasets" && method === "POST") {
      if (options.failCreateDatasetStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failCreateDatasetDetail ??
                `create dataset ${options.failCreateDatasetStatus}`,
            },
            options.failCreateDatasetStatus,
          ),
        );
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        name?: string;
        source?: {
          kind?: string;
          upload_id?: string;
          connector_id?: string;
          source_name?: string;
          source_ref?: string;
          schedule_cron?: string | null;
        };
      };
      const source = payload.source ?? {};
      const upload = source.upload_id ? uploads.get(source.upload_id) : undefined;
      const nowIso = "2026-10-05T12:00:00";
      const id = `ds-${nextDatasetSeq++}`;
      const free = (options.planCode ?? "basic") === "free";
      const dataset: Dataset = {
        id,
        key: `ds_${id.replace(/-/g, "_")}`,
        name: payload.name ?? "未命名",
        source_kind: source.kind === "connector" ? "connector" : "upload",
        connector_id: source.connector_id ?? null,
        source_name: source.source_name,
        source_ref: source.source_ref ?? null,
        columns: upload?.columns ?? [],
        row_count: upload?.row_count ?? 0,
        size_bytes: upload ? upload.row_count * 24 : 0,
        sync_cron: source.schedule_cron ?? null,
        sync_status: "ok",
        paused_reason: null,
        // wire-incremental-sync：新建集缺省全量快照（三字段随详情响应下发）。
        sync_mode: "snapshot",
        watermark_column: null,
        watermark_value: null,
        created_at: nowIso,
        updated_at: nowIso,
        customer_id: customerId,
      };
      // 免费档建集落 api 层（wire-free-tier-data-gate D1），响应带 storage_tier
      // 供列表徽标（组件容错读取）；付费档不带——现状响应零变化。
      if (free) (dataset as DatasetWithTier).storage_tier = "api";
      datasets.set(id, dataset);
      return Promise.resolve(jsonResponse(dataset, 201));
    }

    // 免费档配额端点（wire-free-tier-data-gate 4.1）：注意必须先于下方
    // /api/v1/datasets/:id 正则命中（否则 "quota" 会被当成数据集 id）。
    if (path === "/api/v1/datasets/quota" && method === "GET") {
      if (options.failQuotaStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `dataset quota ${options.failQuotaStatus}` },
            options.failQuotaStatus,
          ),
        );
      }
      const used = [...datasets.values()].filter(
        (d) => !d.customer_id || d.customer_id === customerId,
      ).length;
      return Promise.resolve(
        jsonResponse(
          options.datasetQuota ?? {
            plan_code: options.planCode ?? "basic",
            used_datasets: used,
            max_datasets: 3,
            max_rows_per_dataset: 10_000,
          },
        ),
      );
    }

    // ── 数据开放与市场（wire-dataset-marketplace）：可见性四档端点（主端点
    //    PUT + 一键 publish/unpublish 薄封装）、定向授权增删查、市场浏览
    //    （SQL 层只放行 market 档的镜像：列表在内存层就只查 market 行）、
    //    订阅/退订、准入申请（approval 模式）——语义对齐 api 仓 assets/market
    //    切片（Bearer 身份；viewer 403 写操作；敏感档 market/directed 422）。──
    /** 敏感度（资产元数据段；缺行 = 常规）。 */
    const sensitivityOf = (datasetId: string): string =>
      assetMetaStore.get(datasetId)?.sensitivity ?? "常规";
    /** 可见性设置响应体（契约：{visibility, access_mode, published_at,
     *  published_version}）。 */
    const visibilityOut = (datasetId: string): Record<string, unknown> => {
      const row = visibilityOf(datasetId);
      return {
        visibility: row.visibility,
        access_mode: row.access_mode,
        published_at: row.published_at,
        published_version: row.published_version,
      };
    };
    /** 市场卡片（列表项）：元数据 + 体量/时间 + 订阅态；绝无数据值。 */
    const marketCard = (dataset: Dataset): Record<string, unknown> => {
      const meta = assetMetaStore.get(dataset.id);
      const vis = visibilityOf(dataset.id);
      return {
        id: dataset.id,
        name: dataset.name,
        source_kind: dataset.source_kind,
        storage_tier: (dataset as DatasetWithTier).storage_tier ?? null,
        row_count: dataset.row_count,
        updated_at: dataset.updated_at,
        description: meta?.description ?? "",
        domain: meta?.domain ?? "",
        tags: meta?.tags ? [...meta.tags] : [],
        cadence_hint: meta?.cadence_hint ?? null,
        owner_note: meta?.owner_note ?? "",
        sensitivity: meta?.sensitivity ?? "常规",
        visibility: vis.visibility,
        access_mode: vis.access_mode,
        published_at: vis.published_at,
        published_version: vis.published_version,
        subscribed: (marketSubscriptions.get(customerId) ?? []).includes(
          dataset.id,
        ),
        access_source: (marketSubscriptions.get(customerId) ?? []).includes(
          dataset.id,
        )
          ? "market_subscription"
          : (grantsStore.get(dataset.id) ?? []).includes(customerId)
            ? "directed_grant"
            : null,
      };
    };
    /** 数据集级访问授权（wire-dataset-acl D5）：owner-only；GET/PUT 同形状
     *  {mode, grants:[{logto_sub, permission}]}；PUT 整表替换、mode=team 清空
     *  grants、sub 须 ∈ 本队成员 ∪ owner（否则 400 如实）。 */
    const aclMatch = path.match(/^\/api\/v1\/datasets\/([^/]+)\/access$/);
    if (aclMatch) {
      const dataset = ownDataset(aclMatch[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      if (ownTeamRole() !== "owner") {
        return Promise.resolve(
          jsonResponse({ detail: "团队成员权限不足（owner-only）" }, 403),
        );
      }
      if (method === "GET") {
        return Promise.resolve(jsonResponse(aclOf(dataset.id)));
      }
      if (method === "PUT") {
        if (options.failDatasetAclPutStatus) {
          return Promise.resolve(
            jsonResponse(
              {
                detail:
                  options.failDatasetAclPutDetail ??
                  `dataset acl ${options.failDatasetAclPutStatus}`,
              },
              options.failDatasetAclPutStatus,
            ),
          );
        }
        const payload = (calls[calls.length - 1]!.body ?? {}) as {
          mode?: unknown;
          grants?: unknown;
        };
        if (payload.mode !== "team" && payload.mode !== "restricted") {
          return Promise.resolve(
            jsonResponse({ detail: "mode 只能是 team / restricted" }, 422),
          );
        }
        const rawGrants = Array.isArray(payload.grants) ? payload.grants : [];
        const grants: Array<{
          logto_sub: string;
          permission: "view" | "edit";
        }> = [];
        for (const entry of rawGrants) {
          const row = (entry ?? {}) as Record<string, unknown>;
          if (
            typeof row.logto_sub !== "string" ||
            row.logto_sub === "" ||
            row.permission !== "view" &&
            row.permission !== "edit"
          ) {
            return Promise.resolve(
              jsonResponse(
                { detail: "授权行须为 {logto_sub, permission(view|edit)}" },
                422,
              ),
            );
          }
          grants.push({
            logto_sub: row.logto_sub,
            permission: row.permission,
          });
        }
        if (payload.mode === "team") {
          grants.length = 0;
        } else {
          const subs = grants.map((grant) => grant.logto_sub);
          const duplicates = [...new Set(subs.filter((s) => subs.filter((x) => x === s).length > 1))];
          if (duplicates.length > 0) {
            return Promise.resolve(
              jsonResponse(
                { detail: `授权名单中 logto_sub 重复：${duplicates.join("、")}` },
                400,
              ),
            );
          }
          const allowed = new Set([
            customerId,
            ...teamMemberRows().map((row) => row.logtoSub),
          ]);
          const unknown = subs.filter((sub) => !allowed.has(sub));
          if (unknown.length > 0) {
            return Promise.resolve(
              jsonResponse(
                {
                  detail: `授权名单包含 ${unknown.length} 个不属于本企业的成员：${unknown.join("、")}`,
                },
                400,
              ),
            );
          }
        }
        aclModeStore.set(dataset.id, payload.mode);
        aclGrantsStore.set(
          dataset.id,
          [...grants].sort((a, b) => a.logto_sub.localeCompare(b.logto_sub)),
        );
        dataset.acl_mode = payload.mode;
        return Promise.resolve(jsonResponse(aclOf(dataset.id)));
      }
    }
    /** viewer 写拒绝（可见性设置为 owner/editor 面）。 */
    const visibilityWriteMatch = path.match(
      /^\/api\/v1\/datasets\/([^/]+)\/visibility$/,
    );
    if (visibilityWriteMatch && method === "GET") {
      const dataset = datasets.get(visibilityWriteMatch[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      return Promise.resolve(jsonResponse(visibilityOut(dataset.id)));
    }
    if (visibilityWriteMatch && method === "PUT") {
      const dataset = datasets.get(visibilityWriteMatch[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      if (options.failVisibilityPutStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failVisibilityPutDetail ??
                `visibility ${options.failVisibilityPutStatus}`,
            },
            options.failVisibilityPutStatus,
          ),
        );
      }
      if (teamMembersStore.get(customerId)?.role === "viewer") {
        return Promise.resolve(
          jsonResponse({ detail: "团队成员权限不足（viewer 只读）" }, 403),
        );
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        visibility?: string;
        access_mode?: string;
        grants?: unknown;
      };
      // 敏感档硬闸：敏感度=敏感 只允许 private/link（对齐 api 侧 422）。
      if (
        sensitivityOf(dataset.id) === "敏感" &&
        (payload.visibility === "market" || payload.visibility === "directed")
      ) {
        return Promise.resolve(
          jsonResponse(
            { detail: "敏感度「敏感」的数据集只允许 private 或 link 开放档" },
            422,
          ),
        );
      }
      if (
        payload.visibility !== "private" &&
        payload.visibility !== "link" &&
        payload.visibility !== "directed" &&
        payload.visibility !== "market"
      ) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                "visibility 只能是 private / link / directed / market",
            },
            422,
          ),
        );
      }
      const row = visibilityOf(dataset.id);
      row.visibility = payload.visibility;
      if (payload.access_mode !== undefined) {
        if (
          payload.access_mode !== "open" &&
          payload.access_mode !== "approval"
        ) {
          return Promise.resolve(
            jsonResponse({ detail: "access_mode 只能是 open / approval" }, 422),
          );
        }
        row.access_mode = payload.access_mode;
      }
      if (payload.visibility === "market" && row.published_at == null) {
        row.published_at = "2026-10-13T09:00:00";
        row.published_version = "1";
      }
      if (payload.visibility === "private") {
        row.published_at = null;
        row.published_version = null;
      }
      // grants 仅 directed 档生效：全量替换（其他档忽略）。
      if (
        payload.visibility === "directed" &&
        Array.isArray(payload.grants)
      ) {
        grantsStore.set(
          dataset.id,
          payload.grants.filter((g): g is string => typeof g === "string"),
        );
      }
      return Promise.resolve(jsonResponse(visibilityOut(dataset.id)));
    }
    const publishMatch = path.match(
      /^\/api\/v1\/datasets\/([^/]+)\/market\/(publish|unpublish)$/,
    );
    if (publishMatch && method === "POST") {
      const dataset = datasets.get(publishMatch[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      if (teamMembersStore.get(customerId)?.role === "viewer") {
        return Promise.resolve(
          jsonResponse({ detail: "团队成员权限不足（viewer 只读）" }, 403),
        );
      }
      if (publishMatch[2] === "publish" && sensitivityOf(dataset.id) === "敏感") {
        return Promise.resolve(
          jsonResponse(
            { detail: "敏感度「敏感」的数据集只允许 private 或 link 开放档" },
            422,
          ),
        );
      }
      const row = visibilityOf(dataset.id);
      if (publishMatch[2] === "publish") {
        row.visibility = "market";
        if (row.published_at == null) {
          row.published_at = "2026-10-13T09:00:00";
          row.published_version = "1";
        }
      } else {
        row.visibility = "private";
        row.published_at = null;
        row.published_version = null;
      }
      return Promise.resolve(jsonResponse(visibilityOut(dataset.id)));
    }
    const grantsMatch = path.match(/^\/api\/v1\/datasets\/([^/]+)\/grants$/);
    if (grantsMatch) {
      const datasetId = grantsMatch[1];
      const dataset = datasets.get(datasetId);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      if (method === "GET") {
        return Promise.resolve(
          jsonResponse(
            (grantsStore.get(datasetId) ?? []).map((cid) => ({
              customer_id: cid,
              granted_at: "2026-10-13T09:00:00",
            })),
          ),
        );
      }
      if (method === "POST") {
        if (teamMembersStore.get(customerId)?.role === "viewer") {
          return Promise.resolve(
            jsonResponse({ detail: "团队成员权限不足（viewer 只读）" }, 403),
          );
        }
        const payload = (calls[calls.length - 1]!.body ?? {}) as {
          customer_id?: string;
        };
        if (!payload.customer_id) {
          return Promise.resolve(
            jsonResponse({ detail: "customer_id 不能为空" }, 422),
          );
        }
        const list = grantsStore.get(datasetId) ?? [];
        if (!list.includes(payload.customer_id)) list.push(payload.customer_id);
        grantsStore.set(datasetId, list);
        return Promise.resolve(jsonResponse({ granted: true }, 201));
      }
    }
    const grantDeleteMatch = path.match(
      /^\/api\/v1\/datasets\/([^/]+)\/grants\/([^/]+)$/,
    );
    if (grantDeleteMatch && method === "DELETE") {
      if (teamMembersStore.get(customerId)?.role === "viewer") {
        return Promise.resolve(
          jsonResponse({ detail: "团队成员权限不足（viewer 只读）" }, 403),
        );
      }
      const [, datasetId, target] = grantDeleteMatch;
      const list = (grantsStore.get(datasetId) ?? []).filter(
        (cid) => cid !== decodeURIComponent(target),
      );
      grantsStore.set(datasetId, list);
      return Promise.resolve(jsonResponse({ deleted: true }));
    }
    // 发布方查看准入申请（pending 在前，其余按 id 倒序）。
    const accessListMatch = path.match(
      /^\/api\/v1\/datasets\/([^/]+)\/access-requests$/,
    );
    if (accessListMatch && method === "GET") {
      if (options.failAccessRequestsListStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `access requests ${options.failAccessRequestsListStatus}` },
            options.failAccessRequestsListStatus,
          ),
        );
      }
      const rows = [...accessRequestsStore.values()]
        .filter((r) => r.dataset_id === accessListMatch[1])
        .sort((a, b) => {
          if ((a.status === "pending") !== (b.status === "pending")) {
            return a.status === "pending" ? -1 : 1;
          }
          return b.id - a.id;
        })
        .map((row) => ({ ...row }));
      return Promise.resolve(jsonResponse(rows));
    }
    const accessDecideMatch = path.match(
      /^\/api\/v1\/datasets\/([^/]+)\/access-requests\/(\d+)\/(approve|reject)$/,
    );
    if (accessDecideMatch && method === "POST") {
      if (options.failAccessDecideStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `access decide ${options.failAccessDecideStatus}` },
            options.failAccessDecideStatus,
          ),
        );
      }
      if (teamMembersStore.get(customerId)?.role === "viewer") {
        return Promise.resolve(
          jsonResponse({ detail: "团队成员权限不足（viewer 只读）" }, 403),
        );
      }
      const row = accessRequestsStore.get(Number(accessDecideMatch[2]));
      if (!row || row.dataset_id !== accessDecideMatch[1]) {
        return Promise.resolve(
          jsonResponse({ detail: "access request not found" }, 404),
        );
      }
      if (row.status !== "pending") {
        return Promise.resolve(
          jsonResponse({ detail: "该申请已裁决（仅 pending 可批/拒）" }, 409),
        );
      }
      row.status =
        accessDecideMatch[3] === "approve"
          ? ("approved" as const)
          : ("rejected" as const);
      row.decided_at = "2026-10-13T10:00:00";
      return Promise.resolve(jsonResponse({ ...row }));
    }
    // ── 市场浏览与订阅（登录面；列表内存层只放行 visibility='market'——
    //    私有档的行根本不进结果集，与 api 侧 SQL 谓词同语义）。──
    if (path === "/api/v1/market/datasets" && method === "GET") {
      if (options.failMarketListStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `market ${options.failMarketListStatus}` },
            options.failMarketListStatus,
          ),
        );
      }
      const q = (url.searchParams.get("q") ?? "").trim().toLowerCase();
      const domain = url.searchParams.get("domain") ?? "";
      const tag = url.searchParams.get("tag") ?? "";
      const limit = Number(url.searchParams.get("limit") ?? "20") || 20;
      const offset = Number(url.searchParams.get("offset") ?? "0") || 0;
      let rows = [...datasets.values()].filter(
        (d) => visibilityOf(d.id).visibility === "market",
      );
      if (domain) {
        rows = rows.filter(
          (d) => (assetMetaStore.get(d.id)?.domain ?? "") === domain,
        );
      }
      if (tag) {
        rows = rows.filter((d) =>
          (assetMetaStore.get(d.id)?.tags ?? []).includes(tag),
        );
      }
      if (q) {
        rows = rows.filter((d) => {
          const meta = assetMetaStore.get(d.id);
          return (
            d.name.toLowerCase().includes(q) ||
            (meta?.description ?? "").toLowerCase().includes(q) ||
            (meta?.tags ?? []).some((t) => t.toLowerCase().includes(q))
          );
        });
      }
      rows.sort((a, b) =>
        (visibilityOf(b.id).published_at ?? "").localeCompare(
          visibilityOf(a.id).published_at ?? "",
        ),
      );
      const total = rows.length;
      return Promise.resolve(
        jsonResponse({
          items: rows.slice(offset, offset + limit).map(marketCard),
          total,
        }),
      );
    }
    const marketSubMatch = path.match(
      /^\/api\/v1\/market\/datasets\/([^/]+)\/(subscribe|unsubscribe)$/,
    );
    if (marketSubMatch) {
      if (options.failMarketSubscribeStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `market subscribe ${options.failMarketSubscribeStatus}` },
            options.failMarketSubscribeStatus,
          ),
        );
      }
      const datasetId = decodeURIComponent(marketSubMatch[1]);
      const dataset = datasets.get(datasetId);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      const subscribing = marketSubMatch[2] === "subscribe";
      if (subscribing) {
        // open 模式订阅即得；approval 模式转为在途申请（重复复用原行）。
        if (visibilityOf(datasetId).access_mode === "approval") {
          if (options.failAccessRequestStatus) {
            return Promise.resolve(
              jsonResponse(
                {
                  detail: `access request ${options.failAccessRequestStatus}`,
                },
                options.failAccessRequestStatus,
              ),
            );
          }
          const existing = [...accessRequestsStore.values()].find(
            (r) =>
              r.dataset_id === datasetId &&
              r.requester_customer_id === customerId,
          );
          if (existing && existing.status === "pending") {
            return Promise.resolve(
              jsonResponse(
                { detail: "已有一条在途申请（等待发布方处理）" },
                409,
              ),
            );
          }
          const id = nextAccessRequestId++;
          const created = {
            id,
            dataset_id: datasetId,
            requester_customer_id: customerId,
            status: "pending" as const,
            created_at: "2026-10-13T09:30:00",
            decided_at: null,
          };
          accessRequestsStore.set(id, created);
          return Promise.resolve(jsonResponse({ ...created }, 201));
        }
        const mine = marketSubscriptions.get(customerId) ?? [];
        if (!mine.includes(datasetId)) mine.push(datasetId);
        marketSubscriptions.set(customerId, mine);
      } else {
        const mine = (marketSubscriptions.get(customerId) ?? []).filter(
          (id) => id !== datasetId,
        );
        marketSubscriptions.set(customerId, mine);
      }
      return Promise.resolve(
        jsonResponse({ dataset_id: datasetId, subscribed: subscribing }),
      );
    }
    // 显式准入申请（approval 模式；与订阅转换同状态机）。
    const accessCreateMatch = path.match(
      /^\/api\/v1\/market\/datasets\/([^/]+)\/access-requests$/,
    );
    if (accessCreateMatch && method === "POST") {
      if (options.failAccessRequestStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `access request ${options.failAccessRequestStatus}` },
            options.failAccessRequestStatus,
          ),
        );
      }
      const datasetId = decodeURIComponent(accessCreateMatch[1]);
      if (!datasets.has(datasetId)) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      const existing = [...accessRequestsStore.values()].find(
        (r) =>
          r.dataset_id === datasetId && r.requester_customer_id === customerId,
      );
      if (existing && existing.status === "pending") {
        return Promise.resolve(
          jsonResponse({ detail: "已有一条在途申请（等待发布方处理）" }, 409),
        );
      }
      const id = nextAccessRequestId++;
      const created = {
        id,
        dataset_id: datasetId,
        requester_customer_id: customerId,
        status: "pending" as const,
        created_at: "2026-10-13T09:30:00",
        decided_at: null,
      };
      accessRequestsStore.set(id, created);
      return Promise.resolve(jsonResponse({ ...created }, 201));
    }
    const marketDetailMatch = path.match(/^\/api\/v1\/market\/datasets\/([^/]+)$/);
    if (marketDetailMatch && method === "GET") {
      const datasetId = decodeURIComponent(marketDetailMatch[1]);
      const dataset = datasets.get(datasetId);
      // 详情同样只放行 market 档（directed 档按 grant 放行——定向对象
      // 拿到的链接面；private/link 档一律 404 不泄露）。
      const vis = visibilityOf(datasetId);
      const granted =
        vis.visibility === "directed" &&
        (grantsStore.get(datasetId) ?? []).includes(customerId);
      if (!dataset || (vis.visibility !== "market" && !granted)) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      return Promise.resolve(
        jsonResponse({
          ...marketCard(dataset),
          columns: dataset.columns.map((c) => ({ name: c.name, type: c.type })),
          column_count: dataset.columns.length,
        }),
      );
    }

    if (path === "/api/v1/datasets/uploads" && method === "POST") {
      if (options.failUploadStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: options.failUploadDetail ?? `upload failed ${options.failUploadStatus}` },
            options.failUploadStatus,
          ),
        );
      }
      const call = calls[calls.length - 1]!;
      const file = call.formData?.get("file");
      const filename =
        typeof file === "string" ? file : file?.name ?? "upload.csv";
      const preview: UploadPreview = {
        upload_id: `up-${nextUploadSeq++}`,
        filename,
        columns:
          options.uploadPreview?.columns ?? [
            { name: "id", type: "INTEGER" },
            { name: "amount", type: "REAL" },
          ],
        row_count: options.uploadPreview?.row_count ?? 120,
        within_limit: options.uploadPreview?.within_limit ?? true,
        sample_rows:
          options.uploadPreview?.sample_rows ?? [{ id: 1, amount: 12.5 }],
      };
      uploads.set(preview.upload_id, preview);
      return Promise.resolve(jsonResponse(preview, 201));
    }

    match = path.match(/^\/api\/v1\/datasets\/([^/]+)$/);
    if (match) {
      const dataset = ownDataset(match[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      if (method === "GET") return Promise.resolve(jsonResponse(dataset));
      if (method === "DELETE") {
        datasets.delete(dataset.id);
        return Promise.resolve(jsonResponse({ deleted: true }));
      }
    }

    match = path.match(/^\/api\/v1\/datasets\/([^/]+)\/rows$/);
    if (match && method === "GET") {
      const dataset = ownDataset(match[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      const data = rowsStore.get(dataset.id) ?? { columns: dataset.columns, rows: [] };
      const rawPage = Number(url.searchParams.get("page") ?? "1");
      const rawSize = Number(url.searchParams.get("page_size") ?? "50");
      const page = Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 1;
      const pageSize = Number.isFinite(rawSize) && rawSize > 0 ? rawSize : 50;
      const start = (page - 1) * pageSize;
      return Promise.resolve(
        jsonResponse({
          columns: data.columns,
          rows: data.rows.slice(start, start + pageSize),
          total: data.rows.length,
        }),
      );
    }

    // ── SQL 步骤试运行（wire-transform-operators）：零副作用预览 ──
    // 假后端不真跑 SQL：回固定的两列预览（region/n），够验 UI 的
    // 「预览不落库」标注与渲染路径；真实语义由引擎侧单测与真库冒烟覆盖。
    match = path.match(/^\/api\/v1\/datasets\/([^/]+)\/sql\/preview$/);
    if (match && method === "POST") {
      const dataset = ownDataset(match[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      const body = (calls[calls.length - 1]!.body ?? {}) as {
        sql?: string;
        tables?: Array<{ name: string; dataset_key: string }>;
      };
      const sql = String(body.sql ?? "");
      if (!sql.trim()) {
        return Promise.resolve(jsonResponse({ detail: "sql 不能为空" }, 422));
      }
      if (!/^\s*(select|with)/i.test(sql)) {
        return Promise.resolve(
          jsonResponse(
            { detail: "SQL 步骤只接受只读查询（语句须以 SELECT 或 WITH 开头）" },
            422,
          ),
        );
      }
      const declared = body.tables ?? [];
      const used = new Set(
        Array.from(sql.matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)).map((m) =>
          m[1]!.toLowerCase(),
        ),
      );
      const undeclared = [...used].filter(
        (name) => !declared.some((t) => t.name.toLowerCase() === name),
      );
      if (undeclared.length > 0) {
        return Promise.resolve(
          jsonResponse(
            { detail: `SQL 引用了未声明的占位表: ${undeclared.join("、")}` },
            422,
          ),
        );
      }
      return Promise.resolve(
        jsonResponse({
          preview: true,
          columns: [
            { name: "region", type: "TEXT" },
            { name: "n", type: "INTEGER" },
          ],
          rows: [{ region: "华东", n: 2 }],
          row_count: 1,
          limit: 100,
          truncated: false,
        }),
      );
    }

    // ── 数据面（wire-customer-data-engine）：结构化查询 + key 三端点 ──
    match = path.match(/^\/api\/v1\/datasets\/([^/]+)\/query$/);
    if (match && method === "POST") {
      const dataset = ownDataset(match[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      const body = (calls[calls.length - 1]!.body ?? {}) as {
        filters?: Array<{ column: string; op: string; value?: unknown }>;
        sort?: Array<{ column: string; dir: string }>;
        limit?: number;
        offset?: number;
        distinct_column?: string;
      };
      const data = rowsStore.get(dataset.id) ?? { columns: dataset.columns, rows: [] };
      let out = [...data.rows];
      for (const f of body.filters ?? []) {
        out = out.filter((row) => {
          const cell = row[f.column];
          const value = f.value;
          switch (f.op) {
            case "eq": return cell === value;
            case "ne": return cell !== value;
            case "gt": return Number(cell) > Number(value);
            case "gte": return Number(cell) >= Number(value);
            case "lt": return Number(cell) < Number(value);
            case "lte": return Number(cell) <= Number(value);
            case "in":
              return Array.isArray(value) && value.some((v) => cell === v);
            case "like": return String(cell ?? "").includes(String(value ?? ""));
            case "isnull": return cell == null;
            case "notnull": return cell != null;
            default: return true;
          }
        });
      }
      // distinct 形态（wire-form-entry-advanced 动态选项解析）：去重非空值、
      // 按值升序、limit 裁剪并如实标注 truncated——与 api 档 execute_query
      // 的 distinct 响应同形（{distinct_column, values, count, …}）。
      if (body.distinct_column) {
        const distinct = body.distinct_column;
        const seen = new Set<string>();
        const values: string[] = [];
        for (const row of out) {
          const cell = row[distinct];
          if (cell == null) continue;
          const text = String(cell).trim();
          if (text === "" || seen.has(text)) continue;
          seen.add(text);
          values.push(text);
        }
        values.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
        const limit = body.limit ?? 100;
        return Promise.resolve(
          jsonResponse({
            columns: data.columns,
            distinct_column: distinct,
            values: values.slice(0, limit),
            count: values.length,
            total: out.length,
            limit,
            offset: 0,
            truncated: values.length > limit,
          }),
        );
      }
      for (const s of [...(body.sort ?? [])].reverse()) {
        out.sort((a, b) => {
          const av = a[s.column];
          const bv = b[s.column];
          const cmp = av === bv ? 0 : av == null ? -1 : bv == null ? 1 : String(av) < String(bv) ? -1 : 1;
          return s.dir === "desc" ? -cmp : cmp;
        });
      }
      const limit = body.limit ?? 50;
      const offset = body.offset ?? 0;
      return Promise.resolve(
        jsonResponse({
          columns: data.columns,
          rows: out.slice(offset, offset + limit),
          total: out.length,
          limit,
          offset,
          truncated: out.length > offset + Math.min(out.length - offset, limit),
        }),
      );
    }

    if (path === "/api/v1/data-plane-keys" && method === "GET") {
      return Promise.resolve(
        jsonResponse(
          [...keysStore.values()].filter((k) => !k.customer_id || k.customer_id === customerId),
        ),
      );
    }
    if (path === "/api/v1/data-plane-keys" && method === "POST") {
      const body = (calls[calls.length - 1]!.body ?? {}) as {
        label?: string;
        dataset_ids?: string[];
      };
      const id = `k${keysStore.size + 1}`;
      const fingerprint = `fp${id.padStart(12, "0")}`;
      const created = {
        id,
        customer_id: customerId,
        label: body.label ?? "unlabeled",
        fingerprint,
        dataset_ids: body.dataset_ids ?? [],
        status: "active",
        created_at: "2026-10-10T00:00:00Z",
        revoked_at: null,
        last_used_at: null,
      };
      keysStore.set(id, created);
      return Promise.resolve(
        jsonResponse({ ...created, key: `wdk-test-${id}` }, 201),
      );
    }
    match = path.match(/^\/api\/v1\/data-plane-keys\/([^/]+)$/);
    if (match && method === "DELETE") {
      const row = keysStore.get(match[1]);
      if (!row) {
        return Promise.resolve(jsonResponse({ detail: "key not found" }, 404));
      }
      row.status = "revoked";
      row.revoked_at = "2026-10-10T01:00:00Z";
      return Promise.resolve(jsonResponse(row));
    }

    match = path.match(/^\/api\/v1\/datasets\/([^/]+)\/corrections$/);
    if (match) {
      const dataset = ownDataset(match[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      if (method === "GET") {
        // 真实 API 回 {corrections:[…]} 信封（与生产同形——曾因假后端回
        // 裸数组掩盖客户端解包缺失，生产白屏，E2E 抓出后修正）。
        return Promise.resolve(
          jsonResponse({ corrections: corrections.get(dataset.id) ?? [] }),
        );
      }
      if (method === "POST") {
        const payload = (calls[calls.length - 1]!.body ?? {}) as CorrectionInput;
        const id = nextCorrectionId++;
        const correction: Correction = {
          id,
          dataset_id: dataset.id,
          op: payload.op as CorrectionOp,
          row_id: "row_id" in payload ? payload.row_id : null,
          column: payload.op === "update" ? payload.column : null,
          value: payload.op === "update" ? payload.value : null,
          values: payload.op === "insert" ? payload.values : null,
          actor: "console-test",
          created_at: "2026-10-05T12:30:00",
          reverted_at: null,
          dangling_at: null,
          dangling_reason: null,
        };
        addCorrection(correction);
        // 假后端同步应用修正，便于断言预览刷新后的结果。
        const data = rowsStore.get(dataset.id);
        if (data) {
          if (payload.op === "update") {
            data.rows = data.rows.map((row) =>
              row.__row_id === payload.row_id
                ? { ...row, [payload.column]: payload.value }
                : row,
            );
          } else if (payload.op === "delete") {
            data.rows = data.rows.filter((row) => row.__row_id !== payload.row_id);
          } else {
            data.rows = [...data.rows, { ...payload.values, __row_id: `ins:${id}` }];
          }
        }
        return Promise.resolve(jsonResponse({ correction_id: id }, 201));
      }
    }

    match = path.match(/^\/api\/v1\/datasets\/([^/]+)\/corrections\/(\d+)\/revert$/);
    if (match && method === "POST") {
      const dataset = ownDataset(match[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      const list = corrections.get(dataset.id) ?? [];
      const correction = list.find((c) => c.id === Number(match![2]));
      if (!correction) {
        return Promise.resolve(jsonResponse({ detail: "correction not found" }, 404));
      }
      correction.reverted_at = "2026-10-05T13:00:00";
      return Promise.resolve(jsonResponse({ reverted: true }));
    }

    match = path.match(/^\/api\/v1\/datasets\/([^/]+)\/syncs$/);
    if (match && method === "GET") {
      const dataset = ownDataset(match[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      return Promise.resolve(jsonResponse(syncs.get(dataset.id) ?? []));
    }

    // ── 增量同步设置（wire-incremental-sync 契约镜像）：PATCH
    //    /api/v1/datasets/{id}/sync-settings——非连接器 422、水位列必须 ∈
    //    列清单、增量必须配水位列、切回 snapshot 清水位字段、reset_watermark
    //    只清水位值；返回更新后数据集（详情刷新同口径）。──
    match = path.match(/^\/api\/v1\/datasets\/([^/]+)\/sync-settings$/);
    if (match && method === "PATCH") {
      const dataset = ownDataset(match[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      if (options.failSyncSettingsStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failSyncSettingsDetail ??
                `sync settings ${options.failSyncSettingsStatus}`,
            },
            options.failSyncSettingsStatus,
          ),
        );
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        sync_mode?: string;
        watermark_column?: string;
        reset_watermark?: boolean;
      };
      if (dataset.source_kind !== "connector") {
        return Promise.resolve(
          jsonResponse(
            { detail: "仅连接器（connector）来源数据集支持增量同步设置" },
            422,
          ),
        );
      }
      if (payload.reset_watermark === true) {
        dataset.watermark_value = null;
        dataset.updated_at = "2026-10-11T09:00:00";
        return Promise.resolve(jsonResponse(dataset));
      }
      const nextMode = payload.sync_mode ?? dataset.sync_mode ?? "snapshot";
      if (nextMode === "incremental") {
        const column = payload.watermark_column ?? dataset.watermark_column ?? "";
        if (!column) {
          return Promise.resolve(
            jsonResponse(
              { detail: "增量模式必须配置水位列（watermark_column）" },
              422,
            ),
          );
        }
        if (!dataset.columns.some((c) => c.name === column)) {
          return Promise.resolve(
            jsonResponse(
              { detail: `水位列 ${column} 不在数据集列清单中` },
              422,
            ),
          );
        }
        dataset.sync_mode = "incremental";
        dataset.watermark_column = column;
      } else {
        dataset.sync_mode = "snapshot";
        dataset.watermark_column = null;
        dataset.watermark_value = null;
      }
      dataset.updated_at = "2026-10-11T09:00:00";
      return Promise.resolve(jsonResponse(dataset));
    }

    match = path.match(/^\/api\/v1\/datasets\/([^/]+)\/resume$/);
    if (match && method === "POST") {
      const dataset = ownDataset(match[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        accept_columns?: string[];
      };
      for (const name of payload.accept_columns ?? []) {
        if (!dataset.columns.some((c) => c.name === name)) {
          dataset.columns = [...dataset.columns, { name, type: "TEXT" }];
        }
      }
      dataset.sync_status = "idle";
      dataset.paused_reason = null;
      dataset.updated_at = "2026-10-05T13:10:00";
      return Promise.resolve(jsonResponse(dataset));
    }

    // ── 我的数据：连接器（配对令牌 / 列表 / 吊销） ──
    if (path === "/api/v1/connectors/pairing-tokens" && method === "POST") {
      const ttl = options.pairingTokenTtlSeconds ?? 900;
      return Promise.resolve(
        jsonResponse(
          {
            token: `fwp_test_${nextTokenSeq++}`,
            expires_at: new Date(Date.now() + ttl * 1000).toISOString(),
          },
          201,
        ),
      );
    }

    if (path === "/api/v1/connectors" && method === "GET") {
      const mine = [...connectors.values()].filter(
        (c) => !c.customer_id || c.customer_id === customerId,
      );
      return Promise.resolve(jsonResponse(mine));
    }

    match = path.match(/^\/api\/v1\/connectors\/([^/]+)$/);
    if (match && method === "DELETE") {
      const connector = connectors.get(match[1]);
      if (
        !connector ||
        (connector.customer_id && connector.customer_id !== customerId)
      ) {
        return Promise.resolve(jsonResponse({ detail: "connector not found" }, 404));
      }
      connector.revoked_at = "2026-10-05T13:20:00";
      return Promise.resolve(jsonResponse(connector));
    }

    // ── 定时刷新调度（wire-refresh-scheduler 4.1）：列表/建（类型+地板+配额
    //    校验）/删/暂停恢复——语义对齐 api 仓 schedules 路由（Bearer 客户隔离）──
    const ownSchedules = (): StoredSchedule[] =>
      [...schedulesStore.values()].filter(
        (s) => !s.customer_id || s.customer_id === customerId,
      );
    const scheduleRow = (row: StoredSchedule): RefreshSchedule => {
      const { customer_id: _c, ...out } = row;
      return out;
    };
    const scheduleBase = Date.parse("2026-10-11T08:00:00Z");

    if (path === "/api/v1/refresh-schedules" && method === "GET") {
      return Promise.resolve(jsonResponse(ownSchedules().map(scheduleRow)));
    }
    if (path === "/api/v1/refresh-schedules" && method === "POST") {
      if (options.failScheduleCreateStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failScheduleCreateDetail ??
                `schedule create ${options.failScheduleCreateStatus}`,
            },
            options.failScheduleCreateStatus,
          ),
        );
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        target_type?: string;
        target_id?: string;
        interval_minutes?: number;
      };
      // 类型校验：dataset 须 derived 来源、artifact 须 kind=dashboard（跨客户
      // 与失存目标一律 404——不泄露存在性）。
      let reject: { status: number; detail: string } | null = null;
      if (payload.target_type === "dataset") {
        const dataset = ownDataset(String(payload.target_id ?? ""));
        if (!dataset) {
          reject = { status: 404, detail: "dataset not found" };
        } else if (dataset.source_kind !== "derived") {
          reject = {
            status: 422,
            detail: "仅派生（derived）来源数据集可设定时刷新（上传/连接器来源走各自同步链路）",
          };
        }
      } else if (payload.target_type === "artifact") {
        const artifact = ownLifecycleArtifact(Number(payload.target_id), customerId);
        if (!artifact) {
          reject = { status: 404, detail: "artifact not found" };
        } else if (artifact.kind !== "dashboard") {
          reject = {
            status: 422,
            detail: "仅仪表盘（dashboard）产物可设定时刷新",
          };
        }
      } else {
        reject = {
          status: 422,
          detail: "target_type 必须是 dataset 或 artifact",
        };
      }
      // 间隔地板：dataset ≥60、artifact ≥360（AI 刷新耗 token，封无人值守成本）。
      const floor = payload.target_type === "artifact" ? 360 : 60;
      const minutes = payload.interval_minutes;
      if (
        !reject &&
        (typeof minutes !== "number" || !Number.isFinite(minutes) || minutes < floor)
      ) {
        reject = {
          status: 422,
          detail: `刷新间隔不能低于 ${floor} 分钟（当前 ${String(minutes)}）`,
        };
      }
      // 每客户配额（默认 10）。
      if (!reject && ownSchedules().length >= 10) {
        reject = { status: 422, detail: "定时刷新数量已达上限（10 个）" };
      }
      if (reject) {
        return Promise.resolve(jsonResponse({ detail: reject.detail }, reject.status));
      }
      const id = nextScheduleId++;
      const created: StoredSchedule = {
        id,
        customer_id: customerId,
        target_type: payload.target_type as StoredSchedule["target_type"],
        target_id: String(payload.target_id),
        interval_minutes: minutes!,
        next_run_at: new Date(scheduleBase + minutes! * 60_000)
          .toISOString()
          .slice(0, 19),
        paused: false,
        last_status: "pending",
        last_run_at: null,
        last_error: null,
        created_at: "2026-10-11T08:00:00",
      };
      schedulesStore.set(id, created);
      return Promise.resolve(jsonResponse(scheduleRow(created), 201));
    }
    match = path.match(/^\/api\/v1\/refresh-schedules\/(\d+)$/);
    if (match && method === "DELETE") {
      const row = schedulesStore.get(Number(match[1]));
      if (!row || (row.customer_id && row.customer_id !== customerId)) {
        return Promise.resolve(jsonResponse({ detail: "schedule not found" }, 404));
      }
      schedulesStore.delete(row.id);
      return Promise.resolve(jsonResponse(scheduleRow(row)));
    }
    match = path.match(/^\/api\/v1\/refresh-schedules\/(\d+)\/(pause|resume)$/);
    if (match && method === "POST") {
      const row = schedulesStore.get(Number(match[1]));
      if (!row || (row.customer_id && row.customer_id !== customerId)) {
        return Promise.resolve(jsonResponse({ detail: "schedule not found" }, 404));
      }
      row.paused = match[2] === "pause";
      return Promise.resolve(jsonResponse(scheduleRow(row)));
    }

    // ── 管线图（wire-transform-pipeline-view 2.2）：GET /api/v1/pipeline/graph
    //    ——语义对齐 api 仓组装：本客户数据集全量节点（storage_tier 容错读取、
    //    dataset 调度登记→schedule_cron）、derived 的 source_ref 解析父 key 成边
    //    （op 统一 derived）、BFS 深度（源=0）、环/孤儿边跳过计 anomalies、
    //    节点超 500 截断 truncated。pipelineGraph 覆盖时原样返回。──
    if (path === "/api/v1/pipeline/graph" && method === "GET") {
      if (options.failPipelineGraphStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failPipelineGraphDetail ??
                `pipeline graph ${options.failPipelineGraphStatus}`,
            },
            options.failPipelineGraphStatus,
          ),
        );
      }
      if (options.pipelineGraph) {
        return Promise.resolve(jsonResponse(options.pipelineGraph));
      }
      const mine = [...datasets.values()].filter(
        (d) => !d.customer_id || d.customer_id === customerId,
      );
      const byKey = new Map(mine.map((d) => [d.key, d]));
      let anomalies = 0;
      const parentOf = new Map<string, string>();
      const edges: PipelineGraph["edges"] = [];
      for (const dataset of mine) {
        if (dataset.source_kind !== "derived") continue;
        const parent = dataset.source_ref ? byKey.get(dataset.source_ref) : undefined;
        if (!parent) {
          anomalies += 1; // 孤儿边：父 key 无对应行
          continue;
        }
        edges.push({
          from_dataset_id: parent.id,
          to_dataset_id: dataset.id,
          op: "derived",
        });
        parentOf.set(dataset.id, parent.id);
      }
      // 深度（源=0，派生按链深）；沿父链遇已访问节点即环 → 计 anomalies、按 0 层。
      const depthCache = new Map<string, number>();
      const depthOf = (id: string, seen: Set<string>): number => {
        const cached = depthCache.get(id);
        if (cached != null) return cached;
        const parent = parentOf.get(id);
        if (!parent) {
          depthCache.set(id, 0);
          return 0;
        }
        if (seen.has(id)) {
          anomalies += 1;
          depthCache.set(id, 0);
          return 0;
        }
        seen.add(id);
        const depth = depthOf(parent, seen) + 1;
        depthCache.set(id, depth);
        return depth;
      };
      const truncated = mine.length > 500;
      const kept = new Set(mine.slice(0, 500).map((d) => d.id));
      const nodes: PipelineGraph["nodes"] = mine
        .filter((d) => kept.has(d.id))
        .map((d) => {
          const scheduled = [...schedulesStore.values()].some(
            (s) =>
              (!s.customer_id || s.customer_id === customerId) &&
              s.target_type === "dataset" &&
              s.target_id === d.id,
          );
          return {
            dataset_id: d.id,
            key: d.key,
            name: d.name,
            source_kind: d.source_kind,
            storage_tier: (d as DatasetWithTier).storage_tier ?? null,
            row_count: d.row_count,
            updated_at: d.updated_at,
            depth: depthOf(d.id, new Set()),
            schedule_cron: scheduled ? "0 * * * *" : null,
          };
        });
      return Promise.resolve(
        jsonResponse({
          nodes,
          edges: edges.filter((e) => kept.has(e.from_dataset_id) && kept.has(e.to_dataset_id)),
          anomalies,
          truncated,
        }),
      );
    }

    // ── 资产目录（wire-asset-catalog 2.1）：GET /api/v1/assets——域/标签/
    //    敏感度/来源类型/新鲜度组合筛选 + 名称描述搜索 + 分页；条目 = 数据集
    //    投影 + 资产元数据段（未补全=默认值）；仅本客户数据集（隔离纪律）。──
    if (path === "/api/v1/assets" && method === "GET") {
      if (options.failAssetsStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `assets ${options.failAssetsStatus}` },
            options.failAssetsStatus,
          ),
        );
      }
      const mine = [...datasets.values()].filter(
        (d) => !d.customer_id || d.customer_id === customerId,
      );
      const q = (url.searchParams.get("q") ?? "").trim().toLowerCase();
      const domain = url.searchParams.get("domain") ?? "";
      const tag = url.searchParams.get("tag") ?? "";
      const sensitivity = url.searchParams.get("sensitivity") ?? "";
      const sourceKind = url.searchParams.get("source_kind") ?? "";
      const freshness = url.searchParams.get("freshness") ?? "";
      const rawPage = Number(url.searchParams.get("page") ?? "1");
      const rawSize = Number(url.searchParams.get("page_size") ?? "20");
      const page = Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 1;
      const pageSize = Number.isFinite(rawSize) && rawSize > 0 ? rawSize : 20;
      let rows = mine;
      if (domain) {
        rows = rows.filter((d) => (assetMetaOf(d.id).domain ?? "") === domain);
      }
      if (tag) rows = rows.filter((d) => assetMetaOf(d.id).tags.includes(tag));
      if (sensitivity) {
        rows = rows.filter((d) => assetMetaOf(d.id).sensitivity === sensitivity);
      }
      if (sourceKind) rows = rows.filter((d) => d.source_kind === sourceKind);
      if (freshness) {
        rows = rows.filter((d) => freshnessOf(d.id).freshness === freshness);
      }
      if (q) {
        rows = rows.filter(
          (d) =>
            d.name.toLowerCase().includes(q) ||
            (assetMetaOf(d.id).description ?? "").toLowerCase().includes(q),
        );
      }
      const total = rows.length;
      const items = rows
        .slice((page - 1) * pageSize, (page - 1) * pageSize + pageSize)
        .map((d) => {
          const meta = assetMetaOf(d.id);
          return {
            id: d.id,
            name: d.name,
            source_kind: d.source_kind,
            row_count: d.row_count,
            storage_tier: (d as DatasetWithTier).storage_tier ?? null,
            created_at: d.created_at,
            asset: {
              domain: meta.domain,
              tags: meta.tags,
              description: meta.description,
              cadence_hint: meta.cadence_hint,
              owner_note: meta.owner_note,
              sensitivity: meta.sensitivity,
              cadence_effective: meta.cadence_effective,
            },
          };
        });
      return Promise.resolve(jsonResponse({ items, total, page }));
    }

    // ── 质量汇总（wire-dataset-quality）：GET /api/v1/quality/summary?
    //    dataset_ids=a,b,c——只回有种子的 id（缺失键=无徽章）；条目带
    //    freshness/last_refresh_at/row_count/status。──
    if (path === "/api/v1/quality/summary" && method === "GET") {
      if (options.failQualitySummaryStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `quality summary ${options.failQualitySummaryStatus}` },
            options.failQualitySummaryStatus,
          ),
        );
      }
      const ids = (url.searchParams.get("dataset_ids") ?? "")
        .split(",")
        .filter(Boolean);
      const statuses: Record<string, unknown> = {};
      for (const id of ids) {
        const seed = qualityStore.get(id);
        if (!seed) continue;
        const fresh = freshnessOf(id);
        const latest = seed.latest as
          | { status?: unknown; row_count?: unknown }
          | null
          | undefined;
        statuses[id] = {
          freshness: fresh.freshness,
          last_refresh_at: fresh.last_refresh_at,
          row_count:
            typeof latest?.row_count === "number" ? latest.row_count : 0,
          status:
            typeof latest?.status === "string"
              ? latest.status
              : latest == null
                ? "empty"
                : "ok",
        };
      }
      return Promise.resolve(jsonResponse({ statuses }));
    }

    // ── 资产元数据（wire-asset-catalog 1.2-1.5）：GET/PATCH
    //    /api/v1/datasets/{id}/asset-meta + GET /asset-meta/events——懒建行、
    //    敏感度三值 422 回合法清单、viewer 只读 403（变更类方法统一拒绝）、
    //    留痕 append-only（PATCH 续记最近在前）。──
    const assetMetaOut = (dataset: Dataset): Record<string, unknown> => {
      const meta = assetMetaOf(dataset.id);
      return {
        dataset_id: dataset.id,
        domain: meta.domain,
        tags: meta.tags,
        description: meta.description,
        cadence_hint: meta.cadence_hint,
        owner_note: meta.owner_note,
        sensitivity: meta.sensitivity,
        cadence_effective: meta.cadence_effective,
      };
    };
    match = path.match(/^\/api\/v1\/datasets\/([^/]+)\/asset-meta$/);
    if (match) {
      const dataset = ownDataset(match[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      if (method === "GET") {
        if (options.failAssetMetaStatus) {
          return Promise.resolve(
            jsonResponse(
              { detail: `asset meta ${options.failAssetMetaStatus}` },
              options.failAssetMetaStatus,
            ),
          );
        }
        return Promise.resolve(jsonResponse(assetMetaOut(dataset)));
      }
      if (method === "PATCH") {
        if (options.failAssetMetaPatchStatus) {
          return Promise.resolve(
            jsonResponse(
              {
                detail:
                  options.failAssetMetaPatchDetail ??
                  `asset meta ${options.failAssetMetaPatchStatus}`,
              },
              options.failAssetMetaPatchStatus,
            ),
          );
        }
        // viewer 只读（统一依赖 VIEWER_DENIED_METHODS 语义）。
        if (teamMembersStore.get(customerId)?.role === "viewer") {
          return Promise.resolve(
            jsonResponse({ detail: "团队成员权限不足（viewer 只读）" }, 403),
          );
        }
        const payload = (calls[calls.length - 1]!.body ?? {}) as {
          domain?: string | null;
          tags?: string[];
          description?: string | null;
          cadence_hint?: string | null;
          owner_note?: string | null;
          sensitivity?: string;
        };
        if (
          payload.sensitivity !== undefined &&
          !["常规", "内部", "敏感"].includes(payload.sensitivity)
        ) {
          return Promise.resolve(
            jsonResponse(
              { detail: "敏感度只能是：常规 / 内部 / 敏感" },
              422,
            ),
          );
        }
        const before = assetMetaOut(dataset);
        const row: StoredAssetMeta = { ...assetMetaOf(dataset.id) };
        delete row.customer_id;
        if (payload.domain !== undefined) row.domain = payload.domain;
        if (payload.tags !== undefined) {
          row.tags = Array.isArray(payload.tags)
            ? payload.tags.filter((tag): tag is string => typeof tag === "string")
            : [];
        }
        if (payload.description !== undefined) row.description = payload.description;
        if (payload.cadence_hint !== undefined) {
          row.cadence_hint =
            payload.cadence_hint === "daily" ||
            payload.cadence_hint === "weekly" ||
            payload.cadence_hint === "monthly"
              ? payload.cadence_hint
              : null;
        }
        if (payload.owner_note !== undefined) row.owner_note = payload.owner_note;
        if (payload.sensitivity !== undefined) {
          row.sensitivity = payload.sensitivity as StoredAssetMeta["sensitivity"];
        }
        assetMetaStore.set(dataset.id, row);
        const after = assetMetaOut(dataset);
        const trail = assetEventsStore.get(dataset.id) ?? [];
        trail.unshift({
          at: "2026-10-13T09:00:00",
          actor_sub: customerId,
          before,
          after,
        });
        assetEventsStore.set(dataset.id, trail);
        return Promise.resolve(jsonResponse(after));
      }
    }
    match = path.match(/^\/api\/v1\/datasets\/([^/]+)\/asset-meta\/events$/);
    if (match && method === "GET") {
      const dataset = ownDataset(match[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      return Promise.resolve(
        jsonResponse({ items: assetEventsStore.get(dataset.id) ?? [] }),
      );
    }

    // ── 数据集质量（wire-dataset-quality 4.5/4.6）：GET
    //    /api/v1/datasets/{id}/quality（latest/history/freshness/cadence_hint）
    //    + POST /quality/refresh（手动触发追加 ok 快照，viewer 403）。──
    match = path.match(/^\/api\/v1\/datasets\/([^/]+)\/quality$/);
    if (match && method === "GET") {
      const dataset = ownDataset(match[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      if (options.failQualityStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `quality ${options.failQualityStatus}` },
            options.failQualityStatus,
          ),
        );
      }
      const seed = qualityStore.get(dataset.id) ?? {};
      return Promise.resolve(
        jsonResponse({
          latest: seed.latest ?? null,
          history: seed.history ?? [],
          freshness:
            seed.freshness ?? { freshness: "unknown", last_refresh_at: null },
          cadence_hint: seed.cadence_hint ?? assetMetaOf(dataset.id).cadence_hint,
        }),
      );
    }
    match = path.match(/^\/api\/v1\/datasets\/([^/]+)\/quality\/refresh$/);
    if (match && method === "POST") {
      const dataset = ownDataset(match[1]);
      if (!dataset) {
        return Promise.resolve(jsonResponse({ detail: "dataset not found" }, 404));
      }
      if (options.failQualityRefreshStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failQualityRefreshDetail ??
                `quality refresh ${options.failQualityRefreshStatus}`,
            },
            options.failQualityRefreshStatus,
          ),
        );
      }
      // 手动触发受角色门禁（owner/editor；viewer 403——api 侧同口径）。
      if (teamMembersStore.get(customerId)?.role === "viewer") {
        return Promise.resolve(
          jsonResponse({ detail: "团队成员权限不足（viewer 只读）" }, 403),
        );
      }
      const snapshot = {
        computed_at: "2026-10-13T10:00:00",
        triggered_by: "manual",
        status: "ok",
        row_count: dataset.row_count,
        pk_declared: false,
        pk_unique_ratio: null,
        null_rates: [],
        sample_limit_hit: false,
        error_text: null,
      };
      const seed = qualityStore.get(dataset.id) ?? {};
      const history = [...(seed.history ?? [])];
      const prior = seed.latest as
        | { computed_at?: unknown; row_count?: unknown; null_rates?: unknown }
        | null
        | undefined;
      if (prior && typeof prior === "object") {
        history.push({
          computed_at: prior.computed_at ?? "",
          row_count: prior.row_count ?? 0,
          null_rates: prior.null_rates ?? [],
        });
      }
      history.push({
        computed_at: snapshot.computed_at,
        row_count: snapshot.row_count,
        null_rates: [],
      });
      qualityStore.set(dataset.id, { ...seed, latest: snapshot, history });
      return Promise.resolve(jsonResponse(snapshot, 201));
    }

    // ── 变换管线（wire-transform-pipelines 5.x）：CRUD（版本 append-only：
    //    PUT 改步骤/基础集 → 新版本并回 version_created；仅名称/描述就地更新）
    //    / run（按序执行、失败中止后续、配额/引擎不支持如实态、调度触发的
    //    run 记 trigger=schedule）/ runs 历史（倒序分页 + 单 run 详情）/
    //    schedule（五字段 cron 校验、pause/resume/delete）。写操作 viewer 403。──
    const delayed = <T,>(body: T, status = 200): Promise<FakeResponse> =>
      options.pipelineRunDelayMs
        ? new Promise((resolve) => {
            setTimeout(
              () => resolve(jsonResponse(body, status)),
              options.pipelineRunDelayMs,
            );
          })
        : Promise.resolve(jsonResponse(body, status));
    if (path === "/api/v1/pipelines" && method === "GET") {
      if (options.failPipelinesStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `pipelines ${options.failPipelinesStatus}` },
            options.failPipelinesStatus,
          ),
        );
      }
      const mine = [...pipelinesStore.values()].filter(
        (p) => !p.customer_id || p.customer_id === customerId,
      );
      return Promise.resolve(
        jsonResponse({
          items: mine
        .sort((a, b) => b.id - a.id)
        .map(pipelineListItemOut),
          total: mine.length,
        }),
      );
    }
    if (path === "/api/v1/pipelines" && method === "POST") {
      if (options.failPipelineSaveStatus) {
        return Promise.resolve(
          jsonResponse(
            {
              detail:
                options.failPipelineSaveDetail ??
                `pipelines ${options.failPipelineSaveStatus}`,
            },
            options.failPipelineSaveStatus,
          ),
        );
      }
      if (isViewer(customerId)) return Promise.resolve(viewerDenied());
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        name?: unknown;
        description?: unknown;
        base_dataset_id?: unknown;
        steps?: unknown;
      };
      if (typeof payload.name !== "string" || !payload.name.trim()) {
        return Promise.resolve(
          jsonResponse({ detail: "管线名称不能为空" }, 422),
        );
      }
      if (
        typeof payload.base_dataset_id !== "string" ||
        !payload.base_dataset_id
      ) {
        return Promise.resolve(
          jsonResponse({ detail: "基础数据集不能为空" }, 422),
        );
      }
      const stepsError = validateSteps(payload.steps);
      if (stepsError) {
        return Promise.resolve(jsonResponse({ detail: stepsError }, 422));
      }
      const id = nextPipelineId++;
      const steps = (payload.steps as PipelineStepDef[]).map((step) => ({
        name: step.name,
        op: step.op,
        params: { ...(step.params ?? {}) },
      }));
      pipelinesStore.set(id, {
        id,
        customer_id: customerId,
        name: payload.name as string,
        description:
          typeof payload.description === "string" && payload.description
            ? payload.description
            : null,
        base_dataset_id: payload.base_dataset_id as string,
        created_at: "2026-10-13T09:00:00",
        updated_at: "2026-10-13T09:00:00",
        versions: [
          {
            version: 1,
            steps,
            created_at: "2026-10-13T09:00:00",
            created_by: customerId,
          },
        ],
        schedule: null,
      });
      pipelineRunsStore.set(id, []);
      return Promise.resolve(
        jsonResponse(pipelineDetailOut(pipelinesStore.get(id)!), 201),
      );
    }
    match = path.match(/^\/api\/v1\/pipelines\/(\d+)$/);
    if (match) {
      const pipeline = ownPipeline(Number(match[1]), customerId);
      if (!pipeline) {
        return Promise.resolve(
          jsonResponse({ detail: "pipeline not found" }, 404),
        );
      }
      if (method === "GET") {
        if (options.failPipelineDetailStatus) {
          return Promise.resolve(
            jsonResponse(
              { detail: `pipeline ${options.failPipelineDetailStatus}` },
              options.failPipelineDetailStatus,
            ),
          );
        }
        return Promise.resolve(jsonResponse(pipelineDetailOut(pipeline)));
      }
      if (method === "PUT") {
        if (options.failPipelineSaveStatus) {
          return Promise.resolve(
            jsonResponse(
              {
                detail:
                  options.failPipelineSaveDetail ??
                  `pipelines ${options.failPipelineSaveStatus}`,
              },
              options.failPipelineSaveStatus,
            ),
          );
        }
        if (isViewer(customerId)) return Promise.resolve(viewerDenied());
        const payload = (calls[calls.length - 1]!.body ?? {}) as {
          name?: unknown;
          description?: unknown;
          base_dataset_id?: unknown;
          steps?: unknown;
        };
        if (payload.name !== undefined) {
          if (typeof payload.name !== "string" || !payload.name.trim()) {
            return Promise.resolve(
              jsonResponse({ detail: "管线名称不能为空" }, 422),
            );
          }
          pipeline.name = payload.name;
        }
        if (payload.description !== undefined) {
          pipeline.description =
            typeof payload.description === "string" && payload.description
              ? payload.description
              : null;
        }
        let versionCreated: number | undefined;
        const stepsChanged =
          payload.steps !== undefined &&
          !stepsEqual(
            payload.steps as PipelineStepDef[],
            pipeline.versions[pipeline.versions.length - 1]!.steps,
          );
        const baseChanged =
          payload.base_dataset_id !== undefined &&
          payload.base_dataset_id !== pipeline.base_dataset_id;
        if (baseChanged) {
          if (
            typeof payload.base_dataset_id !== "string" ||
            !payload.base_dataset_id
          ) {
            return Promise.resolve(
              jsonResponse({ detail: "基础数据集不能为空" }, 422),
            );
          }
          pipeline.base_dataset_id = payload.base_dataset_id;
        }
        if (stepsChanged || baseChanged) {
          const stepsError = validateSteps(payload.steps);
          if (stepsError) {
            return Promise.resolve(
              jsonResponse({ detail: stepsError }, 422),
            );
          }
          const nextVersion = pipeline.versions.length + 1;
          pipeline.versions.push({
            version: nextVersion,
            steps: (payload.steps as PipelineStepDef[]).map((step) => ({
              name: step.name,
              op: step.op,
              params: { ...(step.params ?? {}) },
            })),
            created_at: "2026-10-13T10:00:00",
            created_by: customerId,
          });
          versionCreated = nextVersion;
        }
        pipeline.updated_at = "2026-10-13T10:00:00";
        return Promise.resolve(
          jsonResponse({
            ...pipelineDetailOut(pipeline),
            ...(versionCreated != null ? { version_created: versionCreated } : {}),
          }),
        );
      }
    }
    match = path.match(/^\/api\/v1\/pipelines\/(\d+)\/run$/);
    if (match && method === "POST") {
      const pipeline = ownPipeline(Number(match[1]), customerId);
      if (!pipeline) {
        return Promise.resolve(
          jsonResponse({ detail: "pipeline not found" }, 404),
        );
      }
      if (options.failPipelineRunStatus) {
        return delayed(
          {
            detail:
              options.failPipelineRunDetail ??
              `pipeline run ${options.failPipelineRunStatus}`,
          },
          options.failPipelineRunStatus,
        );
      }
      if (isViewer(customerId)) return Promise.resolve(viewerDenied());
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        version?: unknown;
      };
      const version =
        typeof payload.version === "number" ? payload.version : pipeline.versions.length;
      const versionRow = pipeline.versions.find((v) => v.version === version);
      if (!versionRow) {
        return Promise.resolve(
          jsonResponse({ detail: `版本 v${version} 不存在` }, 404),
        );
      }
      // 按序执行：失败/配额/引擎不支持即中止，后续步骤绝不产生记录；
      // 输出槽位 id 稳定（同版本重跑同 id = 原地刷新语义）。
      const steps: PipelineStepRun[] = [];
      let failed = false;
      let runError: string | null = null;
      const baseRows =
        datasets.get(pipeline.base_dataset_id)?.row_count ?? 100;
      for (let i = 0; i < versionRow.steps.length; i += 1) {
        const step = versionRow.steps[i]!;
        const seq = i + 1;
        const failAt = pipeline.failAt;
        if (failAt && failAt.seq === seq) {
          const error =
            failAt.error ??
            (failAt.kind === "quota_exhausted"
              ? "配额硬停：本月行数额度已用尽"
              : failAt.kind === "unsupported"
                ? "引擎暂不支持 link：目标 cell 尚未滚动新引擎镜像"
                : `步骤「${step.name}」执行失败`);
          steps.push({
            seq,
            name: step.name,
            op: step.op,
            status: failAt.kind === "quota_exhausted" ? "quota_exhausted" : "failed",
            output_dataset_id: null,
            output_dataset_name: null,
            row_count: null,
            duration_ms: 90 * seq,
            error_text: error,
          });
          failed = true;
          runError = error;
          break;
        }
        steps.push(simulateStep(pipeline.id, version, step, seq, baseRows));
      }
      pipelineRunClock += 1;
      const startedAt = new Date(pipelineRunBase + pipelineRunClock * 60_000)
        .toISOString()
        .replace(/\.\d{3}Z$/, "");
      const run: StoredPipelineRun = {
        id: nextPipelineRunId++,
        status: failed ? "failed" : "success",
        version,
        trigger: "manual",
        started_at: startedAt,
        finished_at: startedAt,
        error_text: runError,
        steps,
      };
      const history = pipelineRunsStore.get(pipeline.id) ?? [];
      history.unshift(run);
      pipelineRunsStore.set(pipeline.id, history);
      return delayed(run, 201);
    }
    match = path.match(/^\/api\/v1\/pipelines\/(\d+)\/runs$/);
    if (match && method === "GET") {
      const pipeline = ownPipeline(Number(match[1]), customerId);
      if (!pipeline) {
        return Promise.resolve(
          jsonResponse({ detail: "pipeline not found" }, 404),
        );
      }
      if (options.failPipelineHistoryStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `pipeline runs ${options.failPipelineHistoryStatus}` },
            options.failPipelineHistoryStatus,
          ),
        );
      }
      const runs = pipelineRunsStore.get(pipeline.id) ?? [];
      const page = Math.max(1, Number(url.searchParams.get("page") ?? "1") || 1);
      const pageSize = Math.max(
        1,
        Number(url.searchParams.get("page_size") ?? "10") || 10,
      );
      return Promise.resolve(
        jsonResponse({
          items: runs
        .slice((page - 1) * pageSize, (page - 1) * pageSize + pageSize)
        .map((run) => ({
          id: run.id,
          status: run.status,
          version: run.version,
          trigger: run.trigger,
          started_at: run.started_at,
          finished_at: run.finished_at,
          error_text: run.error_text,
        })),
          total: runs.length,
        }),
      );
    }
    match = path.match(/^\/api\/v1\/pipelines\/(\d+)\/runs\/(\d+)$/);
    if (match && method === "GET") {
      const runId = Number(match[2]);
      const pipeline = ownPipeline(Number(match[1]), customerId);
      if (!pipeline) {
        return Promise.resolve(
          jsonResponse({ detail: "pipeline not found" }, 404),
        );
      }
      const run = (pipelineRunsStore.get(pipeline.id) ?? []).find(
        (entry) => entry.id === runId,
      );
      if (!run) {
        return Promise.resolve(jsonResponse({ detail: "run not found" }, 404));
      }
      return Promise.resolve(jsonResponse(run));
    }
    match = path.match(/^\/api\/v1\/pipelines\/(\d+)\/schedule$/);
    if (match) {
      const pipeline = ownPipeline(Number(match[1]), customerId);
      if (!pipeline) {
        return Promise.resolve(
          jsonResponse({ detail: "pipeline not found" }, 404),
        );
      }
      const scheduleOut = () =>
        pipeline.schedule ?? {
          cron: null,
          enabled: false,
          last_run_at: null,
          last_status: null,
        };
      if (method === "GET") {
        return Promise.resolve(jsonResponse(scheduleOut()));
      }
      if (options.failPipelineScheduleStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `pipeline schedule ${options.failPipelineScheduleStatus}` },
            options.failPipelineScheduleStatus,
          ),
        );
      }
      if (isViewer(customerId)) return Promise.resolve(viewerDenied());
      if (method === "PUT") {
        const payload = (calls[calls.length - 1]!.body ?? {}) as {
          cron?: unknown;
        };
        const cron = typeof payload.cron === "string" ? payload.cron.trim() : "";
        if (cron.split(/\s+/).filter(Boolean).length !== 5) {
          return Promise.resolve(
            jsonResponse(
              { detail: "cron 须为五字段（分 时 日 月 周，UTC）" },
              422,
            ),
          );
        }
        pipeline.schedule = {
          cron,
          enabled: true,
          last_run_at: pipeline.schedule?.last_run_at ?? null,
          last_status: pipeline.schedule?.last_status ?? null,
        };
        return Promise.resolve(jsonResponse({ ...pipeline.schedule }));
      }
      if (method === "DELETE") {
        pipeline.schedule = null;
        return Promise.resolve(jsonResponse({ removed: true }));
      }
    }
    match = path.match(
      /^\/api\/v1\/pipelines\/(\d+)\/schedule\/(pause|resume)$/,
    );
    if (match && method === "POST") {
      const pipeline = ownPipeline(Number(match[1]), customerId);
      if (!pipeline) {
        return Promise.resolve(
          jsonResponse({ detail: "pipeline not found" }, 404),
        );
      }
      if (options.failPipelineScheduleStatus) {
        return Promise.resolve(
          jsonResponse(
            { detail: `pipeline schedule ${options.failPipelineScheduleStatus}` },
            options.failPipelineScheduleStatus,
          ),
        );
      }
      if (isViewer(customerId)) return Promise.resolve(viewerDenied());
      if (!pipeline.schedule) {
        return Promise.resolve(
          jsonResponse({ detail: "尚未挂接调度" }, 400),
        );
      }
      pipeline.schedule.enabled = match[2] === "resume";
      return Promise.resolve(jsonResponse({ ...pipeline.schedule }));
    }

    return Promise.resolve(jsonResponse({ detail: "not found" }, 404));
  }) as unknown as typeof fetch;

  return {
    calls,
    fetchImpl,
    addMessage,
    addArtifact,
    addLifecycleArtifact,
    addDataset,
    addConnector,
    addRows,
    addCorrection,
    addSync,
    addRefreshSchedule,
    getDatasets: () => [...datasets.values()],
    getAssetMeta: (datasetId: string) => {
      const row = assetMetaStore.get(datasetId);
      return row
        ? ({ ...row } as Record<string, unknown>)
        : {
            domain: null,
            tags: [],
            description: null,
            cadence_hint: null,
            owner_note: null,
            sensitivity: "常规",
            cadence_effective: null,
          };
    },
    getAssetMetaEvents: (datasetId: string) =>
      (assetEventsStore.get(datasetId) ?? []).map((event) => ({ ...event })),
    getCorrections: (datasetId: string) => corrections.get(datasetId) ?? [],
    getPackSubscriptions: (customerId = "demo-customer") =>
      [...(packSubscriptions.get(customerId) ?? [])],
    getEntities: (): FakeEntityRow[] =>
      [...entitiesStore.values()].map((row) => ({ ...row })),
    getEntityRelationships: (): FakeEntityRelationshipRow[] =>
      [...entityRelationshipsStore.values()].map((row) => ({ ...row })),
    getTeamMembers: (): FakeTeamMemberRow[] =>
      [...teamMembersStore.values()].map((row) => ({ ...row })),
    getTeamInvites: (): FakeTeamInviteRow[] =>
      [...teamInvitesStore.values()].map((row) => ({ ...row })),
    getTeams: () =>
      teamSeedsStore.map((seed) => ({
        customer_id: seed.customer_id,
        customer_name: seed.customer_name,
        role: seed.role,
        is_active: seed.customer_id === activeTeamId,
      })),
    getActiveTeamId: () => activeTeamId,
    getAlertEvents: () => [...alertEventsStore.values()].map((row) => ({ ...row })),
    getAlertStates: (): Record<string, FakeAlertStateSeed> =>
      Object.fromEntries([...alertStatesStore.entries()].map(([k, v]) => [k, { ...v }])),
    getPreferences: (sub = "demo-customer"): Required<FakePreferencesSeed> => {
      const row = preferencesStore.get(sub);
      return row
        ? { ...row }
        : { ui_locale: null, landing_tab: null, pinned_dashboard_id: null };
    },
    /** 管线断言面：当前内存态（版本/调度）与运行历史（倒序）。 */
    getPipelines: () =>
      [...pipelinesStore.values()].map((pipeline) => ({
        id: pipeline.id,
        name: pipeline.name,
        current_version: pipeline.versions.length,
        versions: pipeline.versions.map((v) => ({
          version: v.version,
          steps: v.steps,
        })),
        schedule: pipeline.schedule ? { ...pipeline.schedule } : null,
      })),
    getPipelineRuns: (pipelineId: number): PipelineRunSummary[] =>
      (pipelineRunsStore.get(pipelineId) ?? []).map((run) => ({
        id: run.id,
        status: run.status,
        version: run.version,
        trigger: run.trigger,
        started_at: run.started_at,
        finished_at: run.finished_at,
        error_text: run.error_text,
      })),
    addAlertEvent,
    /** 可见性行断言面（wire-dataset-marketplace；wire snake_case 形状）。 */
    getDatasetVisibility: (datasetId: string) => ({ ...visibilityOf(datasetId) }),
    /** 定向授权名单断言面。 */
    getDatasetGrants: (datasetId: string) => [...(grantsStore.get(datasetId) ?? [])],
    /** 数据集级访问授权断言面（wire-dataset-acl；wire 形状）。 */
    getDatasetAccess: (datasetId: string) => aclOf(datasetId),
    /** 市场订阅断言面（customerId → dataset ids；缺省种子首客户）。 */
    getMarketSubscriptions: (cid: string = "demo-customer") =>
      [...(marketSubscriptions.get(cid) ?? [])],
    /** 准入申请断言面。 */
    getAccessRequests: () =>
      [...accessRequestsStore.values()].map((row) => ({ ...row })),
    /** doc 草稿层断言面（wire-rich-text-docs 6.2）：无行回 null。 */
    getDocDraft: (artifactId: number) => {
      const row = draftStore.get(artifactId);
      return row ? { ...row } : null;
    },
  };
}

/** 任意请求一律失败的后端（错误态测试）。 */
export function consoleFailingBackend(
  status = 500,
  detail = "boom",
): typeof fetch {
  return (() =>
    Promise.resolve(jsonResponse({ detail }, status))) as unknown as typeof fetch;
}

// ---------------------------------------------------------------------------
// 登录会话测试件（wire-self-serve-launch 4.4/4.6/5.2/6.2）
// ---------------------------------------------------------------------------

function b64urlJson(value: unknown): string {
  return btoa(JSON.stringify(value))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

/** 造测试 ID token（JWT 形状，payload base64url；签名段无意义——校验在服务端）。 */
export function makeIdToken(
  claims: { sub?: string; roles?: string[]; email?: string; name?: string } = {},
): string {
  const payload = {
    sub: claims.sub ?? "demo-customer",
    ...(claims.roles ? { roles: claims.roles } : {}),
    ...(claims.email ? { email: claims.email } : {}),
    ...(claims.name ? { name: claims.name } : {}),
  };
  return `${b64urlJson({ alg: "HS256", typ: "JWT" })}.${b64urlJson(payload)}.test-signature`;
}

/** 从请求头解出 Bearer token 的 sub claim（即客户标识；无/坏 token → null）。 */
export function subOfBearer(headers: Record<string, string>): string | null {
  const authorization = headers["authorization"] ?? "";
  const match = authorization.match(/^Bearer (.+)$/);
  if (!match) return null;
  try {
    const payload = match[1]!.split(".")[1];
    if (!payload) return null;
    const padded = payload.replace(/-/g, "+").replace(/_/g, "/");
    const binary = atob(
      padded.padEnd(padded.length + ((4 - (padded.length % 4)) % 4), "="),
    );
    const claims = JSON.parse(new TextDecoder().decode(Uint8Array.from(binary, (c) => c.charCodeAt(0)))) as {
      sub?: string;
    };
    return typeof claims.sub === "string" && claims.sub ? claims.sub : null;
  } catch {
    return null;
  }
}

/**
 * 种子登录会话（sessionStorage）：idToken = makeIdToken(claims)，
 * customer 回填 plan_code（免费档门禁/用量页测试用）。返回会话对象便于断言。
 */
export function seedConsoleSession(
  overrides: {
    sub?: string;
    roles?: string[];
    email?: string;
    planCode?: string;
    customerName?: string;
    idToken?: string;
  } = {},
): ConsoleSession {
  const idToken = overrides.idToken ?? makeIdToken(overrides);
  const session: ConsoleSession = {
    idToken,
    expiresAt: Date.now() + 3600_000,
    identity: overrides.email ?? overrides.sub ?? "demo-customer",
    email: overrides.email,
    roles: overrides.roles ?? [],
    customer: {
      id: 1,
      name: overrides.customerName ?? "演示客户",
      plan_code: overrides.planCode ?? "basic",
    },
  };
  saveSession(session);
  return session;
}
