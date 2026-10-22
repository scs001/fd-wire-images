// 溯源视图组件测试（wire-provenance-ledger 任务组 8：8.1/8.2）：
// 状态条（档位下拉/链完整性徽标/锚定进度/起点标注）、事件时间线（倒序+中文
// 友好名+负载摘要+加载更早 before_seq 分页）、tier 切换 POST 载荷与刷新、
// 证据包带鉴权头下载。fetch 为本文件内置契约假后端（零网络，不改 testing.ts）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import {
  DatasetDetail,
  provenanceEventTypeKey,
  summarizeProvenancePayload,
} from "./DatasetDetail";
import {
  fetchEvidencePackage,
  type Dataset,
  type ProvenanceEvent,
  type ProvenanceOverview,
  type ProvenanceStatus,
  type ProvenanceTier,
} from "./api";
import { makeIdToken } from "./testing";

const CUSTOMER = "demo-customer";
const NOW = "2026-10-05T10:00:00";
const KEY = "ds_key_1";

function makeDataset(): Dataset {
  return {
    id: "ds-1",
    key: KEY,
    name: "门店销量",
    source_kind: "upload",
    connector_id: null,
    source_ref: null,
    columns: [{ name: "id", type: "INTEGER" }],
    row_count: 0,
    size_bytes: 0,
    sync_cron: null,
    sync_status: "ok",
    paused_reason: null,
    created_at: NOW,
    updated_at: NOW,
    customer_id: CUSTOMER,
  };
}

function makeEvent(partial: Partial<ProvenanceEvent> & Pick<ProvenanceEvent, "seq" | "type">): ProvenanceEvent {
  return {
    event_id: `evt-${partial.seq}`,
    ts_platform: `2026-10-01T09:${String(partial.seq).padStart(2, "0")}:00`,
    actor: { kind: "user", id: "member-a" },
    subject: { kind: "dataset", key: KEY },
    payload: {},
    ...partial,
  };
}

function makeStatus(partial: Partial<ProvenanceStatus> = {}): ProvenanceStatus {
  return {
    tier: "chain_only",
    integrity: { ok: true, checked: 128 },
    anchored_through: { epoch: 6, backend: "OpenTimestamps" },
    first_event_ts: "2026-09-30T08:00:00",
    note: null,
    ...partial,
  };
}

function makeOverview(partial: Partial<ProvenanceOverview> = {}): ProvenanceOverview {
  return {
    ledger: "wire-provenance-v1",
    last_seq: 300,
    integrity: { ok: true, checked: 300 },
    anchoring: { backend: "OpenTimestamps", last_sealed_epoch: 7, lag_events: 3 },
    ...partial,
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

/** 契约假后端：数据集详情常规路由 + /api/v1/provenance/* 全套。 */
function install(options: {
  status: ProvenanceStatus;
  events?: ProvenanceEvent[];
  overview?: ProvenanceOverview | null;
  evidenceDisposition?: string;
}) {
  const state = {
    status: options.status,
    events: [...(options.events ?? [])].sort((a, b) => b.seq - a.seq),
  };
  const calls: FetchCall[] = [];
  const fetchImpl = ((_input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(_input), "http://console.test");
    const method = (init?.method ?? "GET").toUpperCase();
    const headers: Record<string, string> = {};
    new Headers(init?.headers as HeadersInit).forEach((v, k) => {
      headers[k.toLowerCase()] = v;
    });
    calls.push({
      method,
      url,
      headers,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    });

    const path = url.pathname;
    if (path === "/api/v1/datasets/ds-1") return jsonResponse(makeDataset());
    if (path === "/api/v1/datasets/ds-1/rows")
      return jsonResponse({ columns: [], rows: [], total: 0 });
    if (path === "/api/v1/datasets/ds-1/corrections") return jsonResponse([]);
    if (path === "/api/v1/datasets/ds-1/syncs") return jsonResponse([]);

    if (path === `/api/v1/provenance/datasets/${KEY}/status`)
      return jsonResponse(state.status);
    if (path === `/api/v1/provenance/datasets/${KEY}/events`) {
      const before = Number(url.searchParams.get("before_seq") ?? "0");
      const limit = Number(url.searchParams.get("limit") ?? "50") || 50;
      const pool = state.events.filter((event) =>
        before > 0 ? event.seq < before : true,
      );
      const page = pool.slice(0, limit);
      return jsonResponse({ events: page, has_more: pool.length > page.length });
    }
    if (path === `/api/v1/provenance/datasets/${KEY}/tier` && method === "POST") {
      const tier = (calls[calls.length - 1]!.body as { tier: ProvenanceTier }).tier;
      state.status = { ...state.status, tier };
      return jsonResponse({ tier });
    }
    if (path === "/api/v1/provenance/overview") {
      if (options.overview === null)
        return jsonResponse({ detail: "overview unavailable" }, 500);
      return jsonResponse(options.overview ?? makeOverview());
    }
    if (path === "/api/v1/provenance/evidence-package") {
      const headers = new Headers({ "content-type": "application/octet-stream" });
      if (options.evidenceDisposition)
        headers.set("content-disposition", options.evidenceDisposition);
      return {
        ok: true,
        status: 200,
        json: () => Promise.reject(new Error("binary body")),
        blob: () =>
          Promise.resolve(new Blob(["ledger"], { type: "application/octet-stream" })),
        headers,
      } as unknown as Response;
    }
    return jsonResponse({ detail: "not found" }, 404);
  }) as unknown as typeof fetch;
  vi.stubGlobal("fetch", fetchImpl);
  return { calls, state };
}

function callsOf(calls: FetchCall[], method: string, path: string): FetchCall[] {
  return calls.filter((call) => call.method === method && call.url.pathname === path);
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  // 登录会话种子：请求自动附 Authorization: Bearer（假后端不校验值，
  // 证据包下载断言按 Bearer 前缀匹配）。
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
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

describe("DatasetDetail 溯源区块：状态条（任务 8.1）", () => {
  it("档位下拉初始为状态值，链完整性徽标绿色「链完整·已核 N 条」，锚定进度含 backend/epoch/滞后", async () => {
    install({
      status: makeStatus({ tier: "anchored_batch", integrity: { ok: true, checked: 42 } }),
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    const section = await screen.findByTestId("provenance-section");
    expect(section).toBeTruthy();
    // status 异步到达：用 findBy 等待渲染，避免与加载态竞态
    const select = await screen.findByTestId("provenance-tier-select");
    expect((select as HTMLSelectElement).value).toBe("anchored_batch");
    // 四档齐全（i18n 中文标签）
    expect(
      [...select.querySelectorAll("option")].map((option) => option.textContent),
    ).toEqual(["关闭（不入账）", "仅哈希链", "周期锚定", "即时锚定"]);

    const integrity = screen.getByTestId("provenance-integrity");
    expect(integrity.getAttribute("data-ok")).toBe("true");
    expect(integrity.textContent).toContain("链完整");
    expect(integrity.textContent).toContain("已核 42 条");

    const anchoring = screen.getByTestId("provenance-anchoring");
    expect(anchoring.getAttribute("data-backend")).toBe("OpenTimestamps");
    expect(anchoring.getAttribute("data-epoch")).toBe("6");
    expect(anchoring.textContent).toContain("已封存至 epoch 6");
    // 滞后事件数来自企业 overview
    expect(anchoring.textContent).toContain("滞后 3 条事件");

    // 无 note 时展示首条事件时间作为溯源起点
    expect(screen.queryByTestId("provenance-note")).toBeNull();
    expect(screen.getByTestId("provenance-first-event").textContent).toContain(
      "溯源起点",
    );
  });

  it("链异常如实红色「链异常」；未配置锚定后端展示占位", async () => {
    install({
      status: makeStatus({
        integrity: { ok: false, checked: 40 },
        anchored_through: { epoch: null, backend: null },
      }),
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    const integrity = await screen.findByTestId("provenance-integrity");
    expect(integrity.getAttribute("data-ok")).toBe("false");
    expect(integrity.textContent).toContain("链异常");
    expect(screen.getByTestId("provenance-anchoring").textContent).toContain(
      "未配置锚定后端",
    );
  });

  it("调档：切换下拉即 POST tier 载荷，成功后刷新状态并回显新档位", async () => {
    const backend = install({ status: makeStatus({ tier: "chain_only" }) });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    const select = await screen.findByTestId("provenance-tier-select");
    expect((select as HTMLSelectElement).value).toBe("chain_only");

    const statusPath = `/api/v1/provenance/datasets/${KEY}/status`;
    const statusCallsBefore = callsOf(backend.calls, "GET", statusPath).length;

    fireEvent.change(select, { target: { value: "anchored_batch" } });
    const tierPath = `/api/v1/provenance/datasets/${KEY}/tier`;
    await waitFor(() =>
      expect(callsOf(backend.calls, "POST", tierPath).length).toBe(1),
    );
    expect(callsOf(backend.calls, "POST", tierPath)[0]!.body).toEqual({
      tier: "anchored_batch",
    });
    expect(callsOf(backend.calls, "POST", tierPath)[0]!.headers["authorization"]).toMatch(
      /^Bearer /,
    );
    // 成功后刷新状态（status 再次 GET），下拉回显新档位
    await waitFor(() =>
      expect(callsOf(backend.calls, "GET", statusPath).length).toBeGreaterThan(
        statusCallsBefore,
      ),
    );
    await waitFor(() =>
      expect(
        screen.getByTestId("provenance-tier-select").getAttribute("data-tier"),
      ).toBe("anchored_batch"),
    );
    expect(screen.queryByTestId("provenance-tier-error")).toBeNull();
  });
});

describe("DatasetDetail 溯源区块：事件时间线（任务 8.1）", () => {
  const events: ProvenanceEvent[] = [
    makeEvent({
      seq: 5,
      type: "dataset.correction.added",
      payload: {
        op: "update",
        row_id: "hash:seed-1",
        old: { amount: 10 },
        new: { amount: 99 },
      },
    }),
    makeEvent({ seq: 4, type: "ledger.epoch.sealed", payload: { epoch: 6 } }),
    makeEvent({ seq: 3, type: "cell.registered", payload: { cell: "c-77" } }),
    makeEvent({ seq: 2, type: "mystery.custom.type", payload: {} }),
    makeEvent({
      seq: 1,
      type: "dataset.upload.registered",
      payload: { filename: "sales.csv" },
    }),
  ];

  it("倒序渲染：类型中文友好名（未知类型原样）、操作者、负载摘要平铺", async () => {
    install({ status: makeStatus(), events });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    const rows = await screen.findAllByTestId("provenance-event");
    expect(rows.length).toBe(5);
    expect(
      screen.getAllByTestId("provenance-event-type").map((el) => el.textContent),
    ).toEqual(["修正", "锚定封存", "隔离单元生命周期", "mystery.custom.type", "上传登记"]);
    // 操作者与序号徽标
    expect(
      screen.getAllByTestId("provenance-event-actor").map((el) => el.textContent),
    ).toEqual(Array(5).fill("member-a"));
    expect(
      screen.getAllByTestId("provenance-event-seq").map((el) => el.textContent),
    ).toEqual(["#5", "#4", "#3", "#2", "#1"]);
    // 负载摘要：键值平铺（对象 JSON 化、不过度展开）
    const first = screen.getAllByTestId("provenance-event-payload")[0]!;
    expect(first.textContent).toContain("op=update");
    expect(first.textContent).toContain("row_id=hash:seed-1");
    expect(first.textContent).toContain('old={"amount":10}');
    // 时间字段渲染（formatServerDate 本地化）
    expect(
      screen.getAllByTestId("provenance-event-ts").every((el) =>
        /\d/.test(el.textContent ?? ""),
      ),
    ).toBe(true);
    // 一页装下：无「加载更早」
    expect(screen.queryByTestId("provenance-load-earlier")).toBeNull();
  });

  it("分页：默认 limit 一页装不下时出现「加载更早」，点击带 before_seq 追加更早事件", async () => {
    const many = Array.from({ length: 60 }, (_, i) =>
      makeEvent({ seq: 60 - i, type: "dataset.snapshot.refreshed" }),
    );
    const backend = install({ status: makeStatus(), events: many });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    const eventsPath = `/api/v1/provenance/datasets/${KEY}/events`;
    await waitFor(() =>
      expect(screen.getAllByTestId("provenance-event").length).toBe(50),
    );
    // 首屏不带 before_seq
    expect(
      callsOf(backend.calls, "GET", eventsPath)[0]!.url.searchParams.get("before_seq"),
    ).toBeNull();

    fireEvent.click(screen.getByTestId("provenance-load-earlier"));
    await waitFor(() =>
      expect(screen.getAllByTestId("provenance-event").length).toBe(60),
    );
    // 追加页以当前最老 seq 为游标
    const second = callsOf(backend.calls, "GET", eventsPath)[1]!;
    expect(second.url.searchParams.get("before_seq")).toBe("11");
    // has_more=false 后按钮消失
    await waitFor(() =>
      expect(screen.queryByTestId("provenance-load-earlier")).toBeNull(),
    );
  });

  it("空时间线如实展示「还没有入账事件」", async () => {
    install({ status: makeStatus({ note: "启用前无历史事件", first_event_ts: null }), events: [] });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    expect(await screen.findByTestId("provenance-timeline-empty")).toBeTruthy();
    expect(screen.queryByTestId("provenance-event")).toBeNull();
  });
});

describe("DatasetDetail 溯源区块：起点如实标注（任务 8.2）", () => {
  it("note 非空时如实展示「启用前无历史事件」类提示（优先于 first_event_ts）", async () => {
    install({
      status: makeStatus({
        note: "溯源功能启用前无历史事件——溯源自启用时点开始。",
        first_event_ts: null,
      }),
    });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);
    const note = await screen.findByTestId("provenance-note");
    expect(note.textContent).toContain("启用前无历史事件");
    expect(note.textContent).toContain("自启用时点");
    expect(screen.queryByTestId("provenance-first-event")).toBeNull();
  });
});

describe("DatasetDetail 溯源区块：证据包下载（任务 8.1）", () => {
  it("点击下载：带 Authorization Bearer 请求 evidence-package，blob 经 URL.createObjectURL 触发浏览器下载", async () => {
    const createObjectURL = vi.fn(() => "blob:evidence");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal(
      "URL",
      Object.assign(URL, { createObjectURL, revokeObjectURL }),
    );
    const backend = install({ status: makeStatus() });
    render(<DatasetDetail datasetId="ds-1" onBack={() => {}} />);

    fireEvent.click(await screen.findByTestId("provenance-evidence-download"));
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    const call = backend.calls.find(
      (c) => c.url.pathname === "/api/v1/provenance/evidence-package",
    );
    expect(call?.method).toBe("GET");
    expect(call?.headers["authorization"]).toMatch(/^Bearer /);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:evidence");
    expect(screen.queryByTestId("provenance-evidence-error")).toBeNull();
  });
});

describe("provenance API client 细节", () => {
  it("fetchEvidencePackage：缺 Content-Disposition 时按 evidence-package-<日期>.jsonl.bin 命名", async () => {
    const blob = new Blob(["ledger"]);
    vi.stubGlobal(
      "fetch",
      (() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({}),
          blob: () => Promise.resolve(blob),
          headers: new Headers(),
        } as unknown as Response)) as unknown as typeof fetch,
    );
    const pkg = await fetchEvidencePackage();
    expect(pkg.blob).toBe(blob);
    expect(pkg.filename).toMatch(
      /^evidence-package-\d{4}-\d{2}-\d{2}\.jsonl\.bin$/,
    );
  });

  it("fetchEvidencePackage：响应 Content-Disposition 文件名优先", async () => {
    vi.stubGlobal(
      "fetch",
      (() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({}),
          blob: () => Promise.resolve(new Blob(["x"])),
          headers: new Headers({
            "content-disposition": 'attachment; filename="evidence-42.bin"',
          }),
        } as unknown as Response)) as unknown as typeof fetch,
    );
    const pkg = await fetchEvidencePackage();
    expect(pkg.filename).toBe("evidence-42.bin");
  });

  it("summarizeProvenancePayload：平铺键值、对象 JSON 化、长值截断、超 4 项计余", () => {
    expect(summarizeProvenancePayload({ a: 1, b: "x", c: null })).toBe(
      "a=1, b=x, c=null",
    );
    expect(summarizeProvenancePayload({ nested: { k: [1, 2] } })).toBe(
      'nested={"k":[1,2]}',
    );
    expect(summarizeProvenancePayload({ long: "y".repeat(100) })).toBe(
      `long=${"y".repeat(48)}…`,
    );
    expect(summarizeProvenancePayload({ a: 1, b: 2, c: 3, d: 4, e: 5, f: 6 })).toBe(
      "a=1, b=2, c=3, d=4, +2",
    );
    expect(summarizeProvenancePayload(null)).toBe("");
    expect(summarizeProvenancePayload({})).toBe("");
  });

  it("provenanceEventTypeKey：已知类型精确映射、cell.* 前缀归并、未知返回 null（原样展示）", () => {
    expect(provenanceEventTypeKey("dataset.upload.registered")).toBe("uploadRegistered");
    expect(provenanceEventTypeKey("dataset.correction.added")).toBe("correctionAdded");
    expect(provenanceEventTypeKey("dataset.correction.reverted")).toBe("correctionReverted");
    expect(provenanceEventTypeKey("dataset.snapshot.refreshed")).toBe("snapshotRefreshed");
    expect(provenanceEventTypeKey("dataset.correction.dangling")).toBe("correctionDangling");
    expect(provenanceEventTypeKey("dataset.dataset.deleted")).toBe("datasetDeleted");
    expect(provenanceEventTypeKey("ledger.epoch.sealed")).toBe("epochSealed");
    expect(provenanceEventTypeKey("cell.registered")).toBe("cellLifecycle");
    expect(provenanceEventTypeKey("cell.deregistered")).toBe("cellLifecycle");
    expect(provenanceEventTypeKey("future.unknown.type")).toBeNull();
  });
});
