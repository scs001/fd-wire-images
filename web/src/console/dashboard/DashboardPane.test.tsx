// 产物页 dashboard 集成测试（wire-dashboard-renderer 任务 4.1 / 5.1，组件级）：
// - dashboard kind 卡片「查看看板」内嵌展开（不走路由）→ 三类面板渲染；
// - 「编辑」切编辑器 → 改标题保存 v2（PUT）→ 返回查看新版本生效；
// - legacy spec 只读 + 编辑禁用；archived 只读；非 dashboard kind 无入口。
// wire-dashboard-refresh 任务 2.2：查看态「刷新数据」——成功流（POST refresh
// 落新版本 → 提示 + 重拉详情重渲染）/ 失败态（502/403 detail 如实）/ 409 单飞
// 进行中提示 / archived 无按钮。
// wire-dashboard-query-contract 任务 2.3/2.4：契约刷新响应呈现（mode=contract
// 按面板统计 + 失败原因列表；ai 响应不回归）；带 query 面板渲染仍读 snapshot。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "../../i18n";
import i18n from "../../i18n";
import { ArtifactsPage } from "../ArtifactsPage";
import { installConsoleBackend, seedConsoleSession } from "../testing";

/** v1 仪表盘信封：kpi + line + table 三面板。 */
const V1_DASHBOARD_SPEC = {
  title: "宏观仪表盘",
  spec: {
    title: "宏观仪表盘",
    panels: [
      {
        id: "k1",
        type: "kpi",
        title: "CPI 同比",
        span: 1,
        snapshot: { columns: ["value"], rows: [[2.1]] },
        config: { unit: "%", subtitle: "2026-09" },
      },
      {
        id: "l1",
        type: "line",
        title: "PMI 走势",
        span: 2,
        snapshot: {
          columns: ["月份", "PMI"],
          rows: [
            ["7月", 49.4],
            ["8月", 49.1],
          ],
        },
      },
      {
        id: "t1",
        type: "table",
        title: "月度明细",
        span: 3,
        snapshot: {
          columns: ["月份", "值"],
          rows: [
            ["7月", 49.4],
            ["8月", 49.1],
          ],
        },
      },
    ],
  },
};

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  seedConsoleSession({ sub: "demo-customer" });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

function install() {
  const backend = installConsoleBackend([]);
  // 31：v1 仪表盘（draft）；32：非 dashboard kind（入口零变化对照）；
  // 33：legacy 自由 JSON 仪表盘；34：archived 仪表盘。
  backend.addLifecycleArtifact({
    id: 31,
    kind: "dashboard",
    title: "宏观仪表盘",
    version: 1,
    spec: V1_DASHBOARD_SPEC,
  });
  backend.addLifecycleArtifact({
    id: 32,
    kind: "alert_rule",
    title: "煤价告警",
    spec: { title: "煤价告警", spec: { threshold: 0.05 } },
  });
  backend.addLifecycleArtifact({
    id: 33,
    kind: "dashboard",
    title: "旧版看板",
    spec: { title: "旧版看板", spec: { indicators: ["CN_CPI_YOY"], layout: "2x2" } },
  });
  backend.addLifecycleArtifact({
    id: 34,
    kind: "dashboard",
    title: "已归档看板",
    status: "archived",
    spec: V1_DASHBOARD_SPEC,
  });
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

function rowOf(id: number): HTMLElement {
  return screen
    .getAllByTestId("artifact-item")
    .find((el) => el.getAttribute("data-artifact-id") === String(id))!;
}

describe("产物页 dashboard 集成（查看看板 → 编辑 → 保存 v2 → 新版本生效）", () => {
  it("dashboard kind 卡片「查看看板」内嵌展开：三类面板渲染 + 快照口径", async () => {
    install();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    // v1 仪表盘与非 dashboard kind 各一个入口（legacy/archived 也是 dashboard kind）
    expect(screen.getAllByTestId("artifact-dashboard-view")).toHaveLength(3);

    fireEvent.click(within(rowOf(31)).getByTestId("artifact-dashboard-view"));
    expect(await screen.findByTestId("dashboard-pane")).toBeTruthy();
    // 三类面板：kpi 大数 + line 图容器 + table 简表
    expect(screen.getByTestId("dashboard-kpi-value").textContent).toBe("2.1");
    expect(screen.getAllByTestId("dashboard-chart")).toHaveLength(1);
    expect(screen.getByTestId("dashboard-table").querySelectorAll("tbody tr")).toHaveLength(2);
    expect(screen.getByTestId("dashboard-snapshot-note").textContent).toContain("快照数据");

    // 非 dashboard kind 行没有入口
    expect(within(rowOf(32)).queryByTestId("artifact-dashboard-view")).toBeNull();

    // 再点一次收起
    fireEvent.click(within(rowOf(31)).getByTestId("artifact-dashboard-view"));
    await waitFor(() => {
      expect(screen.queryByTestId("dashboard-pane")).toBeNull();
    });
  });

  it("E2E：编辑改标题保存 v2 → 返回查看新版本生效（行版本号同步）", async () => {
    const backend = install();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    fireEvent.click(within(rowOf(31)).getByTestId("artifact-dashboard-view"));
    await screen.findByTestId("dashboard-pane");

    // 切编辑器：改面板标题 + kpi 值
    fireEvent.click(screen.getByTestId("dashboard-edit"));
    const editor = await screen.findByTestId("dashboard-editor");
    const kpiRow = within(editor)
      .getAllByTestId("editor-panel")
      .find((el) => el.getAttribute("data-panel-id") === "k1")!;
    fireEvent.change(within(kpiRow).getByTestId("editor-panel-title"), {
      target: { value: "CPI 环比" },
    });
    fireEvent.change(within(kpiRow).getByTestId("editor-kpi-value"), {
      target: { value: "1.8" },
    });
    fireEvent.click(within(editor).getByTestId("editor-save"));

    // PUT 信封 + 新版本提示 + 列表行版本号 v2
    expect((await screen.findByTestId("editor-saved-version")).textContent).toContain("v2");
    const put = backend.calls.find((c) => c.method === "PUT");
    expect(put?.url.pathname).toBe("/api/v1/artifacts/31");
    expect(JSON.stringify(put?.body)).toContain("CPI 环比");
    await waitFor(() => {
      expect(within(rowOf(31)).getByTestId("artifact-version").textContent).toBe("v2");
    });

    // 返回查看：新版本内容生效（保存后基线已推进，不触发 confirm）
    const confirm = vi.fn();
    vi.stubGlobal("confirm", confirm); // happy-dom 无原生 confirm：打桩
    fireEvent.click(within(editor).getByTestId("editor-exit"));
    expect(confirm).not.toHaveBeenCalled();
    expect(
      screen
        .getAllByTestId("dashboard-panel-title")
        .some((el) => el.textContent === "CPI 环比"),
    ).toBe(true);
    expect(screen.getByTestId("dashboard-kpi-value").textContent).toBe("1.8");
  });

  it("legacy 仪表盘：旧版自由格式 + 只读 JSON；编辑入口禁用", async () => {
    install();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    fireEvent.click(within(rowOf(33)).getByTestId("artifact-dashboard-view"));
    expect(await screen.findByTestId("dashboard-legacy")).toBeTruthy();
    expect(screen.getByTestId("dashboard-legacy-json").textContent).toContain(
      "CN_CPI_YOY",
    );
    expect((screen.getByTestId("dashboard-edit") as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it("archived 仪表盘：只读徽标 + 编辑入口禁用（渲染不受影响）+ 无刷新入口", async () => {
    install();
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");

    fireEvent.click(within(rowOf(34)).getByTestId("artifact-dashboard-view"));
    expect(await screen.findByTestId("dashboard-pane")).toBeTruthy();
    expect(screen.getByTestId("dashboard-renderer")).toBeTruthy();
    expect(screen.getByTestId("dashboard-kpi-value").textContent).toBe("2.1");
    expect((screen.getByTestId("dashboard-edit") as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(screen.queryByTestId("dashboard-refresh")).toBeNull(); // archived 不可刷
  });

  it("看板加载失败如实呈现错误态", async () => {
    const failing = installConsoleBackend([]);
    failing.addLifecycleArtifact({
      id: 41,
      kind: "dashboard",
      title: "幽灵看板",
      spec: V1_DASHBOARD_SPEC,
    });
    // 覆盖详情路由：GET /api/v1/artifacts/41 一律 404（列表仍走假后端）
    const wrapped = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://console.test");
      if (
        url.pathname === "/api/v1/artifacts/41" &&
        (init?.method ?? "GET").toUpperCase() === "GET"
      ) {
        return { ok: false, status: 404, json: () => Promise.resolve({ detail: "artifact not found" }) };
      }
      return failing.fetchImpl(input as RequestInfo, init);
    }) as unknown as typeof fetch;
    vi.stubGlobal("fetch", wrapped);

    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");
    fireEvent.click(within(rowOf(41)).getByTestId("artifact-dashboard-view"));
    expect(await screen.findByTestId("dashboard-load-error")).toBeTruthy();
  });
});

describe("看板手动刷新（wire-dashboard-refresh 任务 2.1/2.2）", () => {
  /** 刷新后的新快照：kpi 值 2.1 → 3.4（重渲染断言锚点）。 */
  const REFRESHED_SPEC = JSON.parse(JSON.stringify(V1_DASHBOARD_SPEC)) as typeof V1_DASHBOARD_SPEC;
  (
    REFRESHED_SPEC.spec.panels[0]!.snapshot as { rows: Array<Array<string | number | null>> }
  ).rows[0]![0] = 3.4;

  /** 包一层 fetch：POST /artifacts/31/refresh 注入失败态（其余透传真后端）。 */
  function withRefreshFailure(
    backend: ReturnType<typeof installConsoleBackend>,
    status: number,
    detail: string,
  ): typeof fetch {
    return (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://console.test");
      if (
        url.pathname === "/api/v1/artifacts/31/refresh" &&
        (init?.method ?? "GET").toUpperCase() === "POST"
      ) {
        return { ok: false, status, json: () => Promise.resolve({ detail }) };
      }
      return backend.fetchImpl(input as RequestInfo, init);
    }) as unknown as typeof fetch;
  }

  /** 打开 31 号看板的查看态（返回 pane 就绪后的断言起点）。 */
  async function openDashboard31() {
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");
    fireEvent.click(within(rowOf(31)).getByTestId("artifact-dashboard-view"));
    await screen.findByTestId("dashboard-pane");
    expect(screen.getByTestId("dashboard-kpi-value").textContent).toBe("2.1");
  }

  it("成功流：进行中 loading 禁用 → v2 提示 → 重拉详情重渲染 + 行版本号同步", async () => {
    const backend = install();
    let refreshed = false;
    const refreshCalls: string[] = [];
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const wrapped = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://console.test");
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.pathname === "/api/v1/artifacts/31/refresh" && method === "POST") {
        refreshCalls.push(`${method} ${url.pathname}`);
        return gate.then(() => ({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ version: 2, refreshed_at: "2026-10-11T09:00:00" }),
        }));
      }
      if (refreshed && url.pathname === "/api/v1/artifacts/31" && method === "GET") {
        // 刷新落地后的详情：v2 + 新快照（模拟服务端已按刷新结果落版本）
        const base = (await backend.fetchImpl(input, init)) as {
          json: () => Promise<Record<string, unknown>>;
        };
        const body = await base.json();
        return {
          ok: true,
          status: 200,
          json: () => Promise.resolve({ ...body, version: 2, spec: REFRESHED_SPEC }),
        };
      }
      return backend.fetchImpl(input as RequestInfo, init);
    }) as unknown as typeof fetch;
    vi.stubGlobal("fetch", wrapped);

    await openDashboard31();

    fireEvent.click(screen.getByTestId("dashboard-refresh"));
    // 进行中：按钮禁用 + loading 文案（POST 已发出、门未放行）
    expect(refreshCalls).toEqual(["POST /api/v1/artifacts/31/refresh"]);
    expect((screen.getByTestId("dashboard-refresh") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("dashboard-refresh").textContent).toContain("刷新中");

    release();
    refreshed = true;

    // 成功：新版本号提示 + 重拉详情重渲染（2.1 → 3.4）
    expect((await screen.findByTestId("dashboard-refresh-success")).textContent).toContain("v2");
    await waitFor(() => {
      expect(screen.getByTestId("dashboard-kpi-value").textContent).toBe("3.4");
    });
    // 行版本号经 onSaved 同步为 v2，按钮恢复可用
    await waitFor(() => {
      expect(within(rowOf(31)).getByTestId("artifact-version").textContent).toBe("v2");
    });
    expect((screen.getByTestId("dashboard-refresh") as HTMLButtonElement).disabled).toBe(false);
  });

  it("失败态（502）：引擎失败 detail 如实呈现，旧快照不受影响", async () => {
    const backend = install();
    vi.stubGlobal(
      "fetch",
      withRefreshFailure(backend, 502, "刷新执行失败：LLM 输出解析失败（不是合法 spec JSON）"),
    );

    await openDashboard31();
    fireEvent.click(screen.getByTestId("dashboard-refresh"));

    expect((await screen.findByTestId("dashboard-refresh-error")).textContent).toContain(
      "LLM 输出解析失败",
    );
    // 零半写：视图仍是旧快照；按钮恢复可重试
    expect(screen.getByTestId("dashboard-kpi-value").textContent).toBe("2.1");
    expect((screen.getByTestId("dashboard-refresh") as HTMLButtonElement).disabled).toBe(false);
  });

  it("档位边界（403）：免费档提示 detail 如实呈现（升档引导）", async () => {
    const backend = install();
    vi.stubGlobal(
      "fetch",
      withRefreshFailure(backend, 403, "数据刷新需开通基础档及以上（当前档位：免费档）"),
    );

    await openDashboard31();
    fireEvent.click(screen.getByTestId("dashboard-refresh"));

    expect((await screen.findByTestId("dashboard-refresh-error")).textContent).toContain(
      "基础档及以上",
    );
  });

  it("单飞（409）：刷新进行中提示，不显示后端机口径 detail", async () => {
    const backend = install();
    vi.stubGlobal(
      "fetch",
      withRefreshFailure(backend, 409, "refresh in progress"),
    );

    await openDashboard31();
    fireEvent.click(screen.getByTestId("dashboard-refresh"));

    const error = await screen.findByTestId("dashboard-refresh-error");
    expect(error.textContent).toContain("刷新进行中");
    expect(error.textContent).not.toContain("refresh in progress"); // 固定人话文案
    expect((screen.getByTestId("dashboard-refresh") as HTMLButtonElement).disabled).toBe(false);
  });
});

// ── 定时刷新小节（wire-refresh-scheduler 任务 4.1/4.2）──────────────────────
// 看板查看态：创建流（POST body 断言：target_type=artifact）/暂停恢复/错误
// 展示；archived 与编辑态不出小节。schedules 端点走 testing.ts 假后端。
describe("看板定时刷新小节（wire-refresh-scheduler 4.1/4.2）", () => {
  /** 打开指定产物 id 的看板查看态（复用 install() 四产物种子）。 */
  async function openView(id: number) {
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");
    fireEvent.click(within(rowOf(id)).getByTestId("artifact-dashboard-view"));
    await screen.findByTestId("dashboard-pane");
  }

  /** 包装假后端：拦截 POST /api/v1/refresh-schedules 注入失败态，其余透传。 */
  function withScheduleCreateFailure(
    backend: ReturnType<typeof installConsoleBackend>,
    status: number,
    detail: string,
  ): typeof fetch {
    return (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://console.test");
      if (
        url.pathname === "/api/v1/refresh-schedules" &&
        (init?.method ?? "GET").toUpperCase() === "POST"
      ) {
        return { ok: false, status, json: () => Promise.resolve({ detail }) };
      }
      return backend.fetchImpl(input as RequestInfo, init);
    }) as unknown as typeof fetch;
  }

  it("创建流：预设 6h/24h/7d，选 24h 创建 → POST body（artifact）断言 → 状态行", async () => {
    const backend = install();
    await openView(31);

    const section = await screen.findByTestId("schedule-section");
    expect(section.getAttribute("data-target-type")).toBe("artifact");
    // 等列表落地（无调度 → 创建面）再交互，避开 loading 态竞态。
    const create = await within(section).findByTestId("schedule-create");
    // 预设档 = artifact 地板（360 分钟）起步：6h/24h/7d
    expect(
      within(create)
        .getAllByTestId("schedule-interval")
        .map((el) => el.getAttribute("data-minutes")),
    ).toEqual(["360", "1440", "10080"]);
    // 默认选中首档（360）；改选 24h（1440）后创建
    fireEvent.click(within(create).getAllByTestId("schedule-interval")[1]!);
    fireEvent.click(within(create).getByTestId("schedule-create-submit"));

    expect(await screen.findByTestId("schedule-row")).toBeTruthy();
    const post = backend.calls.find(
      (c) => c.method === "POST" && c.url.pathname === "/api/v1/refresh-schedules",
    );
    expect(post?.body).toEqual({
      target_type: "artifact",
      target_id: "31",
      interval_minutes: 1440,
    });
    expect(screen.getByTestId("schedule-row").getAttribute("data-paused")).toBe(
      "false",
    );
  });

  it("archived 看板无小节（调度器自动清理其调度行）；切编辑态后小节隐藏", async () => {
    install();
    await openView(34);
    expect(screen.queryByTestId("schedule-section")).toBeNull();

    // 31（draft v1）切编辑器：编辑态不管理调度
    await openView(31);
    expect(await screen.findByTestId("schedule-section")).toBeTruthy();
    fireEvent.click(screen.getByTestId("dashboard-edit"));
    await screen.findByTestId("dashboard-editor");
    expect(screen.queryByTestId("schedule-section")).toBeNull();
  });

  it("暂停/恢复：POST /{id}/pause → 已暂停；再点恢复 → 回调度中（含上次结果/错误展示）", async () => {
    const backend = install();
    backend.addRefreshSchedule({
      id: 5,
      target_type: "artifact",
      target_id: "31",
      interval_minutes: 360,
      next_run_at: "2026-10-11T14:00:00",
      paused: false,
      last_status: "failed",
      last_run_at: "2026-10-11T08:00:00",
      last_error: "LLM 输出解析失败",
      created_at: "2026-10-10T08:00:00",
    });
    await openView(31);

    // 状态行如实呈现：上次结果 failed + 上次错误
    await screen.findByTestId("schedule-row");
    expect(
      screen.getByTestId("schedule-last-status").getAttribute("data-status"),
    ).toBe("failed");
    expect(screen.getByTestId("schedule-last-error").textContent).toContain(
      "LLM 输出解析失败",
    );

    // 暂停：POST /5/pause → 已暂停徽标
    fireEvent.click(screen.getByTestId("schedule-toggle"));
    await waitFor(() => {
      expect(screen.getByTestId("schedule-row").getAttribute("data-paused")).toBe(
        "true",
      );
    });
    expect(
      backend.calls.find(
        (c) =>
          c.method === "POST" &&
          c.url.pathname === "/api/v1/refresh-schedules/5/pause",
      ),
    ).toBeTruthy();

    // 恢复：POST /5/resume → 回调度中
    fireEvent.click(screen.getByTestId("schedule-toggle"));
    await waitFor(() => {
      expect(screen.getByTestId("schedule-row").getAttribute("data-paused")).toBe(
        "false",
      );
    });
    expect(
      backend.calls.find(
        (c) =>
          c.method === "POST" &&
          c.url.pathname === "/api/v1/refresh-schedules/5/resume",
      ),
    ).toBeTruthy();
  });

  it("创建失败态（422 地板）：后端 detail 如实呈现，创建面保留", async () => {
    const backend = install();
    vi.stubGlobal(
      "fetch",
      withScheduleCreateFailure(backend, 422, "刷新间隔不能低于 360 分钟（当前 60）"),
    );
    await openView(31);

    // 等创建面落地再触发（GET 列表返回 [] → 创建面）。
    const create = await screen.findByTestId("schedule-create");
    fireEvent.click(within(create).getByTestId("schedule-create-submit"));

    expect(
      (await screen.findByTestId("schedule-op-error")).textContent,
    ).toContain("360 分钟");
    expect(screen.getByTestId("schedule-create")).toBeTruthy();
    expect(screen.queryByTestId("schedule-row")).toBeNull();
  });
});

// ── 契约刷新响应呈现（wire-dashboard-query-contract 任务 2.3/2.4）──────────
// 35 号看板：面板带 query 引用（渲染仍读 snapshot）；刷新 POST 返回
// mode/panels —— 契约路径按面板统计 + 失败原因列表；ai 路径维持既有呈现。
describe("契约刷新响应呈现（wire-dashboard-query-contract 任务 2.3/2.4）", () => {
  /** 契约看板：kpi 带 query（kpi 单值约定），table 带 query（对照：kept）。 */
  const CONTRACT_SPEC = {
    title: "契约看板",
    spec: {
      title: "契约看板",
      panels: [
        {
          id: "k1",
          type: "kpi",
          title: "秦皇岛煤价",
          span: 1,
          snapshot: { columns: ["value"], rows: [[850]] },
          query: {
            dataset_id: "ds_coal_price",
            sort: [{ column: "date", dir: "desc" }],
            limit: 1,
            columns: ["price"],
          },
        },
        {
          id: "t1",
          type: "table",
          title: "煤价明细",
          span: 2,
          snapshot: { columns: ["日期", "价格"], rows: [["7月", 850], ["8月", 845]] },
          query: { dataset_id: "ds_coal_price", limit: 100 },
        },
      ],
    },
  };

  /** 刷新落地后的 v2 详情：k1 快照更新（850 → 860），t1 失败保留原快照。 */
  const REFRESHED_CONTRACT_SPEC = JSON.parse(JSON.stringify(CONTRACT_SPEC)) as typeof CONTRACT_SPEC;
  (
    REFRESHED_CONTRACT_SPEC.spec.panels[0]!.snapshot as { rows: Array<Array<string | number | null>> }
  ).rows[0]![0] = 860;

  /** 种 35 号契约看板 + 包一层 fetch：POST refresh 返回指定响应体，刷新后
   * GET 详情返回 v2 新快照（其余透传真后端）。 */
  function installContractRefresh(refreshBody: Record<string, unknown>): ReturnType<typeof installConsoleBackend> {
    const backend = installConsoleBackend([]);
    backend.addLifecycleArtifact({
      id: 35,
      kind: "dashboard",
      title: "契约看板",
      version: 1,
      spec: CONTRACT_SPEC,
    });
    let refreshed = false;
    const wrapped = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://console.test");
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.pathname === "/api/v1/artifacts/35/refresh" && method === "POST") {
        refreshed = true;
        return { ok: true, status: 200, json: () => Promise.resolve(refreshBody) };
      }
      if (refreshed && url.pathname === "/api/v1/artifacts/35" && method === "GET") {
        const base = (await backend.fetchImpl(input, init)) as {
          json: () => Promise<Record<string, unknown>>;
        };
        const body = await base.json();
        return {
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({ ...body, version: 2, spec: REFRESHED_CONTRACT_SPEC }),
        };
      }
      return backend.fetchImpl(input as RequestInfo, init);
    }) as unknown as typeof fetch;
    vi.stubGlobal("fetch", wrapped);
    return backend;
  }

  /** 打开 35 号契约看板查看态（面板渲染断言起点）。 */
  async function openContract() {
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");
    fireEvent.click(within(rowOf(35)).getByTestId("artifact-dashboard-view"));
    expect(await screen.findByTestId("dashboard-pane")).toBeTruthy();
  }

  it("带 query 面板渲染读 snapshot（契约不参与渲染）+ 契约响应：统计与失败原因如实呈现", async () => {
    installContractRefresh({
      version: 2,
      refreshed_at: "2026-10-11T09:00:00",
      mode: "contract",
      panels: [
        { panel_id: "k1", status: "refreshed" },
        { panel_id: "ghost", status: "kept" }, // spec 外面板 id：标题回退 id 本身
        { panel_id: "t1", status: "failed", error: "列名不存在：price_usd" },
      ],
    });
    await openContract();

    // 刷新前：带 query 面板按 snapshot 渲染（query 不参与渲染）
    expect(screen.getByTestId("dashboard-kpi-value").textContent).toBe("850");
    expect(screen.getByTestId("dashboard-table").querySelectorAll("tbody tr")).toHaveLength(2);

    fireEvent.click(screen.getByTestId("dashboard-refresh"));

    // 契约统计：v2 + 成功 1 / 保留 1 / 失败 1
    const success = await screen.findByTestId("dashboard-refresh-success");
    expect(success.textContent).toContain("v2");
    expect(success.textContent).toContain("成功 1");
    expect(success.textContent).toContain("保留 1");
    expect(success.textContent).toContain("失败 1");
    // 失败原因列表：面板标题（按 spec 解析）+ 服务端 error
    const failures = await screen.findByTestId("dashboard-refresh-failures");
    const failureItems = within(failures).getAllByTestId("dashboard-refresh-failure");
    expect(failureItems).toHaveLength(1);
    expect(failureItems[0]!.textContent).toContain("煤价明细");
    expect(failureItems[0]!.textContent).toContain("列名不存在：price_usd");
    // spec 外 panel_id 的非失败项不进原因列表；ghost 的标题回退只对 failed 生效

    // 重拉详情重渲染：k1 快照更新（850 → 860），t1 失败保留原快照（2 行）
    await waitFor(() => {
      expect(screen.getByTestId("dashboard-kpi-value").textContent).toBe("860");
    });
    expect(screen.getByTestId("dashboard-table").querySelectorAll("tbody tr")).toHaveLength(2);
  });

  it("spec 外 panel_id 的失败项：标题回退为 panel_id 本身", async () => {
    installContractRefresh({
      version: 2,
      refreshed_at: "2026-10-11T09:00:00",
      mode: "contract",
      panels: [{ panel_id: "ghost", status: "failed", error: "数据集不存在" }],
    });
    await openContract();
    fireEvent.click(screen.getByTestId("dashboard-refresh"));

    const failures = await screen.findByTestId("dashboard-refresh-failures");
    const failureItem = within(failures).getAllByTestId("dashboard-refresh-failure")[0]!;
    expect(failureItem.textContent).toContain("ghost"); // 标题缺失回退 id
    expect(failureItem.textContent).toContain("数据集不存在");
  });

  it("ai 响应不回归：仍按既有版本号提示呈现，无契约统计与失败列表", async () => {
    installContractRefresh({
      version: 2,
      refreshed_at: "2026-10-11T09:00:00",
      mode: "ai",
      panels: null,
    });
    await openContract();
    fireEvent.click(screen.getByTestId("dashboard-refresh"));

    const success = await screen.findByTestId("dashboard-refresh-success");
    expect(success.textContent).toBe("已刷新为版本 v2。");
    expect(success.textContent).not.toContain("契约刷新"); // 无契约统计
    expect(screen.queryByTestId("dashboard-refresh-failures")).toBeNull();
    await waitFor(() => {
      expect(screen.getByTestId("dashboard-kpi-value").textContent).toBe("860");
    });
  });
});

// ── 整板 CSV 导出（wire-dashboard-export 任务 1.2/1.3）──────────────────────
// 查看态工具区「导出 CSV」：快照本地合成整板单文件（BOM + 逐面板 section，
// 零请求——不触任何后端调用）经 createObjectURL 下载并回收；文件名 = 板标题
// （spec 内层优先、legacy 回退产物标题）清洗非法字符；archived 只读也可导
// （纯读不落库）；编辑态无导出入口。
describe("整板 CSV 导出（wire-dashboard-export 任务 1.2/1.3）", () => {
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

  /** 打开指定产物 id 的看板查看态（复用 install() 四产物种子）。 */
  async function openView(id: number) {
    render(<ArtifactsPage />);
    await screen.findAllByTestId("artifact-item");
    fireEvent.click(within(rowOf(id)).getByTestId("artifact-dashboard-view"));
    await screen.findByTestId("dashboard-pane");
  }

  it("v1 看板：点击导出 → 零请求本地合成 BOM 整板 CSV，blob 触发下载并回收；文件名 = spec 标题.csv", async () => {
    const download = stubDownload();
    const backend = install();
    await openView(31);
    // 等查看态附带请求（调度列表 GET）全部落地后再快照计数，避免竞态。
    await waitFor(() => {
      expect(
        backend.calls.some((c) => c.url.pathname === "/api/v1/refresh-schedules"),
      ).toBe(true);
    });
    const callsBefore = backend.calls.length;

    fireEvent.click(screen.getByTestId("dashboard-export-csv"));
    await waitFor(() =>
      expect(download.createObjectURL).toHaveBeenCalledTimes(1),
    );
    // 零请求：导出全在客户端（快照在 spec 里），无任何后端调用增量
    expect(backend.calls.length).toBe(callsBefore);
    // blob 内容：BOM + 逐面板 section（注释行 + 快照表 + 空行分隔）
    const blob = download.createObjectURL.mock.calls[0]![0]!;
    expect(blob).toBeInstanceOf(Blob);
    expect(blob.type).toBe("text/csv;charset=utf-8");
    const text = await blob.text();
    expect(text.charCodeAt(0)).toBe(0xfeff); // UTF-8 BOM
    expect(text).toContain("# CPI 同比（kpi）\r\nvalue\r\n2.1");
    expect(text).toContain("# PMI 走势（line）\r\n月份,PMI\r\n7月,49.4\r\n8月,49.1");
    expect(text).toContain("# 月度明细（table）");
    expect(text).toContain("\r\n\r\n"); // 空行分隔
    // a 标签下载即回收
    const anchor = download.anchor();
    expect(anchor?.getAttribute("href")).toBe("blob:csv");
    expect(anchor?.download).toBe("宏观仪表盘.csv");
    expect(download.revokeObjectURL).toHaveBeenCalledWith("blob:csv");
  });

  it("legacy 看板：板名回退产物标题，文件名非法字符清洗（/ \\ : → _），内容仅标题行（空板如实不报错）", async () => {
    const download = stubDownload();
    const backend = installConsoleBackend([]);
    backend.addLifecycleArtifact({
      id: 36,
      kind: "dashboard",
      title: "Q3/汇报:看板",
      spec: { title: "Q3/汇报:看板", spec: { indicators: ["CN_CPI_YOY"] } },
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    await openView(36);

    fireEvent.click(screen.getByTestId("dashboard-export-csv"));
    await waitFor(() =>
      expect(download.createObjectURL).toHaveBeenCalledTimes(1),
    );
    const blob = download.createObjectURL.mock.calls[0]![0]!;
    expect(await blob.text()).toBe("\uFEFF# Q3/汇报:看板"); // 内容标题原样
    expect(download.anchor()?.download).toBe("Q3_汇报_看板.csv"); // 文件名清洗
  });

  it("archived 只读也可导出（纯读不落库）；编辑态无导出入口", async () => {
    const download = stubDownload();
    install();
    await openView(34);

    // archived：刷新入口隐藏，导出入口仍在且可用
    expect(screen.queryByTestId("dashboard-refresh")).toBeNull();
    fireEvent.click(screen.getByTestId("dashboard-export-csv"));
    await waitFor(() =>
      expect(download.createObjectURL).toHaveBeenCalledTimes(1),
    );
    expect(download.anchor()?.download).toBe("宏观仪表盘.csv");

    // 编辑态：工具区整体收起，无导出入口
    await openView(31);
    fireEvent.click(screen.getByTestId("dashboard-edit"));
    await screen.findByTestId("dashboard-editor");
    expect(screen.queryByTestId("dashboard-export-csv")).toBeNull();
  });
});
