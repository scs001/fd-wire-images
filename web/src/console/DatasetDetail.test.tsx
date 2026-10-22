// 数据集详情行内编辑器测试（wire-dataset-form-editing 任务 2.1/2.2）：
// 模式切换与只读默认、双击改值/删行/加行入脏队列（Esc 取消）、保存按入队
// 顺序逐条提交与失败隔离（成功条目生效、失败带行上下文+服务端原因、不重试）、
// 未保存离开确认与放弃清队、保存成功后刷新预览与修正历史、cell/api 两档同 UI
// （mock 端点同契约）。编辑器纯逻辑（editQueue 队列归约/顺序保存）单测见文末。
// CSV 导出测试（wire-dataset-export 任务 2.2/2.3）：Bearer 下载触发、blob 下载
// 流程（createObjectURL 桩）、截断提示、失败如实、下载中防重复。
// 重派生与定时重派生测试（wire-pipeline-actions 任务 1.2/1.3）：POST /refresh
// （Bearer+cascade 参数）与级联结果呈现、完成后详情刷新、cron 登记/更新/
// 态呈现/删除、失败如实、api 档与上传集无入口。
// 增量同步设置测试（wire-incremental-sync）：连接器门禁（其他来源不渲染）、
// 切增量+选水位列保存的 PATCH 载荷、未选水位列保存禁用、重置水位二次确认
// （{reset_watermark: true}）、422 detail 如实、长水位值截断+title、双语文案。
// fetch 为本文件内置契约假后端（零网络，不改 testing.ts——DatasetProvenance 手法）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { DatasetDetail } from "./DatasetDetail";
import {
  describeEditItem,
  enqueueEdit,
  saveEditQueue,
  type EditQueueItem,
} from "./editQueue";
import { formatServerDate } from "./util";
import type {
  ApiDetailItem,
  Correction,
  CorrectionInput,
  Dataset,
  DatasetColumn,
  DatasetRefreshCascadeEntry,
  ProvenanceOverview,
  ProvenanceStatus,
} from "./api";
import { makeIdToken } from "./testing";

const CUSTOMER = "demo-customer";
const NOW = "2026-10-05T10:00:00";
const KEY = "ds_key_1";
const ROWS_PATH = "/api/v1/datasets/ds-1/rows";
const CORRECTIONS_PATH = "/api/v1/datasets/ds-1/corrections";
const EXPORT_PATH = "/api/v1/datasets/ds-1/export";
const REFRESH_PATH = "/api/v1/datasets/ds-1/refresh";
const SCHEDULE_PATH = "/api/v1/datasets/ds-1/schedule";
const SYNC_SETTINGS_PATH = "/api/v1/datasets/ds-1/sync-settings";

const COLUMNS: DatasetColumn[] = [
  { name: "id", type: "INTEGER" },
  { name: "amount", type: "REAL" },
];

function makeDataset(partial: Partial<Dataset> = {}): Dataset {
  return {
    id: "ds-1",
    key: KEY,
    name: "门店销量",
    source_kind: "upload",
    connector_id: null,
    source_ref: null,
    columns: COLUMNS,
    row_count: 2,
    size_bytes: 64,
    sync_cron: null,
    sync_status: "ok",
    paused_reason: null,
    // wire-incremental-sync：详情响应三字段（缺省全量快照、无水位）。
    sync_mode: "snapshot",
    watermark_column: null,
    watermark_value: null,
    created_at: NOW,
    updated_at: NOW,
    customer_id: CUSTOMER,
    ...partial,
  };
}

/** 连接器来源测试集（增量同步卡片门禁用；水位列候选含 updated_at）。 */
const CONNECTOR_COLUMNS: DatasetColumn[] = [
  ...COLUMNS,
  { name: "updated_at", type: "TEXT" },
];

function makeConnectorDataset(partial: Partial<Dataset> = {}): Dataset {
  return makeDataset({
    source_kind: "connector",
    connector_id: "cn-1",
    source_ref: "erp_db",
    columns: CONNECTOR_COLUMNS,
    ...partial,
  });
}

function makeStatus(): ProvenanceStatus {
  return {
    tier: "chain_only",
    integrity: { ok: true, checked: 1 },
    anchored_through: { epoch: null, backend: null },
    first_event_ts: null,
    note: null,
  };
}

function makeOverview(): ProvenanceOverview {
  return {
    ledger: "wire-provenance-v1",
    last_seq: 1,
    integrity: { ok: true, checked: 1 },
    anchoring: { backend: null, last_sealed_epoch: null, lag_events: 0 },
  };
}

type FetchCall = {
  method: string;
  url: URL;
  headers: Record<string, string>;
  body?: unknown;
};

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
    headers: new Headers(),
  } as unknown as Response;
}

/** CSV 导出响应（wire-dataset-export 契约：text/csv + BOM、
 * Content-Disposition 文件名、可选 X-Export-Truncated: true）。 */
function csvResponse(options: { truncated?: boolean } = {}): Response {
  const headers = new Headers({
    "content-type": "text/csv; charset=utf-8",
    "content-disposition": `attachment; filename="${KEY}.csv"`,
  });
  if (options.truncated) headers.set("x-export-truncated", "true");
  return {
    ok: true,
    status: 200,
    json: () => Promise.reject(new Error("csv body")),
    blob: () =>
      Promise.resolve(new Blob(["\uFEFFid,amount\n1,10\n"], { type: "text/csv" })),
    headers,
  } as unknown as Response;
}

/** 契约假后端：数据集详情常规路由 + 修正三件套（POST 落层并应用，语义对齐
 * testing.ts）+ 可注入第 N 条 POST 失败（失败隔离测试）。 */
function install(options: {
  dataset?: Dataset;
  rows?: Array<Record<string, unknown>>;
  correctionsSeed?: Correction[];
  failNthCorrectionPost?: { nth: number; status: number; detail: string };
  /** 导出端点强制失败（如 500 带原因）。 */
  failExportStatus?: number;
  failExportDetail?: string;
  /** 导出响应带 X-Export-Truncated: true。 */
  exportTruncated?: boolean;
  /** 导出请求悬挂至该 promise 完成（防重复点击测试）。 */
  holdExport?: Promise<void>;
  /** 重派生级联结果注入（wire-pipeline-actions；缺省空级联）。 */
  cascade?: DatasetRefreshCascadeEntry[];
  /** 强制 POST /datasets/{id}/refresh 返回该状态（失败如实测试）。 */
  failRefreshStatus?: number;
  failRefreshDetail?: string;
  /** 重派生请求悬挂至该 promise 完成（进行中禁用防重复测试）。 */
  holdRefresh?: Promise<void>;
  /** 定时重派生登记种子（GET/PUT/DELETE 内存维护；不传即未登记）。 */
  scheduleSeed?: {
    cron: string;
    enabled?: boolean;
    last_run_at?: string | null;
    last_status?: string | null;
    last_error?: string | null;
    last_rows?: number | null;
  };
  /** 强制 PUT /datasets/{id}/schedule 返回该状态（非法 cron 等测试）。 */
  failSchedulePutStatus?: number;
  failSchedulePutDetail?: string;
  /** 强制 PATCH /datasets/{id}/sync-settings 返回该状态（422 回显测试）。 */
  failSyncSettingsStatus?: number;
  failSyncSettingsDetail?: string;
  /** 资产元数据种子（wire-asset-catalog 5.2；缺省全默认：常规/无域无标签）。 */
  assetMeta?: {
    domain?: string | null;
    tags?: string[];
    description?: string | null;
    cadence_hint?: "daily" | "weekly" | "monthly" | null;
    owner_note?: string | null;
    sensitivity?: "常规" | "内部" | "敏感";
    cadence_effective?: "daily" | "weekly" | "monthly" | null;
  };
  /** 种子资产元数据留痕（最近在前；PATCH 会续记在最前）。 */
  assetMetaEvents?: Array<{
    at: string;
    actor_sub: string;
    before: Record<string, unknown> | null;
    after: Record<string, unknown> | null;
  }>;
  /** 强制 GET /datasets/{id}/asset-meta 返回该状态。 */
  failAssetMetaStatus?: number;
  /** 强制 PATCH /datasets/{id}/asset-meta 返回该状态（422/403 注入；detail
   *  可为字符串或 FastAPI 校验数组形状）。 */
  failAssetMetaPatchStatus?: number;
  failAssetMetaPatchDetail?: string | ApiDetailItem[];
  /** 数据集质量种子（wire-dataset-quality 5.3；缺省=暂无快照诚实空态）。 */
  quality?: {
    latest?: Record<string, unknown> | null;
    history?: Array<Record<string, unknown>>;
    /** 对象 {freshness,last_refresh_at} 或裸枚举字符串。 */
    freshness?: unknown;
    cadence_hint?: "daily" | "weekly" | "monthly" | null;
  };
  /** 强制 GET /datasets/{id}/quality 返回该状态。 */
  failQualityStatus?: number;
  /** 强制 POST /datasets/{id}/quality/refresh 返回该状态。 */
  failQualityRefreshStatus?: number;
  failQualityRefreshDetail?: string;
  /** 质量刷新请求悬挂至该 promise 完成（进行中禁用测试）。 */
  holdQualityRefresh?: Promise<void>;
}) {
  const dataset = options.dataset ?? makeDataset();
  let rows = (options.rows ?? [
    { id: 1, amount: 10 },
    { id: 2, amount: 20 },
  ]).map((row, i) => ({
    ...row,
    __row_id: row.__row_id ?? `hash:seed-${i + 1}`,
  }));
  const corrections: Correction[] = [...(options.correctionsSeed ?? [])];
  const calls: FetchCall[] = [];
  let nextCorrectionId = 1;
  let correctionPostCount = 0;
  // 定时重派生登记内存态（唯一行 per 数据集——对齐 api materialization 调度行）。
  let schedule: {
    cron: string;
    enabled: boolean;
    last_run_at: string | null;
    last_status: string | null;
    last_error: string | null;
    last_rows: number | null;
  } | null = options.scheduleSeed
    ? {
        cron: options.scheduleSeed.cron,
        enabled: options.scheduleSeed.enabled ?? true,
        last_run_at: options.scheduleSeed.last_run_at ?? null,
        last_status: options.scheduleSeed.last_status ?? null,
        last_error: options.scheduleSeed.last_error ?? null,
        last_rows: options.scheduleSeed.last_rows ?? null,
      }
    : null;

  // ── 资产元数据 + 质量假后端状态（wire-asset-catalog 5.2 /
  //    wire-dataset-quality 5.3）：元数据懒合并（PATCH 全量六字段）、留痕
  //    append-only（续记最前）、质量种子直存（refresh 追加 ok 快照）。──
  type AssetMetaRow = {
    domain: string | null;
    tags: string[];
    description: string | null;
    cadence_hint: "daily" | "weekly" | "monthly" | null;
    owner_note: string | null;
    sensitivity: "常规" | "内部" | "敏感";
    cadence_effective: "daily" | "weekly" | "monthly" | null;
  };
  let assetMeta: AssetMetaRow = {
    domain: options.assetMeta?.domain ?? null,
    tags: options.assetMeta?.tags ? [...options.assetMeta.tags] : [],
    description: options.assetMeta?.description ?? null,
    cadence_hint: options.assetMeta?.cadence_hint ?? null,
    owner_note: options.assetMeta?.owner_note ?? null,
    sensitivity: options.assetMeta?.sensitivity ?? "常规",
    cadence_effective: options.assetMeta?.cadence_effective ?? null,
  };
  const assetEvents: Array<{
    at: string;
    actor_sub: string;
    before: Record<string, unknown> | null;
    after: Record<string, unknown> | null;
  }> = [...(options.assetMetaEvents ?? [])];
  let qualityState: {
    latest: Record<string, unknown> | null;
    history: Array<Record<string, unknown>>;
    freshness: unknown;
    cadence_hint: "daily" | "weekly" | "monthly" | null;
  } = {
    latest: options.quality?.latest ?? null,
    history: options.quality?.history ? [...options.quality.history] : [],
    freshness: options.quality?.freshness ?? {
      freshness: "unknown",
      last_refresh_at: null,
    },
    cadence_hint: options.quality?.cadence_hint ?? null,
  };

  const fetchImpl = ((_input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(_input), "http://console.test");
    const method = (init?.method ?? "GET").toUpperCase();
    const headers: Record<string, string> = {};
    if (init?.headers) {
      new Headers(init.headers as HeadersInit).forEach((v, k) => {
        headers[k.toLowerCase()] = v;
      });
    }
    calls.push({
      method,
      url,
      headers,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    });

    const path = url.pathname;
    if (path === "/api/v1/datasets/ds-1" && method === "GET") {
      return jsonResponse(dataset);
    }
    if (path === ROWS_PATH && method === "GET") {
      return jsonResponse({ columns: COLUMNS, rows, total: rows.length });
    }
    if (path === "/api/v1/datasets/ds-1/syncs" && method === "GET") {
      return jsonResponse([]);
    }
    if (path === CORRECTIONS_PATH && method === "GET") {
      return jsonResponse(corrections);
    }
    if (path === CORRECTIONS_PATH && method === "POST") {
      correctionPostCount += 1;
      const payload = calls[calls.length - 1]!.body as CorrectionInput;
      const fail = options.failNthCorrectionPost;
      if (fail && correctionPostCount === fail.nth) {
        return jsonResponse({ detail: fail.detail }, fail.status);
      }
      const id = nextCorrectionId++;
      corrections.push({
        id,
        dataset_id: "ds-1",
        op: payload.op,
        row_id: "row_id" in payload ? payload.row_id : null,
        column: payload.op === "update" ? payload.column : null,
        value: payload.op === "update" ? payload.value : null,
        values: payload.op === "insert" ? payload.values : null,
        actor: "console-test",
        created_at: "2026-10-11T09:00:00",
        reverted_at: null,
        dangling_at: null,
        dangling_reason: null,
      });
      // 应用修正（假后端内存态，供保存后刷新断言）。
      if (payload.op === "update") {
        rows = rows.map((row) =>
          row.__row_id === payload.row_id
            ? { ...row, [payload.column]: payload.value }
            : row,
        );
      } else if (payload.op === "delete") {
        rows = rows.filter((row) => row.__row_id !== payload.row_id);
      } else {
        rows = [...rows, { ...payload.values, __row_id: `ins:${id}` }];
      }
      return jsonResponse({ correction_id: id }, 201);
    }
    if (path === EXPORT_PATH && method === "GET") {
      if (options.holdExport) {
        return options.holdExport.then(() =>
          csvResponse({ truncated: options.exportTruncated }),
        ) as unknown as Response;
      }
      if (options.failExportStatus) {
        return jsonResponse(
          { detail: options.failExportDetail ?? `export ${options.failExportStatus}` },
          options.failExportStatus,
        );
      }
      return csvResponse({ truncated: options.exportTruncated });
    }
    if (path === `/api/v1/provenance/datasets/${KEY}/status`) {
      return jsonResponse(makeStatus());
    }
    if (path === `/api/v1/provenance/datasets/${KEY}/events`) {
      return jsonResponse({ events: [], has_more: false });
    }
    if (path === "/api/v1/provenance/overview") {
      return jsonResponse(makeOverview());
    }
    // ── 重派生（wire-pipeline-actions 1.2 契约）：POST /refresh?cascade=——
    //    本体推进行数/更新时间（详情刷新断言），级联按注入返回。──
    if (path === REFRESH_PATH && method === "POST") {
      if (options.holdRefresh) {
        return options.holdRefresh.then(() =>
          jsonResponse({ ...dataset, cascade: [] }),
        ) as unknown as Response;
      }
      if (options.failRefreshStatus) {
        return jsonResponse(
          {
            detail:
              options.failRefreshDetail ?? `refresh ${options.failRefreshStatus}`,
          },
          options.failRefreshStatus,
        );
      }
      const cascadeOn = url.searchParams.get("cascade") !== "false";
      dataset.row_count += 10;
      dataset.updated_at = "2026-10-11T10:30:00";
      return jsonResponse({
        ...dataset,
        cascade: cascadeOn ? (options.cascade ?? []) : [],
      });
    }
    // ── 定时重派生（wire-pipeline-actions 1.2 契约）：GET/PUT/DELETE
    //    /schedule——唯一登记行内存维护；PUT 五字段轻校验（对齐 parse_cron）。──
    if (path === SCHEDULE_PATH && method === "GET") {
      if (!schedule) {
        return jsonResponse({ dataset_id: "ds-1", cron: null, enabled: false });
      }
      return jsonResponse({ dataset_id: "ds-1", ...schedule });
    }
    if (path === SCHEDULE_PATH && method === "PUT") {
      if (options.failSchedulePutStatus) {
        return jsonResponse(
          {
            detail:
              options.failSchedulePutDetail ??
              `schedule ${options.failSchedulePutStatus}`,
          },
          options.failSchedulePutStatus,
        );
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as { cron?: string };
      const fields = (payload.cron ?? "").trim().split(/\s+/);
      if (!payload.cron || fields.length !== 5) {
        return jsonResponse(
          { detail: "cron 须为五字段表达式（分 时 日 月 周）" },
          400,
        );
      }
      schedule = schedule
        ? { ...schedule, cron: payload.cron, enabled: true }
        : {
            cron: payload.cron,
            enabled: true,
            last_run_at: null,
            last_status: null,
            last_error: null,
            last_rows: null,
          };
      return jsonResponse({ dataset_id: "ds-1", ...schedule });
    }
    if (path === SCHEDULE_PATH && method === "DELETE") {
      schedule = null;
      return jsonResponse({ dataset_id: "ds-1", removed: true });
    }
    // ── 增量同步设置（wire-incremental-sync 契约镜像）：PATCH
    //    /sync-settings——非连接器 422、水位列必须 ∈ 列清单、增量必须配
    //    水位列、切回 snapshot 清水位字段、reset_watermark 只清水位值；
    //    成功返回更新后数据集（详情刷新断言）。──
    if (path === SYNC_SETTINGS_PATH && method === "PATCH") {
      if (options.failSyncSettingsStatus) {
        return jsonResponse(
          {
            detail:
              options.failSyncSettingsDetail ??
              `sync settings ${options.failSyncSettingsStatus}`,
          },
          options.failSyncSettingsStatus,
        );
      }
      if (dataset.source_kind !== "connector") {
        return jsonResponse(
          { detail: "仅连接器（connector）来源数据集支持增量同步设置" },
          422,
        );
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as {
        sync_mode?: string;
        watermark_column?: string;
        reset_watermark?: boolean;
      };
      if (payload.reset_watermark === true) {
        dataset.watermark_value = null;
        return jsonResponse(dataset);
      }
      const nextMode = payload.sync_mode ?? dataset.sync_mode ?? "snapshot";
      if (nextMode === "incremental") {
        const column = payload.watermark_column ?? dataset.watermark_column ?? "";
        if (!column) {
          return jsonResponse(
            { detail: "增量模式必须配置水位列（watermark_column）" },
            422,
          );
        }
        if (!dataset.columns.some((c) => c.name === column)) {
          return jsonResponse(
            { detail: `水位列 ${column} 不在数据集列清单中` },
            422,
          );
        }
        dataset.sync_mode = "incremental";
        dataset.watermark_column = column;
      } else {
        dataset.sync_mode = "snapshot";
        dataset.watermark_column = null;
        dataset.watermark_value = null;
      }
      return jsonResponse(dataset);
    }
    // ── 资产元数据（wire-asset-catalog 5.2 契约镜像）：GET/PATCH /asset-meta
    //    + GET /asset-meta/events——敏感度三值 422 回合法清单、PATCH 续留痕。──
    if (path === "/api/v1/datasets/ds-1/asset-meta" && method === "GET") {
      if (options.failAssetMetaStatus) {
        return jsonResponse(
          { detail: `asset meta ${options.failAssetMetaStatus}` },
          options.failAssetMetaStatus,
        );
      }
      return jsonResponse({ dataset_id: "ds-1", ...assetMeta });
    }
    if (path === "/api/v1/datasets/ds-1/asset-meta" && method === "PATCH") {
      if (options.failAssetMetaPatchStatus) {
        return jsonResponse(
          {
            detail:
              options.failAssetMetaPatchDetail ??
              `asset meta ${options.failAssetMetaPatchStatus}`,
          },
          options.failAssetMetaPatchStatus,
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
        return jsonResponse(
          { detail: "敏感度只能是：常规 / 内部 / 敏感" },
          422,
        );
      }
      const before: Record<string, unknown> = { ...assetMeta };
      assetMeta = {
        domain: payload.domain !== undefined ? payload.domain : assetMeta.domain,
        tags: payload.tags !== undefined ? payload.tags : assetMeta.tags,
        description:
          payload.description !== undefined
            ? payload.description
            : assetMeta.description,
        cadence_hint:
          payload.cadence_hint !== undefined
            ? (payload.cadence_hint as AssetMetaRow["cadence_hint"])
            : assetMeta.cadence_hint,
        owner_note:
          payload.owner_note !== undefined
            ? payload.owner_note
            : assetMeta.owner_note,
        sensitivity:
          payload.sensitivity !== undefined
            ? (payload.sensitivity as AssetMetaRow["sensitivity"])
            : assetMeta.sensitivity,
        cadence_effective: assetMeta.cadence_effective,
      };
      assetEvents.unshift({
        at: "2026-10-13T09:00:00",
        actor_sub: "demo-customer",
        before,
        after: { ...assetMeta },
      });
      return jsonResponse({ dataset_id: "ds-1", ...assetMeta });
    }
    if (
      path === "/api/v1/datasets/ds-1/asset-meta/events" &&
      method === "GET"
    ) {
      return jsonResponse({ items: assetEvents });
    }

    // ── 数据质量（wire-dataset-quality 5.3 契约镜像）：GET /quality（种子或
    //    暂无快照空态）+ POST /quality/refresh（追加 ok 快照，可悬挂/失败注入）。──
    if (path === "/api/v1/datasets/ds-1/quality" && method === "GET") {
      if (options.failQualityStatus) {
        return jsonResponse(
          { detail: `quality ${options.failQualityStatus}` },
          options.failQualityStatus,
        );
      }
      return jsonResponse(qualityState);
    }
    if (path === "/api/v1/datasets/ds-1/quality/refresh" && method === "POST") {
      const respond = () => {
        if (options.failQualityRefreshStatus) {
          return jsonResponse(
            {
              detail:
                options.failQualityRefreshDetail ??
                `quality refresh ${options.failQualityRefreshStatus}`,
            },
            options.failQualityRefreshStatus,
          );
        }
        const snapshot: Record<string, unknown> = {
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
        const history = [...qualityState.history];
        if (qualityState.latest) {
          history.push({
            computed_at: qualityState.latest.computed_at,
            row_count: qualityState.latest.row_count,
            null_rates: qualityState.latest.null_rates ?? [],
          });
        }
        history.push({
          computed_at: snapshot.computed_at,
          row_count: snapshot.row_count,
          null_rates: [],
        });
        qualityState = { ...qualityState, latest: snapshot, history };
        return jsonResponse(snapshot, 201);
      };
      if (options.holdQualityRefresh) {
        return options.holdQualityRefresh.then(
          respond,
        ) as unknown as Response;
      }
      return respond();
    }

    return jsonResponse({ detail: "not found" }, 404);
  }) as unknown as typeof fetch;
  vi.stubGlobal("fetch", fetchImpl);
  return {
    calls,
    getRows: () => rows,
    getCorrections: () => corrections,
    getSchedule: () => schedule,
  };
}

function callsOf(calls: FetchCall[], method: string, path: string): FetchCall[] {
  return calls.filter((call) => call.method === method && call.url.pathname === path);
}

/** 定位预览单元格（列名 + 行锚）。 */
function findCell(column: string, rowId: string) {
  return screen
    .getAllByTestId("preview-cell")
    .find(
      (el) =>
        el.getAttribute("data-column") === column &&
        el.getAttribute("data-row-id") === rowId,
    );
}

/** 进入编辑模式（等预览网格就绪后）。 */
async function enterEditMode() {
  await screen.findByTestId("preview-grid");
  fireEvent.click(screen.getByTestId("preview-edit-toggle"));
}

/** 双击改值并 Enter 入队。 */
function editCell(rowId: string, column: string, value: string) {
  fireEvent.doubleClick(findCell(column, rowId)!);
  const input = screen.getByTestId("preview-edit-input");
  fireEvent.change(input, { target: { value } });
  fireEvent.keyDown(input, { key: "Enter" });
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  // 登录会话种子：请求自动附 Authorization: Bearer（假后端不校验值）。
  sessionStorage.setItem(
    "wire.console.session",
    JSON.stringify({
      idToken: makeIdToken({ sub: CUSTOMER }),
      expiresAt: Date.now() + 3600_000,
      identity: CUSTOMER,
      roles: [],
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

describe("DatasetDetail 行内编辑器：模式切换与只读默认（任务 2.1）", () => {
  it("默认只读：无删行/加行/脏徽标控件，双击不开编辑框；切换进编辑后控件出现", async () => {
    install({});
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    await screen.findByTestId("preview-grid");
    const toggle = screen.getByTestId("preview-edit-toggle");
    expect(toggle.getAttribute("data-mode")).toBe("read");
    expect(toggle.textContent).toBe("编辑");
    // 只读态零编辑控件
    expect(screen.queryByTestId("preview-delete-row")).toBeNull();
    expect(screen.queryByTestId("preview-add-row")).toBeNull();
    expect(screen.queryByTestId("preview-dirty-badge")).toBeNull();
    fireEvent.doubleClick(findCell("amount", "hash:seed-1")!);
    expect(screen.queryByTestId("preview-edit-input")).toBeNull();

    // 切换进编辑：删行/加行出现，双击可开编辑框（Esc 取消）
    fireEvent.click(toggle);
    expect(toggle.getAttribute("data-mode")).toBe("edit");
    expect(screen.getAllByTestId("preview-delete-row").length).toBe(2);
    expect(screen.getByTestId("preview-add-row")).toBeTruthy();
    fireEvent.doubleClick(findCell("amount", "hash:seed-1")!);
    const input = screen.getByTestId("preview-edit-input");
    expect((input as HTMLInputElement).value).toBe("10");
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByTestId("preview-edit-input")).toBeNull();
    expect(screen.queryByTestId("preview-dirty-badge")).toBeNull();
  });
});

describe("DatasetDetail 行内编辑器：脏队列入队（任务 2.1）", () => {
  it("双击改值 Enter 入队（Esc 取消不入队）；行删除与添加行入队——均不即时提交", async () => {
    const backend = install({});
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await enterEditMode();

    // Esc 取消：不入队
    fireEvent.doubleClick(findCell("amount", "hash:seed-1")!);
    fireEvent.change(screen.getByTestId("preview-edit-input"), {
      target: { value: "77" },
    });
    fireEvent.keyDown(screen.getByTestId("preview-edit-input"), { key: "Escape" });
    expect(screen.queryByTestId("preview-dirty-badge")).toBeNull();

    // Enter 确认：update 入队（徽标 1，未发 POST）
    editCell("hash:seed-1", "amount", "99");
    expect(screen.getByTestId("preview-dirty-badge").textContent).toContain("1");
    expect(callsOf(backend.calls, "POST", CORRECTIONS_PATH).length).toBe(0);

    // 行删除：delete 入队（徽标 2）
    fireEvent.click(screen.getAllByTestId("preview-delete-row")[1]!);
    expect(screen.getByTestId("preview-dirty-badge").textContent).toContain("2");

    // 添加行：内联表单按列输入（可留空）→ insert 入队（徽标 3）
    fireEvent.click(screen.getByTestId("preview-add-row"));
    const amountInput = screen
      .getAllByTestId("preview-add-row-input")
      .find((el) => el.getAttribute("data-column") === "amount");
    fireEvent.change(amountInput!, { target: { value: "5" } });
    fireEvent.click(screen.getByTestId("preview-add-row-submit"));
    expect(screen.getByTestId("preview-dirty-badge").textContent).toContain("3");
    expect(callsOf(backend.calls, "POST", CORRECTIONS_PATH).length).toBe(0);

    // 队列叠加可视化：待保存 update 单元格标注、delete 行划线
    expect(findCell("amount", "hash:seed-1")!.getAttribute("data-pending-edit")).toBe(
      "true",
    );
    expect(
      screen
        .getAllByTestId("preview-row")
        .find((el) => el.getAttribute("data-row-id") === "hash:seed-2")!
        .getAttribute("data-pending-delete"),
    ).toBe("true");
  });
});

describe("DatasetDetail 行内编辑器：保存顺序与失败隔离（任务 2.2）", () => {
  it("保存按入队顺序逐条提交；第 2 条 400：第 1 条已生效、失败条目带原因与行上下文、第 3 条仍提交", async () => {
    const backend = install({
      failNthCorrectionPost: {
        nth: 2,
        status: 400,
        detail: "未知列 nosuch（合法列：id, amount）",
      },
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await enterEditMode();

    editCell("hash:seed-1", "amount", "99"); // 1: update
    fireEvent.click(screen.getAllByTestId("preview-delete-row")[1]!); // 2: delete
    fireEvent.click(screen.getByTestId("preview-add-row"));
    const amountInput = screen
      .getAllByTestId("preview-add-row-input")
      .find((el) => el.getAttribute("data-column") === "amount");
    fireEvent.change(amountInput!, { target: { value: "5" } });
    fireEvent.click(screen.getByTestId("preview-add-row-submit")); // 3: insert

    fireEvent.click(screen.getByTestId("preview-queue-save"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "POST", CORRECTIONS_PATH).length).toBe(3),
    );
    const posts = callsOf(backend.calls, "POST", CORRECTIONS_PATH);
    // 按入队顺序逐条提交（载荷序 = 入队序）
    expect(posts.map((call) => call.body)).toEqual([
      { op: "update", row_id: "hash:seed-1", column: "amount", value: "99" },
      { op: "delete", row_id: "hash:seed-2" },
      { op: "insert", values: { amount: "5" } },
    ]);

    // 逐条结果：成功已生效；失败带行上下文与服务端原因（不自动重试——恰 3 次 POST）
    const results = await screen.findAllByTestId("preview-save-result");
    expect(results.length).toBe(3);
    expect(results.map((el) => el.getAttribute("data-ok"))).toEqual([
      "true",
      "false",
      "true",
    ]);
    const failed = screen
      .getAllByTestId("preview-save-result")
      .find((el) => el.getAttribute("data-ok") === "false")!;
    expect(failed.getAttribute("data-op")).toBe("delete");
    expect(failed.textContent).toContain("hash:seed-2");
    expect(failed.textContent).toContain("未知列 nosuch");

    // 保存后队列清空并刷新：update/insert 生效、delete 未生效（行仍在），
    // 修正历史刷新为 2 条（成功条目）。
    await waitFor(() =>
      expect(screen.queryByTestId("preview-dirty-badge")).toBeNull(),
    );
    await waitFor(() =>
      expect(findCell("amount", "hash:seed-1")!.textContent).toBe("99"),
    );
    await waitFor(() =>
      expect(screen.getAllByTestId("preview-row").length).toBe(3),
    );
    await waitFor(() =>
      expect(screen.getAllByTestId("correction-row").length).toBe(2),
    );
    // 预览与修正历史都重新拉取（两个 GET 各自再次调用）
    const rowsGets = callsOf(backend.calls, "GET", ROWS_PATH);
    const correctionsGets = callsOf(backend.calls, "GET", CORRECTIONS_PATH);
    expect(rowsGets.length).toBeGreaterThanOrEqual(2);
    expect(correctionsGets.length).toBeGreaterThanOrEqual(2);
  });

  it("保存成功后刷新行预览与修正历史（mock 两个 fetch），脏队列清空", async () => {
    const backend = install({});
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await enterEditMode();

    const rowsGetsBefore = callsOf(backend.calls, "GET", ROWS_PATH).length;
    const correctionsGetsBefore = callsOf(
      backend.calls,
      "GET",
      CORRECTIONS_PATH,
    ).length;

    editCell("hash:seed-1", "amount", "42");
    fireEvent.click(screen.getByTestId("preview-queue-save"));
    await waitFor(() =>
      expect(
        callsOf(backend.calls, "POST", CORRECTIONS_PATH).length,
      ).toBe(1),
    );
    // 预览刷新：GET rows 再次调用并呈现新值
    await waitFor(() =>
      expect(
        callsOf(backend.calls, "GET", ROWS_PATH).length,
      ).toBeGreaterThan(rowsGetsBefore),
    );
    await waitFor(() =>
      expect(findCell("amount", "hash:seed-1")!.textContent).toBe("42"),
    );
    // 修正历史刷新：GET corrections 再次调用并出现 1 条
    await waitFor(() =>
      expect(
        callsOf(backend.calls, "GET", CORRECTIONS_PATH).length,
      ).toBeGreaterThan(correctionsGetsBefore),
    );
    await waitFor(() =>
      expect(screen.getAllByTestId("correction-row").length).toBe(1),
    );
    // 脏队列清空
    expect(screen.queryByTestId("preview-dirty-badge")).toBeNull();
    expect(
      screen.getByTestId("preview-save-result").getAttribute("data-ok"),
    ).toBe("true");
  });
});

describe("DatasetDetail 行内编辑器：未保存提示与放弃（任务 2.1/2.2）", () => {
  it("队列非空离开编辑模式先确认：取消留在编辑（队列保留）；确认丢弃退出", async () => {
    install({});
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await enterEditMode();

    editCell("hash:seed-1", "amount", "99");
    fireEvent.click(screen.getByTestId("preview-edit-toggle"));
    // 确认弹窗出现；取消 → 仍在编辑态、队列保留
    const dialog = screen.getByTestId("preview-leave-dialog");
    expect(dialog.textContent).toContain("1");
    fireEvent.click(screen.getByTestId("preview-leave-cancel"));
    expect(screen.queryByTestId("preview-leave-dialog")).toBeNull();
    expect(screen.getAllByTestId("preview-delete-row").length).toBe(2);
    expect(screen.getByTestId("preview-dirty-badge").textContent).toContain("1");

    // 再次离开 → 确认 → 丢弃并退出编辑（队列清、编辑控件消失）
    fireEvent.click(screen.getByTestId("preview-edit-toggle"));
    fireEvent.click(screen.getByTestId("preview-leave-confirm"));
    expect(screen.getByTestId("preview-edit-toggle").getAttribute("data-mode")).toBe(
      "read",
    );
    expect(screen.queryByTestId("preview-dirty-badge")).toBeNull();
    expect(screen.queryByTestId("preview-delete-row")).toBeNull();
    expect(screen.queryByTestId("preview-leave-dialog")).toBeNull();
  });

  it("「放弃」直接清队（无弹窗），队列空时离开不再确认", async () => {
    install({});
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await enterEditMode();

    editCell("hash:seed-1", "amount", "99");
    fireEvent.click(screen.getAllByTestId("preview-delete-row")[0]!);
    expect(screen.getByTestId("preview-dirty-badge").textContent).toContain("2");
    fireEvent.click(screen.getByTestId("preview-queue-discard"));
    expect(screen.queryByTestId("preview-dirty-badge")).toBeNull();
    expect(screen.queryByTestId("preview-save-result")).toBeNull();
    // 叠加标注随队列清空复原
    expect(findCell("amount", "hash:seed-1")!.getAttribute("data-pending-edit")).toBe(
      "false",
    );

    // 队列空 → 直接退出，不弹确认
    fireEvent.click(screen.getByTestId("preview-edit-toggle"));
    expect(screen.queryByTestId("preview-leave-dialog")).toBeNull();
    expect(screen.getByTestId("preview-edit-toggle").getAttribute("data-mode")).toBe(
      "read",
    );
  });
});

describe("DatasetDetail 行内编辑器：cell/api 两档同 UI（任务 2.2）", () => {
  it("响应带 storage_tier=api 与 cell 档（不带标记）走同一编辑流与同契约载荷", async () => {
    for (const tier of ["api", "cell"]) {
      cleanup();
      vi.unstubAllGlobals();
      const backend = install({
        dataset: makeDataset({
          ...(tier === "api" ? { storage_tier: "api" } : {}),
        } as Partial<Dataset>),
      });
      render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
      await enterEditMode();

      // 两档同 UI：编辑控件与只读默认完全一致
      expect(screen.getByTestId("preview-edit-toggle").getAttribute("data-mode")).toBe(
        "edit",
      );
      expect(screen.getAllByTestId("preview-delete-row").length).toBe(2);
      editCell("hash:seed-1", "amount", "7");
      fireEvent.click(screen.getByTestId("preview-queue-save"));
      await waitFor(() =>
        expect(callsOf(backend.calls, "POST", CORRECTIONS_PATH).length).toBe(1),
      );
      const posts = callsOf(backend.calls, "POST", CORRECTIONS_PATH);
      // 同一端点契约（不因档位分叉）
      expect(posts[0]!.url.pathname).toBe(CORRECTIONS_PATH);
      expect(posts[0]!.body).toEqual({
        op: "update",
        row_id: "hash:seed-1",
        column: "amount",
        value: "7",
      });
    }
  });
});

describe("DatasetDetail 导出 CSV（wire-dataset-export 任务 2.2/2.3）", () => {
  /** 桩化 blob 下载（createObjectURL/revokeObjectURL）并捕获下载 a 标签。 */
  function stubDownload() {
    const createObjectURL = vi.fn((_blob: Blob) => "blob:csv");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal(
      "URL",
      Object.assign(URL, { createObjectURL, revokeObjectURL }),
    );
    const createElement = vi.spyOn(document, "createElement");
    return {
      createObjectURL,
      revokeObjectURL,
      anchor: () =>
        createElement.mock.results
          .map((result) => result.value as HTMLElement)
          .find((el) => el.tagName === "A") as HTMLAnchorElement | undefined,
    };
  }

  it("点击「导出 CSV」：带 Bearer 请求 export 端点，blob 经 createObjectURL 触发下载并回收；文件名取 Content-Disposition", async () => {
    const download = stubDownload();
    const backend = install({});
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    fireEvent.click(await screen.findByTestId("detail-export-csv"));
    await waitFor(() =>
      expect(download.createObjectURL).toHaveBeenCalledTimes(1),
    );
    const call = callsOf(backend.calls, "GET", EXPORT_PATH)[0]!;
    expect(call.url.pathname).toBe(EXPORT_PATH);
    expect(call.headers["authorization"]).toMatch(/^Bearer /);
    expect(call.headers["accept"]).toBe("text/csv");
    // blob（BOM CSV 全文）交给浏览器下载，object URL 即时回收
    expect(download.createObjectURL.mock.calls[0]![0]).toBeInstanceOf(Blob);
    const anchor = download.anchor();
    expect(anchor?.getAttribute("href")).toBe("blob:csv");
    expect(anchor?.download).toBe(`${KEY}.csv`);
    expect(download.revokeObjectURL).toHaveBeenCalledWith("blob:csv");
    // 成功且无截断头：零截断提示、零错误
    expect(screen.queryByTestId("export-csv-truncated")).toBeNull();
    expect(screen.queryByTestId("export-csv-error")).toBeNull();
  });

  it("X-Export-Truncated: true → 呈现「已截断至前 10,000 行」提示", async () => {
    const download = stubDownload();
    install({ exportTruncated: true });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    fireEvent.click(await screen.findByTestId("detail-export-csv"));
    await waitFor(() =>
      expect(download.createObjectURL).toHaveBeenCalledTimes(1),
    );
    expect(screen.getByTestId("export-csv-truncated").textContent).toContain(
      "已截断至前 10,000 行",
    );
    expect(screen.queryByTestId("export-csv-error")).toBeNull();
  });

  it("失败如实呈现：500 的后端 detail 原样展示，按钮复位可重试", async () => {
    install({ failExportStatus: 500, failExportDetail: "导出暂时不可用" });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    const button = await screen.findByTestId("detail-export-csv");
    fireEvent.click(button);
    const error = await screen.findByTestId("export-csv-error");
    expect(error.textContent).toContain("导出暂时不可用");
    expect(screen.queryByTestId("export-csv-truncated")).toBeNull();
    await waitFor(() => expect(button.hasAttribute("disabled")).toBe(false));
  });

  it("下载中禁用防重复：悬挂期间二次点击不再发请求，完成后恰 1 次", async () => {
    const download = stubDownload();
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const backend = install({ holdExport: held });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    const button = await screen.findByTestId("detail-export-csv");
    fireEvent.click(button);
    await waitFor(() =>
      expect(callsOf(backend.calls, "GET", EXPORT_PATH).length).toBe(1),
    );
    // 悬挂期间按钮禁用（文案切换「导出中…」）；再点不发第二次请求
    await waitFor(() => expect(button.hasAttribute("disabled")).toBe(true));
    expect(button.textContent).toContain("导出中");
    fireEvent.click(button);
    expect(callsOf(backend.calls, "GET", EXPORT_PATH).length).toBe(1);

    release();
    await waitFor(() =>
      expect(download.createObjectURL).toHaveBeenCalledTimes(1),
    );
    expect(callsOf(backend.calls, "GET", EXPORT_PATH).length).toBe(1);
    await waitFor(() => expect(button.hasAttribute("disabled")).toBe(false));
  });
});

describe("DatasetDetail 重派生（wire-pipeline-actions 任务 1.2/1.3）", () => {
  it("点击重派生：POST /refresh 带 Bearer 与 cascade=true（默认勾选）；取消勾选后 cascade=false 且无级联列表", async () => {
    const backend = install({
      dataset: makeDataset({ source_kind: "derived" }),
      cascade: [],
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("rederive-section");

    // 级联默认勾选
    expect(
      (screen.getByTestId("rederive-cascade") as HTMLInputElement).checked,
    ).toBe(true);
    fireEvent.click(screen.getByTestId("rederive-submit"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "POST", REFRESH_PATH).length).toBe(1),
    );
    let call = callsOf(backend.calls, "POST", REFRESH_PATH)[0]!;
    expect(call.headers["authorization"]).toMatch(/^Bearer /);
    expect(call.url.searchParams.get("cascade")).toBe("true");

    // 取消勾选 → cascade=false；关闭级联：空级联注记（无级联列表）
    fireEvent.click(screen.getByTestId("rederive-cascade"));
    fireEvent.click(screen.getByTestId("rederive-submit"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "POST", REFRESH_PATH).length).toBe(2),
    );
    call = callsOf(backend.calls, "POST", REFRESH_PATH)[1]!;
    expect(call.url.searchParams.get("cascade")).toBe("false");
    expect(
      (await screen.findByTestId("rederive-cascade-empty")).textContent,
    ).toContain("无级联结果");
    expect(screen.queryByTestId("rederive-cascade-row")).toBeNull();
  });

  it("级联结果如实呈现（ok/failed 混合 + 失败原因与深度），完成后详情与预览刷新、按钮复位", async () => {
    const backend = install({
      dataset: makeDataset({ source_kind: "derived" }),
      cascade: [
        { dataset_id: "ds-2", name: "华东月度汇总", depth: 1, status: "ok", error: null },
        {
          dataset_id: "ds-3",
          name: "毛利明细",
          depth: 2,
          status: "failed",
          error: "上游列缺失：price",
        },
      ],
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("rederive-section");
    const detailGetsBefore = callsOf(backend.calls, "GET", "/api/v1/datasets/ds-1").length;
    const rowsGetsBefore = callsOf(backend.calls, "GET", ROWS_PATH).length;

    fireEvent.click(screen.getByTestId("rederive-submit"));
    const entries = await screen.findAllByTestId("rederive-cascade-row");
    expect(entries.map((el) => el.getAttribute("data-status"))).toEqual([
      "ok",
      "failed",
    ]);
    expect(entries[0]!.textContent).toContain("华东月度汇总");
    expect(entries[0]!.textContent).toContain("第 1 级");
    expect(entries[1]!.textContent).toContain("毛利明细");
    expect(entries[1]!.getAttribute("data-depth")).toBe("2");
    expect(
      screen.getByTestId("rederive-cascade-error").textContent,
    ).toContain("上游列缺失：price");

    // 完成后刷新：详情字段（行数/更新时间）与预览行重拉
    await waitFor(() =>
      expect(
        callsOf(backend.calls, "GET", "/api/v1/datasets/ds-1").length,
      ).toBeGreaterThan(detailGetsBefore),
    );
    await waitFor(() =>
      expect(callsOf(backend.calls, "GET", ROWS_PATH).length).toBeGreaterThan(
        rowsGetsBefore,
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("rederive-submit").hasAttribute("disabled")).toBe(
        false,
      ),
    );
  });

  it("重派生中禁用防重复：悬挂期间二次点击不再发请求；失败如实呈现 detail 且按钮复位", async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const backend = install({
      dataset: makeDataset({ source_kind: "derived" }),
      holdRefresh: held,
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    const submit = await screen.findByTestId("rederive-submit");
    fireEvent.click(submit);
    await waitFor(() =>
      expect(callsOf(backend.calls, "POST", REFRESH_PATH).length).toBe(1),
    );
    // 悬挂期间按钮禁用（文案切换「重派生中…」）；再点不发第二次请求
    await waitFor(() => expect(submit.hasAttribute("disabled")).toBe(true));
    expect(submit.textContent).toContain("重派生中");
    fireEvent.click(submit);
    expect(callsOf(backend.calls, "POST", REFRESH_PATH).length).toBe(1);
    release();
    await waitFor(() =>
      expect(submit.hasAttribute("disabled")).toBe(false),
    );
  });

  it("重派生失败如实：500 的后端 detail 原样展示，无级联结果", async () => {
    install({
      dataset: makeDataset({ source_kind: "derived" }),
      failRefreshStatus: 500,
      failRefreshDetail: "引擎暂不可用",
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    fireEvent.click(await screen.findByTestId("rederive-submit"));
    expect(
      (await screen.findByTestId("rederive-error")).textContent,
    ).toContain("引擎暂不可用");
    expect(screen.queryByTestId("rederive-cascade-row")).toBeNull();
    await waitFor(() =>
      expect(screen.getByTestId("rederive-submit").hasAttribute("disabled")).toBe(
        false,
      ),
    );
  });
});

describe("DatasetDetail 定时重派生（wire-pipeline-actions 任务 1.2/1.3）", () => {
  it("cron 登记 → 登记态呈现 → 更新 cron → 删除登记回未登记（PUT/DELETE 带 Bearer）", async () => {
    const backend = install({
      dataset: makeDataset({ source_kind: "derived" }),
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    // 未登记（GET cron=null）：登记面，保存按钮文案=登记
    const save = await screen.findByTestId("rederive-schedule-save");
    expect(save.textContent).toContain("登记定时重派生");
    expect(screen.queryByTestId("rederive-schedule-row")).toBeNull();

    fireEvent.change(screen.getByTestId("rederive-cron-input"), {
      target: { value: "*/5 * * * *" },
    });
    fireEvent.click(save);
    await waitFor(() =>
      expect(callsOf(backend.calls, "PUT", SCHEDULE_PATH).length).toBe(1),
    );
    const put = callsOf(backend.calls, "PUT", SCHEDULE_PATH)[0]!;
    expect(put.headers["authorization"]).toMatch(/^Bearer /);
    expect(put.body).toEqual({ cron: "*/5 * * * *" });

    // 登记态（PUT 响应内存落库）：cron + 已登记徽标 + 尚未运行
    await screen.findByTestId("rederive-schedule-row");
    expect(screen.getByTestId("rederive-schedule-cron").textContent).toBe(
      "*/5 * * * *",
    );
    expect(
      screen.getByTestId("rederive-schedule-enabled").getAttribute("data-enabled"),
    ).toBe("true");
    expect(screen.getByTestId("rederive-schedule-last-run").textContent).toContain(
      "尚未运行",
    );

    // 更新：改 cron 再保存（按钮文案切换「更新 cron」）
    expect(screen.getByTestId("rederive-schedule-save").textContent).toContain(
      "更新 cron",
    );
    fireEvent.change(screen.getByTestId("rederive-cron-input"), {
      target: { value: "0 * * * *" },
    });
    fireEvent.click(screen.getByTestId("rederive-schedule-save"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "PUT", SCHEDULE_PATH).length).toBe(2),
    );
    expect(callsOf(backend.calls, "PUT", SCHEDULE_PATH)[1]!.body).toEqual({
      cron: "0 * * * *",
    });
    expect(
      (await screen.findByTestId("rederive-schedule-cron")).textContent,
    ).toBe("0 * * * *");

    // 删除：DELETE → 回未登记态（登记面复现、输入清空）
    fireEvent.click(screen.getByTestId("rederive-schedule-delete"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "DELETE", SCHEDULE_PATH).length).toBe(1),
    );
    const del = callsOf(backend.calls, "DELETE", SCHEDULE_PATH)[0]!;
    expect(del.headers["authorization"]).toMatch(/^Bearer /);
    await waitFor(() =>
      expect(screen.queryByTestId("rederive-schedule-row")).toBeNull(),
    );
    expect(screen.getByTestId("rederive-schedule-save").textContent).toContain(
      "登记定时重派生",
    );
    expect(
      (screen.getByTestId("rederive-cron-input") as HTMLInputElement).value,
    ).toBe("");
  });

  it("已登记态如实呈现：cron/enabled/最近运行（本地化）/上次结果/错误/行数 + UTC 诚实注记", async () => {
    install({
      dataset: makeDataset({ source_kind: "derived" }),
      scheduleSeed: {
        cron: "*/15 * * * *",
        enabled: true,
        last_run_at: "2026-10-11T08:30:00",
        last_status: "partial_failure",
        last_error: "子链毛利明细失败：上游列缺失",
        last_rows: 4200,
      },
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    await screen.findByTestId("rederive-schedule-row");
    expect(screen.getByTestId("rederive-schedule-cron").textContent).toBe(
      "*/15 * * * *",
    );
    expect(
      screen.getByTestId("rederive-schedule-enabled").textContent,
    ).toContain("已登记");
    expect(
      screen.getByTestId("rederive-schedule-last-run").textContent,
    ).toContain(formatServerDate("2026-10-11T08:30:00"));
    expect(
      screen.getByTestId("rederive-schedule-last-status").textContent,
    ).toContain("partial_failure");
    expect(
      screen.getByTestId("rederive-schedule-last-error").textContent,
    ).toContain("上游列缺失");
    expect(
      screen.getByTestId("rederive-schedule-last-rows").textContent,
    ).toContain("4200");
    // naive UTC 诚实注记（v1 口径：客户时区换算属登记侧）
    expect(
      screen.getByTestId("rederive-schedule-utc-note").textContent,
    ).toContain("协调世界时");
  });

  it("登记失败如实：非法 cron 的 400 detail 原样展示，不进入登记态，按钮复位", async () => {
    install({ dataset: makeDataset({ source_kind: "derived" }) });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("rederive-schedule-save");

    fireEvent.change(screen.getByTestId("rederive-cron-input"), {
      target: { value: "bad" },
    });
    fireEvent.click(screen.getByTestId("rederive-schedule-save"));
    expect(
      (await screen.findByTestId("rederive-schedule-error")).textContent,
    ).toContain("五字段");
    expect(screen.queryByTestId("rederive-schedule-row")).toBeNull();
    await waitFor(() =>
      expect(
        screen.getByTestId("rederive-schedule-save").hasAttribute("disabled"),
      ).toBe(false),
    );
  });
});

describe("DatasetDetail 重派生入口门禁（wire-pipeline-actions 任务 1.3）", () => {
  it("api 档（免费档）与上传来源数据集：两节均不渲染、零 refresh/schedule 请求", async () => {
    for (const dataset of [
      makeDataset({
        source_kind: "derived",
        storage_tier: "api",
      } as Partial<Dataset>),
      makeDataset({ source_kind: "upload" }),
    ]) {
      cleanup();
      vi.unstubAllGlobals();
      const backend = install({ dataset });
      render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
      await screen.findByTestId("preview-grid");

      expect(screen.queryByTestId("rederive-section")).toBeNull();
      expect(screen.queryByTestId("rederive-schedule-section")).toBeNull();
      expect(
        backend.calls.filter(
          (call) =>
            call.url.pathname === REFRESH_PATH ||
            call.url.pathname === SCHEDULE_PATH,
        ),
      ).toEqual([]);
    }
  });
});

describe("DatasetDetail 增量同步设置（wire-incremental-sync）", () => {
  it("连接器数据集渲染卡片（默认全量快照+诚实注记）；上传/声明/派生来源不渲染且零 PATCH", async () => {
    for (const dataset of [
      makeDataset({ source_kind: "upload" }),
      makeDataset({ source_kind: "declared" }),
      makeDataset({ source_kind: "derived" }),
    ]) {
      cleanup();
      vi.unstubAllGlobals();
      const backend = install({ dataset });
      render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
      await screen.findByTestId("preview-grid");
      expect(screen.queryByTestId("sync-settings-section")).toBeNull();
      expect(
        backend.calls.filter((call) => call.url.pathname === SYNC_SETTINGS_PATH),
      ).toEqual([]);
    }

    // 连接器来源：卡片渲染，默认 snapshot（缺省字段容错）+ 无水位；
    // snapshot 态无重置按钮、无「尚未同步」注记（水位概念仅增量态）。
    cleanup();
    vi.unstubAllGlobals();
    install({ dataset: makeConnectorDataset() });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("sync-settings-section");
    expect(screen.getByTestId("sync-status-mode").getAttribute("data-mode")).toBe(
      "snapshot",
    );
    expect(screen.getByTestId("sync-status-mode").textContent).toContain("全量快照");
    expect(screen.getByTestId("sync-status-column").textContent).toContain("—");
    expect(screen.queryByTestId("sync-reset-button")).toBeNull();
    expect(screen.queryByTestId("sync-not-synced")).toBeNull();
    // 诚实口径小字（合并语义 + 重置回全量）
    expect(screen.getByTestId("sync-honesty-note").textContent).toContain(
      "不会同步源端删除的行",
    );
  });

  it("切增量+选水位列保存：PATCH {sync_mode, watermark_column} 带 Bearer，成功后详情刷新、状态转增量；切回 snapshot 只发 sync_mode", async () => {
    const backend = install({ dataset: makeConnectorDataset() });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("sync-settings-section");
    const detailGetsBefore = callsOf(backend.calls, "GET", "/api/v1/datasets/ds-1")
      .length;

    // 切增量：水位列下拉出现（选项 = 数据集列清单），未选时保存禁用
    fireEvent.change(screen.getByTestId("sync-mode-select"), {
      target: { value: "incremental" },
    });
    const columnSelect = screen.getByTestId("watermark-column-select");
    expect(
      Array.from(columnSelect.querySelectorAll("option")).map((o) => o.value),
    ).toEqual(["", "id", "amount", "updated_at"]);
    expect(screen.getByTestId("sync-settings-save").hasAttribute("disabled")).toBe(
      true,
    );

    // 选水位列 → 保存放行 → PATCH 契约载荷（Bearer）
    fireEvent.change(columnSelect, { target: { value: "updated_at" } });
    expect(screen.getByTestId("sync-settings-save").hasAttribute("disabled")).toBe(
      false,
    );
    fireEvent.click(screen.getByTestId("sync-settings-save"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "PATCH", SYNC_SETTINGS_PATH).length).toBe(1),
    );
    const patch = callsOf(backend.calls, "PATCH", SYNC_SETTINGS_PATH)[0]!;
    expect(patch.headers["authorization"]).toMatch(/^Bearer /);
    expect(patch.body).toEqual({
      sync_mode: "incremental",
      watermark_column: "updated_at",
    });

    // 成功后详情重拉（手动 fetch 模式）：状态行转增量 + 水位列 + 未同步注记
    await waitFor(() =>
      expect(
        callsOf(backend.calls, "GET", "/api/v1/datasets/ds-1").length,
      ).toBeGreaterThan(detailGetsBefore),
    );
    await waitFor(() =>
      expect(
        screen.getByTestId("sync-status-mode").getAttribute("data-mode"),
      ).toBe("incremental"),
    );
    expect(screen.getByTestId("sync-status-column").textContent).toContain(
      "updated_at",
    );
    expect(screen.getByTestId("sync-not-synced").textContent).toBe(
      "尚未同步（首次将建立基线）",
    );

    // 切回 snapshot：只发模式（水位字段由服务端清空）→ 状态回全量、无水位
    await waitFor(() =>
      expect(
        screen.getByTestId("sync-mode-select").hasAttribute("disabled"),
      ).toBe(false),
    );
    fireEvent.change(screen.getByTestId("sync-mode-select"), {
      target: { value: "snapshot" },
    });
    fireEvent.click(screen.getByTestId("sync-settings-save"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "PATCH", SYNC_SETTINGS_PATH).length).toBe(2),
    );
    expect(callsOf(backend.calls, "PATCH", SYNC_SETTINGS_PATH)[1]!.body).toEqual({
      sync_mode: "snapshot",
    });
    await waitFor(() =>
      expect(
        screen.getByTestId("sync-status-mode").getAttribute("data-mode"),
      ).toBe("snapshot"),
    );
    expect(screen.getByTestId("sync-status-column").textContent).toContain("—");
  });

  it("增量未选水位列 → 保存禁用零 PATCH；服务端 422 → 后端 detail 原样展示、卡片不崩、按钮复位", async () => {
    const backend = install({
      dataset: makeConnectorDataset(),
      failSyncSettingsStatus: 422,
      failSyncSettingsDetail: "水位列 nosuch 不在数据集列清单中",
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("sync-settings-section");

    // 客户端闸：增量未选水位列 → 保存禁用，点击不发请求
    fireEvent.change(screen.getByTestId("sync-mode-select"), {
      target: { value: "incremental" },
    });
    const save = screen.getByTestId("sync-settings-save");
    expect(save.hasAttribute("disabled")).toBe(true);
    fireEvent.click(save);
    expect(callsOf(backend.calls, "PATCH", SYNC_SETTINGS_PATH).length).toBe(0);

    // 服务端 422（注入失败态）：detail 原样展示，卡片仍完整渲染
    fireEvent.change(screen.getByTestId("watermark-column-select"), {
      target: { value: "id" },
    });
    fireEvent.click(save);
    const error = await screen.findByTestId("sync-settings-error");
    expect(error.textContent).toContain("水位列 nosuch 不在数据集列清单中");
    expect(screen.getByTestId("sync-settings-section")).toBeTruthy();
    expect(screen.getByTestId("sync-mode-select")).toBeTruthy();
    await waitFor(() =>
      expect(save.hasAttribute("disabled")).toBe(false),
    );
  });

  it("重置水位二次确认：警告「下次同步将全量重建」；取消不发，确认 PATCH {reset_watermark: true} 只清水位值", async () => {
    const backend = install({
      dataset: makeConnectorDataset({
        sync_mode: "incremental",
        watermark_column: "updated_at",
        watermark_value: "2026-10-11T08:30:00",
      }),
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("sync-settings-section");
    // 当前水位值字符串原样展示
    expect(screen.getByTestId("sync-status-value-text").textContent).toBe(
      "2026-10-11T08:30:00",
    );

    // 重置 → 二次确认（全量重建警告）；取消不发请求
    fireEvent.click(screen.getByTestId("sync-reset-button"));
    expect(screen.getByTestId("sync-reset-warning").textContent).toContain(
      "下次同步将全量重建",
    );
    fireEvent.click(screen.getByTestId("sync-reset-cancel"));
    expect(screen.queryByTestId("sync-reset-dialog")).toBeNull();
    expect(callsOf(backend.calls, "PATCH", SYNC_SETTINGS_PATH).length).toBe(0);

    // 再开确认 → 确认 → PATCH 恰为 {reset_watermark: true}
    fireEvent.click(screen.getByTestId("sync-reset-button"));
    fireEvent.click(screen.getByTestId("sync-reset-confirm"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "PATCH", SYNC_SETTINGS_PATH).length).toBe(1),
    );
    const patch = callsOf(backend.calls, "PATCH", SYNC_SETTINGS_PATH)[0]!;
    expect(patch.body).toEqual({ reset_watermark: true });

    // 重置只清水位值：模式/水位列保持，状态回「尚未同步（首次将建立基线）」
    await waitFor(() =>
      expect(screen.getByTestId("sync-not-synced").textContent).toBe(
        "尚未同步（首次将建立基线）",
      ),
    );
    expect(screen.getByTestId("sync-status-mode").getAttribute("data-mode")).toBe(
      "incremental",
    );
    expect(screen.getByTestId("sync-status-column").textContent).toContain(
      "updated_at",
    );
    // 水位值清空后重置按钮不再渲染
    await waitFor(() =>
      expect(screen.queryByTestId("sync-reset-button")).toBeNull(),
    );
  });

  it("长水位值截断展示 + title 全文（字符串原样口径不丢信息）", async () => {
    const long = "2026-10-11T08:30:00.123456789+00:00-extremely-long-watermark";
    install({
      dataset: makeConnectorDataset({
        sync_mode: "incremental",
        watermark_column: "id",
        watermark_value: long,
      }),
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("sync-settings-section");

    const text = screen.getByTestId("sync-status-value-text");
    expect(text.textContent).not.toBe(long);
    expect(text.textContent).toContain("…");
    expect(text.textContent!.startsWith(long.slice(0, 32))).toBe(true);
    expect(text.getAttribute("title")).toBe(long);
  });

  it("i18n 双语对照：zh/en 的诚实口径注记与未同步注记（卡片标题两语言）", async () => {
    const incrementalNotSynced = () =>
      makeConnectorDataset({
        sync_mode: "incremental",
        watermark_column: "updated_at",
        watermark_value: null,
      });

    // zh（beforeEach 默认语言）
    install({ dataset: incrementalNotSynced() });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("sync-settings-section");
    expect(
      screen.getByTestId("sync-settings-section").querySelector("h2")!
        .textContent,
    ).toBe("增量同步");
    expect(screen.getByTestId("sync-honesty-note").textContent).toBe(
      "增量同步按主键合并，不会同步源端删除的行；需要重建时重置水位，下次同步回到全量快照。",
    );
    expect(screen.getByTestId("sync-not-synced").textContent).toBe(
      "尚未同步（首次将建立基线）",
    );

    // en 对照（重渲染）
    cleanup();
    vi.unstubAllGlobals();
    i18n.changeLanguage("en");
    install({ dataset: incrementalNotSynced() });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("sync-settings-section");
    expect(
      screen.getByTestId("sync-settings-section").querySelector("h2")!
        .textContent,
    ).toBe("Incremental sync");
    expect(screen.getByTestId("sync-honesty-note").textContent).toBe(
      "Incremental sync merges by primary key; rows deleted at the source are not removed. Reset the watermark to rebuild with a full snapshot.",
    );
    expect(screen.getByTestId("sync-not-synced").textContent).toBe(
      "Not synced yet (first run takes a full baseline)",
    );
  });
});

describe("编辑器纯逻辑 editQueue（任务 2.1：队列归约/顺序保存）", () => {
  it("enqueueEdit：按入队顺序递增 seq、不改原数组", () => {
    const first = enqueueEdit([], { op: "delete", row_id: "hash:seed-2" });
    const second = enqueueEdit(first, {
      op: "update",
      row_id: "hash:seed-1",
      column: "amount",
      value: "99",
    });
    const third = enqueueEdit(second, { op: "insert", values: { amount: "5" } });
    expect(first.map((item) => item.seq)).toEqual([1]);
    expect(third.map((item) => [item.seq, item.payload.op])).toEqual([
      [1, "delete"],
      [2, "update"],
      [3, "insert"],
    ]);
    expect(second.length).toBe(2); // 原数组不动（追加式）
  });

  it("describeEditItem：三种操作各带行上下文（行锚/列=值/values）", () => {
    expect(
      describeEditItem({
        op: "update",
        row_id: "hash:seed-1",
        column: "amount",
        value: "99",
      }),
    ).toBe("hash:seed-1 · amount = 99");
    expect(describeEditItem({ op: "delete", row_id: "hash:seed-2" })).toBe(
      "hash:seed-2",
    );
    expect(describeEditItem({ op: "insert", values: { amount: "5" } })).toBe(
      '{"amount":"5"}',
    );
  });

  it("saveEditQueue：按序逐条提交；单条失败隔离（后续继续、不自动重试）", async () => {
    const items: EditQueueItem[] = [
      { seq: 1, payload: { op: "update", row_id: "r1", column: "c", value: "1" } },
      { seq: 2, payload: { op: "delete", row_id: "r2" } },
      { seq: 3, payload: { op: "insert", values: {} } },
    ];
    const submitted: CorrectionInput[] = [];
    const boom = new Error("server said no");
    const outcomes = await saveEditQueue(items, async (payload) => {
      submitted.push(payload);
      if (payload.op === "delete") throw boom;
    });
    // 顺序提交 + 每条恰一次（失败不重试、不断链）
    expect(submitted).toEqual(items.map((item) => item.payload));
    expect(outcomes.map((outcome) => outcome.ok)).toEqual([true, false, true]);
    expect(outcomes[1]!.error).toBe(boom);
    expect(outcomes[0]!.error).toBeUndefined();
  });
});

// ── 资产信息编辑区（wire-asset-catalog 任务 5.2）──────────────────────────
// 六字段呈现与保存 PATCH 载荷、生效节奏提示（来自指标目录）、422 字段级回显、
// 403（viewer 只读）切只读态、最近修改留痕（可折叠、续记最前）。
describe("DatasetDetail 资产信息（wire-asset-catalog 5.2）", () => {
  const ASSET_META_PATH = "/api/v1/datasets/ds-1/asset-meta";
  const ASSET_EVENTS_PATH = `${ASSET_META_PATH}/events`;

  it("六字段呈现种子值；改标签与敏感度保存 → PATCH 全量六字段载荷 + 已保存提示", async () => {
    const backend = install({
      assetMeta: {
        domain: "销售",
        tags: ["月度"],
        description: "门店月度销量",
        cadence_hint: "monthly",
        owner_note: "张三",
        sensitivity: "常规",
      },
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    const form = await screen.findByTestId("asset-meta-form");
    expect(form.getAttribute("data-readonly")).toBe("false");
    expect(
      (within(form).getByTestId("asset-meta-domain") as HTMLInputElement).value,
    ).toBe("销售");
    expect(
      (within(form).getByTestId("asset-meta-tags") as HTMLInputElement).value,
    ).toBe("月度");
    expect(
      (within(form).getByTestId("asset-meta-description") as HTMLTextAreaElement)
        .value,
    ).toBe("门店月度销量");
    expect(
      (within(form).getByTestId("asset-meta-cadence") as HTMLSelectElement).value,
    ).toBe("monthly");
    expect(
      (within(form).getByTestId("asset-meta-owner") as HTMLInputElement).value,
    ).toBe("张三");
    expect(
      (within(form).getByTestId("asset-meta-sensitivity") as HTMLSelectElement)
        .value,
    ).toBe("常规");

    // 改标签（逗号拆数组）与敏感度（三选）→ 保存
    fireEvent.change(screen.getByTestId("asset-meta-tags"), {
      target: { value: "月度, 门店" },
    });
    fireEvent.change(screen.getByTestId("asset-meta-sensitivity"), {
      target: { value: "敏感" },
    });
    fireEvent.click(screen.getByTestId("asset-meta-save"));

    await waitFor(() =>
      expect(callsOf(backend.calls, "PATCH", ASSET_META_PATH)).toHaveLength(1),
    );
    const patch = callsOf(backend.calls, "PATCH", ASSET_META_PATH)[0]!;
    expect(patch.body).toEqual({
      domain: "销售",
      tags: ["月度", "门店"],
      description: "门店月度销量",
      cadence_hint: "monthly",
      owner_note: "张三",
      sensitivity: "敏感",
    });
    expect(await screen.findByTestId("asset-meta-saved")).toBeTruthy();
  });

  it("生效节奏提示：cadence_hint 未声明但指标目录带节奏时展示（声明后隐藏）", async () => {
    install({
      assetMeta: { cadence_hint: null, cadence_effective: "weekly" },
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    const hint = await screen.findByTestId("asset-meta-effective-hint");
    expect(hint.textContent).toContain("来自指标目录");
    expect(hint.textContent).toContain("周更");

    // 声明期望节奏后提示退位（客户口径优先于目录口径）。
    fireEvent.change(screen.getByTestId("asset-meta-cadence"), {
      target: { value: "daily" },
    });
    await waitFor(() =>
      expect(screen.queryByTestId("asset-meta-effective-hint")).toBeNull(),
    );
  });

  it("422 非法敏感度：后端合法清单 detail 如实回显，仍可编辑重试", async () => {
    install({
      assetMeta: { sensitivity: "常规" },
      failAssetMetaPatchStatus: 422,
      failAssetMetaPatchDetail: "敏感度只能是：常规 / 内部 / 敏感",
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("asset-meta-form");

    fireEvent.click(screen.getByTestId("asset-meta-save"));
    const error = await screen.findByTestId("asset-meta-error");
    expect(error.textContent).toContain("常规 / 内部 / 敏感");
    // 未切只读：仍可改值重试
    expect(screen.getByTestId("asset-meta-form").getAttribute("data-readonly")).toBe(
      "false",
    );
    expect(screen.queryByTestId("asset-meta-readonly")).toBeNull();
  });

  it("422 FastAPI 数组形状：按 loc 定位到敏感度字段旁回显", async () => {
    install({
      assetMeta: { sensitivity: "常规" },
      failAssetMetaPatchStatus: 422,
      failAssetMetaPatchDetail: [
        {
          type: "enum",
          loc: ["body", "sensitivity"],
          msg: "敏感度只能是：常规 / 内部 / 敏感",
        },
      ] as ApiDetailItem[],
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("asset-meta-form");

    fireEvent.click(screen.getByTestId("asset-meta-save"));
    const fieldError = await screen.findByTestId("asset-meta-field-error");
    expect(fieldError.getAttribute("data-field")).toBe("sensitivity");
    expect(fieldError.textContent).toContain("常规 / 内部 / 敏感");
  });

  it("403（viewer 只读）：保存被拒后切只读态——字段禁用、保存收起、提示如实", async () => {
    const backend = install({
      assetMeta: { domain: "销售" },
      failAssetMetaPatchStatus: 403,
      failAssetMetaPatchDetail: "团队成员权限不足（viewer 只读）",
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("asset-meta-form");

    fireEvent.click(screen.getByTestId("asset-meta-save"));
    await waitFor(() =>
      expect(
        callsOf(backend.calls, "PATCH", ASSET_META_PATH).length,
      ).toBe(1),
    );
    await waitFor(() =>
      expect(screen.getByTestId("asset-meta-readonly")).toBeTruthy(),
    );
    const form = screen.getByTestId("asset-meta-form");
    expect(form.getAttribute("data-readonly")).toBe("true");
    expect(
      (screen.getByTestId("asset-meta-domain") as HTMLInputElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByTestId("asset-meta-sensitivity") as HTMLSelectElement).disabled,
    ).toBe(true);
    expect(screen.queryByTestId("asset-meta-save")).toBeNull();
  });

  it("留痕：种子留痕折叠态默认不展开，展开显最近 3 条；保存后新留痕续记最前", async () => {
    const backend = install({
      assetMeta: { domain: "销售" },
      assetMetaEvents: [
        {
          at: "2026-10-12T08:00:00",
          actor_sub: "editor-sub-1",
          before: { sensitivity: "常规" },
          after: { sensitivity: "内部" },
        },
        {
          at: "2026-10-11T08:00:00",
          actor_sub: "owner-sub-1",
          before: { domain: null },
          after: { domain: "销售" },
        },
      ],
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("asset-meta-form");

    // 折叠默认：留痕区在、条目不展开
    expect(screen.getByTestId("asset-meta-events")).toBeTruthy();
    expect(screen.queryByTestId("asset-meta-event")).toBeNull();

    fireEvent.click(screen.getByTestId("asset-meta-events-toggle"));
    const events = screen.getAllByTestId("asset-meta-event");
    expect(events).toHaveLength(2);
    expect(
      screen.getAllByTestId("asset-meta-event-changes").map((el) => el.textContent),
    ).toEqual(
      expect.arrayContaining([expect.stringContaining("敏感度")]),
    );

    // 保存（改业务域）→ 新留痕续记最前，变更字段含业务域
    fireEvent.change(screen.getByTestId("asset-meta-domain"), {
      target: { value: "供应链" },
    });
    fireEvent.click(screen.getByTestId("asset-meta-save"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "PATCH", ASSET_META_PATH)).toHaveLength(1),
    );
    await waitFor(() =>
      expect(screen.getAllByTestId("asset-meta-event").length).toBe(3),
    );
    expect(
      screen.getAllByTestId("asset-meta-event-changes")[0]!.textContent,
    ).toContain("业务域");
    // 留痕端点被查询（保存后重拉）
    expect(
      callsOf(backend.calls, "GET", ASSET_EVENTS_PATH).length,
    ).toBeGreaterThanOrEqual(2);
  });
});

// ── 数据质量面板（wire-dataset-quality 任务 5.3）──────────────────────────
// 口径标注常驻 + 四种如实态（ok 指标 / failed 只示原因 / unsupported / 暂无
// 快照）+ 手动「立即检查」（POST refresh → 重拉；进行中禁用；失败如实）。
describe("DatasetDetail 数据质量（wire-dataset-quality 5.3）", () => {
  const QUALITY_PATH = "/api/v1/datasets/ds-1/quality";
  const QUALITY_REFRESH_PATH = `${QUALITY_PATH}/refresh`;

  /** ok 态种子：五项指标 + 三点历史（趋势）+ 过期新鲜度。 */
  function okSeed() {
    return {
      latest: {
        computed_at: "2026-10-12T09:00:00",
        triggered_by: "sync",
        status: "ok",
        row_count: 1200,
        pk_declared: true,
        pk_unique_ratio: 0.987,
        null_rates: [
          { column: "id", null_rate: 0, sampled_rows: 10000 },
          { column: "amount", null_rate: 0.0234, sampled_rows: 10000 },
        ],
        sample_limit_hit: true,
        error_text: null,
      },
      history: [
        { computed_at: "2026-10-10T09:00:00", row_count: 900, null_rates: [] },
        { computed_at: "2026-10-11T09:00:00", row_count: 1000, null_rates: [] },
        { computed_at: "2026-10-12T09:00:00", row_count: 1200, null_rates: [] },
      ],
      freshness: { freshness: "stale", last_refresh_at: "2026-10-09T03:00:00" },
      cadence_hint: "daily" as const,
    };
  }

  it("ok 态：五项指标 + 空值率表 + 抽样标注 + 新鲜度 chip + 趋势图 + 口径标注", async () => {
    install({ quality: okSeed() });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    // 口径标注常驻（任何质量信息界面都在场）
    expect(await screen.findByTestId("quality-caption")).toBeTruthy();
    expect(screen.getByTestId("quality-caption").textContent).toBe(
      "指示性检查，非质量认证",
    );

    // ok 指标先行落位（避免与常驻口径标注的加载态竞态），再验新鲜度 chip。
    const metrics = await screen.findByTestId("quality-metrics");
    expect(metrics.getAttribute("data-state")).toBe("ok");

    // 新鲜度 chip：stale=过期 + 最近刷新时间
    const chip = screen.getByTestId("quality-freshness");
    expect(chip.getAttribute("data-freshness")).toBe("stale");
    expect(chip.textContent).toContain("过期");
    expect(chip.textContent).toContain("最近刷新");

    // ok 指标：行数（本地化千分位）/ 主键唯一性百分比 / 计算时间 / 触发方式
    expect(screen.getByTestId("quality-rows").textContent).toContain("1,200");
    expect(screen.getByTestId("quality-pk").getAttribute("data-declared")).toBe(
      "true",
    );
    expect(screen.getByTestId("quality-pk").textContent).toContain("98.7%");
    expect(screen.getByTestId("quality-computed").textContent).toContain(
      "2026",
    );
    expect(screen.getByTestId("quality-trigger").textContent).toContain("同步");

    // 按列空值率表 + 抽样行数
    const nullRows = screen.getAllByTestId("quality-null-rate");
    expect(nullRows).toHaveLength(2);
    expect(nullRows[1]!.textContent).toContain("amount");
    expect(nullRows[1]!.textContent).toContain("2.3%");
    expect(nullRows[1]!.textContent).toContain("10,000");

    // 抽样上限标注（sample_limit_hit=true）
    expect(screen.getByTestId("quality-sample-note").textContent).toBe(
      "基于抽样（上限 10,000 行）",
    );

    // 行数趋势（≥2 历史点 → echarts SVG 容器）
    expect(screen.getByTestId("quality-trend")).toBeTruthy();
  });

  it("未声明键列：主键唯一性标「未声明键列」，不给比例值", async () => {
    install({
      quality: {
        ...okSeed(),
        latest: {
          ...okSeed().latest,
          pk_declared: false,
          pk_unique_ratio: null,
        },
      },
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    await screen.findByTestId("quality-metrics");
    const pk = screen.getByTestId("quality-pk");
    expect(pk.getAttribute("data-declared")).toBe("false");
    expect(pk.textContent).toContain("未声明键列");
    expect(pk.textContent).not.toContain("%");
  });

  it("failed 态：只示「最近一次快照失败」+原因摘要，不渲染指标冒充当前值", async () => {
    install({
      quality: {
        ...okSeed(),
        latest: {
          computed_at: "2026-10-12T09:00:00",
          triggered_by: "sync",
          status: "failed",
          row_count: 1200,
          pk_declared: true,
          pk_unique_ratio: 0.987,
          null_rates: [],
          sample_limit_hit: false,
          error_text: "engine timeout: quality tool exceeded 30s budget",
        },
      },
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    const failed = await screen.findByTestId("quality-failed");
    expect(failed.getAttribute("data-state")).toBe("failed");
    expect(failed.textContent).toContain("最近一次快照失败");
    expect(screen.getByTestId("quality-failed-error").textContent).toContain(
      "engine timeout",
    );
    // 口径标注仍常驻；ok 指标区绝不出现（旧值不冒充当前值）
    expect(screen.getByTestId("quality-caption")).toBeTruthy();
    expect(screen.queryByTestId("quality-metrics")).toBeNull();
    expect(screen.queryByTestId("quality-pk")).toBeNull();
  });

  it("unsupported 态：「引擎暂不支持质量快照」降级显示，无指标值", async () => {
    install({
      quality: {
        ...okSeed(),
        latest: {
          computed_at: "2026-10-12T09:00:00",
          triggered_by: "create",
          status: "unsupported",
          row_count: 0,
          pk_declared: false,
          pk_unique_ratio: null,
          null_rates: [],
          sample_limit_hit: false,
          error_text: null,
        },
      },
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    const unsupported = await screen.findByTestId("quality-unsupported");
    expect(unsupported.getAttribute("data-state")).toBe("unsupported");
    expect(unsupported.textContent).toContain("引擎暂不支持质量快照");
    expect(screen.getByTestId("quality-caption")).toBeTruthy();
    expect(screen.queryByTestId("quality-metrics")).toBeNull();
  });

  it("暂无快照态：与 unsupported 分开呈现（空态引导 + 无指标）", async () => {
    const backend = install({ quality: { latest: null } });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    const empty = await screen.findByTestId("quality-empty");
    expect(empty.getAttribute("data-state")).toBe("empty");
    expect(empty.textContent).toContain("暂无快照");
    expect(screen.getByTestId("quality-caption")).toBeTruthy();
    expect(screen.queryByTestId("quality-metrics")).toBeNull();
    expect(screen.queryByTestId("quality-unsupported")).toBeNull();
    // 暂无快照时新鲜度=未知（未声明节奏不误判）
    expect(screen.getByTestId("quality-freshness").getAttribute("data-freshness")).toBe(
      "unknown",
    );
    // 未发过刷新请求
    expect(callsOf(backend.calls, "POST", QUALITY_REFRESH_PATH)).toHaveLength(0);
  });

  it("手动「立即检查」：POST refresh 后重拉详情出新快照", async () => {
    const backend = install({ quality: { latest: null } });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("quality-empty");

    fireEvent.click(screen.getByTestId("quality-refresh"));
    await waitFor(() =>
      expect(
        callsOf(backend.calls, "POST", QUALITY_REFRESH_PATH),
      ).toHaveLength(1),
    );
    // 重拉详情（GET 至少两次：首载 + 刷新后）
    await waitFor(() =>
      expect(callsOf(backend.calls, "GET", QUALITY_PATH).length).toBeGreaterThanOrEqual(2),
    );
    // 新快照呈现：ok 指标出现、空态消失
    await waitFor(() =>
      expect(screen.getByTestId("quality-metrics").getAttribute("data-state")).toBe(
        "ok",
      ),
    );
    expect(screen.queryByTestId("quality-empty")).toBeNull();
    expect(screen.getByTestId("quality-trigger").textContent).toContain("手动");
  });

  it("手动「立即检查」失败（如 viewer 403）：错误如实呈现，按钮回可用", async () => {
    const failing = install({
      quality: { latest: null },
      failQualityRefreshStatus: 403,
      failQualityRefreshDetail: "团队成员权限不足（viewer 只读）",
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("quality-empty");
    fireEvent.click(screen.getByTestId("quality-refresh"));
    const error = await screen.findByTestId("quality-refresh-error");
    expect(error.textContent).toContain("viewer 只读");
    expect(
      (screen.getByTestId("quality-refresh") as HTMLButtonElement).disabled,
    ).toBe(false);
    expect(callsOf(failing.calls, "POST", QUALITY_REFRESH_PATH)).toHaveLength(1);
  });

  it("手动检查进行中禁用防重复（悬挂请求期间按钮 disabled）", async () => {
    let release!: () => void;
    const hold = new Promise<void>((resolve) => {
      release = resolve;
    });
    const backend = install({
      quality: { latest: null },
      holdQualityRefresh: hold,
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    await screen.findByTestId("quality-empty");

    fireEvent.click(screen.getByTestId("quality-refresh"));
    await waitFor(() =>
      expect(
        (screen.getByTestId("quality-refresh") as HTMLButtonElement).disabled,
      ).toBe(true),
    );
    // 悬挂期间不重复发请求
    fireEvent.click(screen.getByTestId("quality-refresh"));
    expect(
      callsOf(backend.calls, "POST", QUALITY_REFRESH_PATH),
    ).toHaveLength(1);

    release();
    await waitFor(() =>
      expect(
        (screen.getByTestId("quality-refresh") as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    await screen.findByTestId("quality-metrics");
  });

  it("质量详情加载失败：错误态 + 重试（既有错误面语义）", async () => {
    const backend = install({ failQualityStatus: 500 });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    expect(await screen.findByTestId("quality-error")).toBeTruthy();
    // 口径标注只在数据面渲染（错误面走重试，不呈现指标）
    expect(screen.queryByTestId("quality-metrics")).toBeNull();

    fireEvent.click(screen.getByTestId("quality-retry"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "GET", QUALITY_PATH).length).toBe(2),
    );
  });
});
