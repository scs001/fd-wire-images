// 「我的数据」组件测试（wire-customer-local-data 任务组 3：3.1-3.5）：
// 列表六态/空态、上传全流程载荷、预览网格改值/删行/加行载荷、漂移 resume 流、
// 悬空徽标与回滚、连接器在线离线与配对令牌一次性显示、白名单建档、路由接入。
// 免费档放行（wire-free-tier-data-gate 4.1-4.3）：完整渲染/配额条/存储徽标/
// 内联限额提示/连接器区付费标注 + 付费档零变化回归。
// fetch 一律走 testing.ts 的内存假后端（零网络）。
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
import { DatasetsPage } from "./DatasetsPage";
import { installConsoleBackend, seedConsoleSession, type FetchCall } from "./testing";
import type { Connector, Dataset, DatasetQuota } from "./api";
import { CONSOLE_TABS } from "./ConsoleLayout";
import { parseHash } from "../App";

const CUSTOMER = "demo-customer";
const NOW = "2026-10-05T10:00:00";

function makeDataset(
  partial: Partial<Dataset> &
    Pick<Dataset, "id" | "name"> & { storage_tier?: string },
): Dataset {
  return {
    key: partial.id.replace(/-/g, "_"),
    source_kind: "upload",
    connector_id: null,
    source_ref: null,
    columns: [
      { name: "id", type: "INTEGER" },
      { name: "amount", type: "REAL" },
    ],
    row_count: 120,
    size_bytes: 4096,
    sync_cron: null,
    sync_status: "ok",
    paused_reason: null,
    created_at: NOW,
    updated_at: NOW,
    customer_id: CUSTOMER,
    ...partial,
  };
}

function makeConnector(
  partial: Partial<Connector> & Pick<Connector, "id" | "name">,
): Connector {
  return {
    version: "v0.1.0",
    status: "online",
    last_heartbeat_at: NOW,
    revoked_at: null,
    created_at: NOW,
    customer_id: CUSTOMER,
    ...partial,
  };
}

function install(options?: Parameters<typeof installConsoleBackend>[1]) {
  const backend = installConsoleBackend([], options);
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

function postCalls(
  backend: ReturnType<typeof installConsoleBackend>,
  path: string,
): FetchCall[] {
  return backend.calls.filter(
    (call) => call.method === "POST" && call.url.pathname === path,
  );
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  // 登录会话种子：假后端以 Bearer 的 sub 为客户身份（demo-customer）。
  seedConsoleSession({ sub: "demo-customer" });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

describe("DatasetsPage（我的数据列表与上传，任务 3.1/3.2）", () => {
  it("指标目录徽标：source_kind=indicator 琥珀徽标 + 来源筛选项在场（wire-indicator-to-dataset 2.2）", async () => {
    install({
      datasets: [
        makeDataset({
          id: "ds-ind-1",
          name: "河南生产总值",
          source_kind: "indicator",
          source_ref: "yearbook_catalog:1699",
          sync_status: "ok",
        }),
      ],
    });
    render(<DatasetsPage planCode="basic" />);

    const badge = await screen.findByTestId("dataset-source-badge");
    expect(badge.getAttribute("data-kind")).toBe("indicator");
    expect(badge.textContent).toBe("指标目录");
    // 来源类型筛选项含 indicator（SOURCE_KIND_OPTIONS）
    const filter = screen.getByTestId("assets-filter-source-kind");
    const optionValues = Array.from(filter.querySelectorAll("option")).map(
      (o) => o.getAttribute("value"),
    );
    expect(optionValues).toContain("indicator");
  });

  it("卡片渲染：来源徽标、六态同步状态、行数/大小/最近更新、暂停原因如实展示", async () => {
    install({
      datasets: [
        makeDataset({ id: "ds-1", name: "门店销量", sync_status: "ok" }),
        makeDataset({
          id: "ds-2",
          name: "ERP 订单",
          source_kind: "connector",
          source_ref: "erp_db",
          sync_status: "paused_drift",
          paused_reason: "drift: 列 price 缺失",
          row_count: 3000,
          size_bytes: 2 * 1024 * 1024,
        }),
        makeDataset({ id: "ds-3", name: "同步中", sync_status: "syncing" }),
        makeDataset({ id: "ds-4", name: "同步失败", sync_status: "failed" }),
        makeDataset({ id: "ds-5", name: "待同步", sync_status: "idle" }),
        makeDataset({ id: "ds-6", name: "配额暂停", sync_status: "paused_quota" }),
      ],
    });
    render(<DatasetsPage planCode="basic" />);

    const cards = await screen.findAllByTestId("dataset-card");
    expect(cards.length).toBe(6);
    expect(
      screen
        .getAllByTestId("dataset-sync-status")
        .map((el) => el.getAttribute("data-status"))
        .sort(),
    ).toEqual([
      "failed",
      "idle",
      "ok",
      "paused_drift",
      "paused_quota",
      "syncing",
    ]);
    expect(
      screen.getAllByTestId("dataset-source-badge").map((el) => el.getAttribute("data-kind")),
    ).toEqual(["upload", "connector", "upload", "upload", "upload", "upload"]);
    expect(screen.getByTestId("dataset-source-ref").textContent).toBe("erp_db");
    // 暂停态原因如实展示（连字符文案含 API 值）
    expect(screen.getByTestId("dataset-paused-reason").textContent).toContain(
      "drift: 列 price 缺失",
    );
    // 行数/大小/最近更新（数字来自 API，不写死）
    const rows = screen.getAllByTestId("dataset-rows").map((el) => el.textContent);
    expect(rows).toContain("120 行");
    expect(rows).toContain("3000 行");
    expect(
      screen.getAllByTestId("dataset-size").map((el) => el.textContent),
    ).toContain("大小 2.0 MB");
    expect(screen.getAllByTestId("dataset-updated")[0]!.textContent).toContain(
      "最近更新",
    );
    // 同步状态文案走 i18n（六态中文）
    expect(
      screen.getAllByTestId("dataset-sync-status").map((el) => el.textContent),
    ).toEqual(
      expect.arrayContaining([
        "同步正常",
        "已暂停 · 结构漂移",
        "已暂停 · 配额超限",
        "同步中",
        "同步失败",
        "待同步",
      ]),
    );
  });

  it("无数据集显示空态并引导上传", async () => {
    install({ datasets: [] });
    render(<DatasetsPage planCode="basic" />);

    const empty = await screen.findByTestId("datasets-empty");
    expect(empty.textContent).toContain("上传");
    expect(screen.getByTestId("upload-section")).toBeTruthy();
    expect(screen.getByTestId("connectors-section")).toBeTruthy();
  });

  it("上传全流程：multipart 载荷、列与类型预览、命名建档载荷与列表刷新", async () => {
    const backend = install({ datasets: [] });
    render(<DatasetsPage planCode="basic" />);
    await screen.findByTestId("datasets-empty");

    const file = new File(["id,amount\n1,2"], "sales.csv", {
      type: "text/csv",
    });
    fireEvent.change(screen.getByTestId("upload-file-input"), {
      target: { files: [file] },
    });

    await screen.findByTestId("upload-preview");
    const uploadCall = backend.calls.find(
      (call) => call.url.pathname === "/api/v1/datasets/uploads",
    );
    expect(uploadCall?.method).toBe("POST");
    expect(uploadCall?.headers["authorization"]).toMatch(/^Bearer /);
    expect((uploadCall?.formData?.get("file") as File).name).toBe("sales.csv");
    expect(screen.getByTestId("upload-preview-rows").textContent).toContain("120");
    expect(
      screen
        .getAllByTestId("upload-preview-column")
        .map((el) => [el.getAttribute("data-column"), el.getAttribute("data-type")]),
    ).toEqual([
      ["id", "INTEGER"],
      ["amount", "REAL"],
    ]);

    // 默认名称取自文件名，改为中文名后建档
    expect((screen.getByTestId("upload-name") as HTMLInputElement).value).toBe(
      "sales",
    );
    fireEvent.change(screen.getByTestId("upload-name"), {
      target: { value: "门店销量" },
    });
    fireEvent.click(screen.getByTestId("upload-create"));

    await waitFor(() =>
      expect(postCalls(backend, "/api/v1/datasets").length).toBe(1),
    );
    expect(postCalls(backend, "/api/v1/datasets")[0]!.body).toEqual({
      name: "门店销量",
      source: { kind: "upload", upload_id: "up-1" },
    });
    // 建档成功：预览收起、列表刷新出新卡片
    expect(screen.queryByTestId("upload-preview")).toBeNull();
    expect(await screen.findByTestId("upload-created")).toBeTruthy();
    await waitFor(() =>
      expect(screen.getAllByTestId("dataset-card").length).toBe(1),
    );
    expect(screen.getByTestId("dataset-name").textContent).toBe("门店销量");
  });

  it("上传超限：within_limit=false 提示拒收并禁用建档", async () => {
    install({ datasets: [], uploadPreview: { within_limit: false } });
    render(<DatasetsPage planCode="basic" />);
    await screen.findByTestId("datasets-empty");

    fireEvent.change(screen.getByTestId("upload-file-input"), {
      target: { files: [new File(["x"], "big.csv")] },
    });
    await screen.findByTestId("upload-preview");
    expect(screen.getByTestId("upload-over-limit").textContent).toContain(
      "行数超出上限",
    );
    expect(
      (screen.getByTestId("upload-create") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("上传解析失败：4xx 的 detail 如实呈现", async () => {
    install({
      datasets: [],
      failUploadStatus: 400,
      failUploadDetail: "行数超出上限（上限 500000），已拒收",
    });
    render(<DatasetsPage planCode="basic" />);
    await screen.findByTestId("datasets-empty");

    fireEvent.change(screen.getByTestId("upload-file-input"), {
      target: { files: [new File(["x"], "broken.csv")] },
    });
    const error = await screen.findByTestId("upload-error");
    expect(error.textContent).toContain("行数超出上限（上限 500000），已拒收");
    expect(screen.queryByTestId("upload-preview")).toBeNull();
  });
});

describe("DatasetDetail（预览网格/修正/同步/悬空，任务 3.4）", () => {
  it("预览网格编辑模式：默认只读，双击改值/删行/加行入脏队列，保存按序提交 update/delete/insert", async () => {
    const backend = install({
      datasets: [makeDataset({ id: "ds-1", name: "门店销量" })],
      datasetRows: {
        "ds-1": {
          columns: [
            { name: "id", type: "INTEGER" },
            { name: "amount", type: "REAL" },
          ],
          rows: [
            { id: 1, amount: 10 },
            { id: 2, amount: 20 },
          ],
        },
      },
    });
    render(<DatasetsPage planCode="basic" />);
    fireEvent.click(await screen.findByTestId("dataset-open"));
    await screen.findByTestId("preview-grid");

    // 只读默认（wire-dataset-form-editing 2.1）：无删行/加行控件，双击不开编辑框
    expect(screen.queryByTestId("preview-delete-row")).toBeNull();
    expect(screen.queryByTestId("preview-add-row")).toBeNull();
    const readOnlyCell = screen
      .getAllByTestId("preview-cell")
      .find(
        (el) =>
          el.getAttribute("data-column") === "amount" &&
          el.getAttribute("data-row-id") === "hash:seed-1",
      );
    expect(readOnlyCell).toBeTruthy();
    fireEvent.doubleClick(readOnlyCell!);
    expect(screen.queryByTestId("preview-edit-input")).toBeNull();

    // 进入编辑模式
    fireEvent.click(screen.getByTestId("preview-edit-toggle"));

    // 双击改值 → Enter 入队（行锚来自引擎注入的 __row_id，假后端补 hash:seed-<n>）
    const cell = screen
      .getAllByTestId("preview-cell")
      .find(
        (el) =>
          el.getAttribute("data-column") === "amount" &&
          el.getAttribute("data-row-id") === "hash:seed-1",
      );
    fireEvent.doubleClick(cell!);
    fireEvent.change(screen.getByTestId("preview-edit-input"), {
      target: { value: "99" },
    });
    fireEvent.keyDown(screen.getByTestId("preview-edit-input"), { key: "Enter" });
    expect(screen.getByTestId("preview-dirty-badge").textContent).toContain("1");
    expect(postCalls(backend, "/api/v1/datasets/ds-1/corrections").length).toBe(0);

    // 删行入队（第二行 → hash:seed-2）
    expect(screen.getAllByTestId("preview-delete-row").length).toBe(2);
    fireEvent.click(screen.getAllByTestId("preview-delete-row")[1]!);
    expect(screen.getByTestId("preview-dirty-badge").textContent).toContain("2");

    // 加行入队（只填 amount 列）
    fireEvent.click(screen.getByTestId("preview-add-row"));
    const amountInput = screen
      .getAllByTestId("preview-add-row-input")
      .find((el) => el.getAttribute("data-column") === "amount");
    fireEvent.change(amountInput!, { target: { value: "5" } });
    fireEvent.click(screen.getByTestId("preview-add-row-submit"));
    expect(screen.getByTestId("preview-dirty-badge").textContent).toContain("3");
    expect(postCalls(backend, "/api/v1/datasets/ds-1/corrections").length).toBe(0);

    // 保存：按入队顺序逐条提交三条修正
    fireEvent.click(screen.getByTestId("preview-queue-save"));
    await waitFor(() =>
      expect(postCalls(backend, "/api/v1/datasets/ds-1/corrections").length).toBe(3),
    );
    const posts = postCalls(backend, "/api/v1/datasets/ds-1/corrections");
    expect(posts.map((call) => call.body)).toEqual([
      { op: "update", row_id: "hash:seed-1", column: "amount", value: "99" },
      { op: "delete", row_id: "hash:seed-2" },
      { op: "insert", values: { amount: "5" } },
    ]);
    // 全部成功：队列清空、逐条结果已生效、预览刷新呈现修正后数据
    await waitFor(() =>
      expect(screen.queryByTestId("preview-dirty-badge")).toBeNull(),
    );
    await waitFor(() =>
      expect(
        screen.getAllByTestId("preview-save-result").length,
      ).toBe(3),
    );
    expect(
      screen
        .getAllByTestId("preview-save-result")
        .every((el) => el.getAttribute("data-ok") === "true"),
    ).toBe(true);
    await waitFor(() => {
      const updated = screen
        .getAllByTestId("preview-cell")
        .find(
          (el) =>
            el.getAttribute("data-column") === "amount" &&
            el.getAttribute("data-row-id") === "hash:seed-1",
        );
      expect(updated?.textContent).toBe("99");
    });
  });

  it("漂移暂停：resume 确认弹窗展示被拒列，确认后提交 accept_columns 并恢复", async () => {
    const drift = "drift: 列 price 缺失，列 amount 类型变更";
    const backend = install({
      datasets: [
        makeDataset({
          id: "ds-1",
          name: "ERP 订单",
          source_kind: "connector",
          source_ref: "erp_db",
          sync_status: "paused_drift",
          paused_reason: drift,
        }),
      ],
      datasetRows: {
        "ds-1": { columns: [{ name: "id", type: "INTEGER" }], rows: [{ id: 1 }] },
      },
    });
    backend.addSync({
      id: "sy-1",
      dataset_id: "ds-1",
      started_at: NOW,
      finished_at: NOW,
      status: "drift",
      rows: 0,
      error: drift,
    });

    render(<DatasetsPage planCode="basic" />);
    fireEvent.click(await screen.findByTestId("dataset-open"));
    expect(await screen.findByTestId("drift-banner")).toBeTruthy();
    expect((await screen.findByTestId("sync-row")).getAttribute("data-status")).toBe(
      "drift",
    );

    fireEvent.click(screen.getByTestId("drift-resume"));
    expect(screen.getByTestId("resume-dialog")).toBeTruthy();
    expect(screen.getByTestId("resume-reason").textContent).toContain(drift);
    expect(
      screen.getAllByTestId("resume-column").map((el) => el.getAttribute("data-column")),
    ).toEqual(["price", "amount"]);

    fireEvent.click(screen.getByTestId("resume-confirm"));
    await waitFor(() =>
      expect(postCalls(backend, "/api/v1/datasets/ds-1/resume").length).toBe(1),
    );
    expect(postCalls(backend, "/api/v1/datasets/ds-1/resume")[0]!.body).toEqual({
      accept_columns: ["price", "amount"],
    });
    expect(await screen.findByTestId("resume-done")).toBeTruthy();
    await waitFor(() =>
      expect(
        screen.getByTestId("detail-sync-status").getAttribute("data-status"),
      ).toBe("idle"),
    );
    expect(screen.queryByTestId("drift-banner")).toBeNull();
  });

  it("悬空：修正历史带悬空徽标，悬空清单逐条回滚后移出清单", async () => {
    const backend = install({
      datasets: [makeDataset({ id: "ds-1", name: "门店销量" })],
      datasetRows: {
        "ds-1": { columns: [{ name: "id", type: "INTEGER" }], rows: [{ id: 1 }] },
      },
    });
    backend.addCorrection({
      id: 1,
      dataset_id: "ds-1",
      op: "update",
      row_id: "hash:seed-1",
      column: "id",
      value: "9",
      values: null,
      actor: "demo",
      created_at: NOW,
      reverted_at: null,
      dangling_at: null,
      dangling_reason: null,
    });
    backend.addCorrection({
      id: 2,
      dataset_id: "ds-1",
      op: "delete",
      row_id: "hash:gone",
      column: null,
      value: null,
      values: null,
      actor: "demo",
      created_at: NOW,
      reverted_at: null,
      dangling_at: NOW,
      dangling_reason: "快照中无匹配行",
    });

    render(<DatasetsPage planCode="basic" />);
    fireEvent.click(await screen.findByTestId("dataset-open"));
    expect(await screen.findAllByTestId("correction-row")).toHaveLength(2);
    expect(screen.getAllByTestId("correction-dangling-badge")).toHaveLength(1);
    expect(screen.getByTestId("dangling-item")).toBeTruthy();

    fireEvent.click(screen.getByTestId("dangling-revert"));
    await waitFor(() =>
      expect(
        postCalls(backend, "/api/v1/datasets/ds-1/corrections/2/revert").length,
      ).toBe(1),
    );
    await waitFor(() => expect(screen.queryByTestId("dangling-item")).toBeNull());
    expect(screen.getByTestId("correction-reverted-badge")).toBeTruthy();
  });

  it("删除数据集：取消不动作；确认后发出 DELETE 并回列表刷新", async () => {
    const backend = install({
      datasets: [makeDataset({ id: "ds-1", name: "门店销量" })],
      datasetRows: {
        "ds-1": {
          columns: [{ name: "id", type: "INTEGER" }],
          rows: [{ id: 1 }],
        },
      },
    });
    render(<DatasetsPage planCode="basic" />);
    fireEvent.click(await screen.findByTestId("dataset-open"));
    await screen.findByTestId("preview-grid");

    fireEvent.click(screen.getByTestId("detail-delete"));
    const dialog = await screen.findByTestId("delete-dialog");
    // 文案如实：只删平台副本与修正记录，不碰客户源
    expect(dialog.textContent).toContain("客户源数据不受任何影响");

    // 取消：关弹窗、未发任何 DELETE、详情保持
    fireEvent.click(screen.getByTestId("delete-cancel"));
    expect(screen.queryByTestId("delete-dialog")).toBeNull();
    expect(backend.calls.filter((call) => call.method === "DELETE")).toHaveLength(0);
    expect(screen.getByTestId("dataset-detail")).toBeTruthy();

    // 确认：DELETE /api/v1/datasets/ds-1，成功后回列表（fake 已删 → 空态）
    fireEvent.click(screen.getByTestId("detail-delete"));
    fireEvent.click(screen.getByTestId("delete-confirm"));
    await waitFor(() =>
      expect(
        backend.calls.filter(
          (call) =>
            call.method === "DELETE" &&
            call.url.pathname === "/api/v1/datasets/ds-1",
        ),
      ).toHaveLength(1),
    );
    await waitFor(() =>
      expect(screen.queryByTestId("dataset-detail")).toBeNull(),
    );
    expect(await screen.findByTestId("datasets-empty")).toBeTruthy();
  });

  it("连接器来源的删除确认额外说明同步停止", async () => {
    install({
      datasets: [
        makeDataset({
          id: "ds-1",
          name: "ERP 订单",
          source_kind: "connector",
          source_ref: "erp_db",
        }),
      ],
    });
    render(<DatasetsPage planCode="basic" />);
    fireEvent.click(await screen.findByTestId("dataset-open"));
    await screen.findByTestId("dataset-detail");

    fireEvent.click(screen.getByTestId("detail-delete"));
    const dialog = await screen.findByTestId("delete-dialog");
    expect(
      screen.getByTestId("delete-connector-warning").textContent,
    ).toContain("同步停止");
    expect(dialog.textContent).toContain("客户源数据不受任何影响");
  });
});

describe("Connectors（任务 3.3）", () => {
  it("连接器列表：在线/离线/已吊销徽标、版本与最后心跳", async () => {
    install({
      datasets: [],
      connectors: [
        makeConnector({ id: "cn-1", name: "erp-connector" }),
        makeConnector({
          id: "cn-2",
          name: "offline-connector",
          status: "offline",
          last_heartbeat_at: null,
        }),
        makeConnector({ id: "cn-3", name: "revoked-connector", revoked_at: NOW }),
      ],
    });
    render(<DatasetsPage planCode="basic" />);

    const items = await screen.findAllByTestId("connector-item");
    expect(items.map((el) => el.getAttribute("data-status"))).toEqual([
      "online",
      "offline",
      "revoked",
    ]);
    expect(
      screen.getAllByTestId("connector-status").map((el) => el.textContent),
    ).toEqual(["在线", "离线", "已吊销"]);
    expect(
      screen.getAllByTestId("connector-heartbeat")[1]!.textContent,
    ).toContain("—");
    // 已吊销连接器不再提供吊销按钮
    expect(screen.getAllByTestId("connector-revoke")).toHaveLength(2);
  });

  it("配对向导：令牌仅显示一次，关闭后不再展示，重新生成得到新令牌", async () => {
    const backend = install({ datasets: [] });
    render(<DatasetsPage planCode="basic" />);
    await screen.findByTestId("connectors-empty");

    fireEvent.click(screen.getByTestId("pairing-generate"));
    const tokenEl = await screen.findByTestId("pairing-token");
    const firstToken = tokenEl.textContent!;
    expect(firstToken.startsWith("fwp_")).toBe(true);
    expect(screen.getByTestId("pairing-once-hint").textContent).toContain(
      "仅显示一次",
    );
    expect(screen.getByTestId("pairing-countdown").textContent).toContain(
      "剩余有效时间",
    );
    // 安装指引命令携带令牌与 api 地址
    expect(screen.getByTestId("pairing-command").textContent).toContain(
      `fd-wire-connector pair --token ${firstToken}`,
    );
    expect(
      screen.getByTestId("pairing-command").textContent,
    ).toContain("--api");
    expect(postCalls(backend, "/api/v1/connectors/pairing-tokens").length).toBe(1);

    // 一次性语义：关闭即不再展示（组件不持久化令牌）
    fireEvent.click(screen.getByTestId("pairing-dismiss"));
    expect(screen.queryByTestId("pairing-token")).toBeNull();

    fireEvent.click(screen.getByTestId("pairing-generate"));
    const secondToken = (await screen.findByTestId("pairing-token")).textContent;
    expect(secondToken).not.toBe(firstToken);
    expect(postCalls(backend, "/api/v1/connectors/pairing-tokens").length).toBe(2);
  });

  it("白名单配置：源名+源内表名双字段建档 → createDataset(connector) 载荷含 source_name", async () => {
    const backend = install({
      datasets: [],
      connectors: [makeConnector({ id: "cn-1", name: "erp-connector" })],
    });
    render(<DatasetsPage planCode="basic" />);
    await screen.findByTestId("connector-item");

    fireEvent.change(screen.getByTestId("whitelist-connector"), {
      target: { value: "cn-1" },
    });
    fireEvent.change(screen.getByTestId("whitelist-source-name"), {
      target: { value: "erp_db" },
    });
    fireEvent.change(screen.getByTestId("whitelist-source-ref"), {
      target: { value: "public.orders" },
    });
    fireEvent.change(screen.getByTestId("whitelist-name"), {
      target: { value: "ERP 订单" },
    });
    fireEvent.change(screen.getByTestId("whitelist-cron"), {
      target: { value: "0 3 * * *" },
    });
    fireEvent.submit(screen.getByTestId("whitelist-submit").closest("form")!);

    await waitFor(() =>
      expect(postCalls(backend, "/api/v1/datasets").length).toBe(1),
    );
    expect(postCalls(backend, "/api/v1/datasets")[0]!.body).toEqual({
      name: "ERP 订单",
      source: {
        kind: "connector",
        connector_id: "cn-1",
        source_name: "erp_db",
        source_ref: "public.orders",
        schedule_cron: "0 3 * * *",
      },
    });
    expect(await screen.findByTestId("whitelist-created")).toBeTruthy();
    await waitFor(() =>
      expect(screen.getAllByTestId("dataset-card").length).toBe(1),
    );
    // 卡片来源展示「源名 · 源内表名」
    expect(screen.getByTestId("dataset-source-ref").textContent).toBe(
      "erp_db · public.orders",
    );
  });

  it("白名单校验：源名或源内表名缺一即禁用提交（必填提示可见）", async () => {
    install({
      datasets: [],
      connectors: [makeConnector({ id: "cn-1", name: "erp-connector" })],
    });
    render(<DatasetsPage planCode="basic" />);
    await screen.findByTestId("connector-item");

    const submit = () =>
      screen.getByTestId("whitelist-submit") as HTMLButtonElement;
    // 只选连接器：缺源名、缺源内表名
    fireEvent.change(screen.getByTestId("whitelist-connector"), {
      target: { value: "cn-1" },
    });
    expect(submit().disabled).toBe(true);
    expect(screen.getByTestId("whitelist-section").textContent).toContain(
      "均为必填",
    );

    // 补源名，仍缺源内表名 → 仍禁用
    fireEvent.change(screen.getByTestId("whitelist-source-name"), {
      target: { value: "erp_db" },
    });
    expect(submit().disabled).toBe(true);

    // 补齐源内表名（+名称）→ 解禁
    fireEvent.change(screen.getByTestId("whitelist-source-ref"), {
      target: { value: "public.orders" },
    });
    fireEvent.change(screen.getByTestId("whitelist-name"), {
      target: { value: "ERP 订单" },
    });
    expect(submit().disabled).toBe(false);
  });

  it("REST 源登记：表单零凭据输入框，提交载荷只含非敏感请求形状", async () => {
    const backend = install({
      datasets: [],
      connectors: [makeConnector({ id: "cn-1", name: "erp-connector" })],
    });
    render(<DatasetsPage planCode="basic" />);
    await screen.findByTestId("connector-item");

    fireEvent.change(screen.getByTestId("whitelist-connector"), {
      target: { value: "cn-1" },
    });
    fireEvent.change(screen.getByTestId("whitelist-source-name"), {
      target: { value: "orders-api" },
    });
    fireEvent.change(screen.getByTestId("whitelist-source-ref"), {
      target: { value: "orders" },
    });
    fireEvent.change(screen.getByTestId("whitelist-name"), {
      target: { value: "订单 API" },
    });
    fireEvent.change(screen.getByTestId("whitelist-source-type"), {
      target: { value: "rest" },
    });
    // 零凭据：REST 区不含任何 password/token/secret 输入框
    const rest = screen.getByTestId("rest-source-fields");
    for (const input of Array.from(rest.querySelectorAll("input"))) {
      const haystack = [
        input.getAttribute("type") ?? "",
        input.getAttribute("name") ?? "",
        input.getAttribute("id") ?? "",
        input.getAttribute("data-testid") ?? "",
      ]
        .join(" ")
        .toLowerCase();
      expect(haystack).not.toContain("password");
      expect(haystack).not.toContain("token");
      expect(haystack).not.toContain("secret");
    }
    // 本地配置提示可见
    expect(screen.getByTestId("rest-credential-hint").textContent).toContain(
      "本地配置文件",
    );

    fireEvent.change(screen.getByTestId("rest-url"), {
      target: { value: "https://api.example.com/v1/orders" },
    });
    fireEvent.change(screen.getByTestId("rest-root-path"), {
      target: { value: "data.items" },
    });
    fireEvent.change(screen.getByTestId("rest-projection-path-0"), {
      target: { value: "id" },
    });
    fireEvent.change(screen.getByTestId("rest-projection-column-0"), {
      target: { value: "order_id" },
    });
    fireEvent.change(screen.getByTestId("rest-pagination-mode"), {
      target: { value: "page" },
    });
    fireEvent.change(screen.getByTestId("rest-page-param"), {
      target: { value: "page" },
    });
    fireEvent.change(screen.getByTestId("rest-size-param"), {
      target: { value: "size" },
    });
    fireEvent.change(screen.getByTestId("rest-page-size"), {
      target: { value: "500" },
    });
    fireEvent.submit(screen.getByTestId("whitelist-submit").closest("form")!);

    await waitFor(() =>
      expect(postCalls(backend, "/api/v1/datasets").length).toBe(1),
    );
    const body = postCalls(backend, "/api/v1/datasets")[0]!.body as {
      source: Record<string, unknown>;
    };
    expect(body.source).toEqual({
      kind: "connector",
      connector_id: "cn-1",
      source_name: "orders-api",
      source_ref: "orders",
      schedule_cron: null,
      source_config: {
        url: "https://api.example.com/v1/orders",
        method: "GET",
        root_path: "data.items",
        projection: [{ path: "id", column: "order_id", type: "TEXT" }],
        pagination: {
          mode: "page",
          page_param: "page",
          size_param: "size",
          page_size: 500,
        },
      },
    });
    // 载荷里没有任何凭据字段
    expect(JSON.stringify(body.source)).not.toMatch(
      /authorization|password|secret|token/i,
    );
  });

  it("REST 源校验：URL 缺 scheme / 投影未填 → 禁用提交并给出原因", async () => {
    install({
      datasets: [],
      connectors: [makeConnector({ id: "cn-1", name: "erp-connector" })],
    });
    render(<DatasetsPage planCode="basic" />);
    await screen.findByTestId("connector-item");

    fireEvent.change(screen.getByTestId("whitelist-connector"), {
      target: { value: "cn-1" },
    });
    fireEvent.change(screen.getByTestId("whitelist-source-name"), {
      target: { value: "orders-api" },
    });
    fireEvent.change(screen.getByTestId("whitelist-source-ref"), {
      target: { value: "orders" },
    });
    fireEvent.change(screen.getByTestId("whitelist-name"), {
      target: { value: "订单 API" },
    });
    fireEvent.change(screen.getByTestId("whitelist-source-type"), {
      target: { value: "rest" },
    });
    const submit = () =>
      screen.getByTestId("whitelist-submit") as HTMLButtonElement;

    // URL 空
    expect(submit().disabled).toBe(true);
    expect(screen.getByTestId("rest-source-error").textContent).toContain(
      "必填",
    );

    // 缺 scheme
    fireEvent.change(screen.getByTestId("rest-url"), {
      target: { value: "api.example.com/v1/orders" },
    });
    expect(submit().disabled).toBe(true);
    expect(screen.getByTestId("rest-source-error").textContent).toContain(
      "http://",
    );

    // 投影未填
    fireEvent.change(screen.getByTestId("rest-url"), {
      target: { value: "https://api.example.com/v1/orders" },
    });
    expect(submit().disabled).toBe(true);
    expect(screen.getByTestId("rest-source-error").textContent).toContain(
      "投影",
    );

    // 补齐后解禁
    fireEvent.change(screen.getByTestId("rest-projection-path-0"), {
      target: { value: "id" },
    });
    fireEvent.change(screen.getByTestId("rest-projection-column-0"), {
      target: { value: "order_id" },
    });
    expect(submit().disabled).toBe(false);
    expect(screen.queryByTestId("rest-source-error")).toBeNull();
  });

  it("库表源登记不带 source_config（rest 分支才提交该键）", async () => {
    const backend = install({
      datasets: [],
      connectors: [makeConnector({ id: "cn-1", name: "erp-connector" })],
    });
    render(<DatasetsPage planCode="basic" />);
    await screen.findByTestId("connector-item");
    fireEvent.change(screen.getByTestId("whitelist-connector"), {
      target: { value: "cn-1" },
    });
    fireEvent.change(screen.getByTestId("whitelist-source-name"), {
      target: { value: "erp_db" },
    });
    fireEvent.change(screen.getByTestId("whitelist-source-ref"), {
      target: { value: "public.orders" },
    });
    fireEvent.change(screen.getByTestId("whitelist-name"), {
      target: { value: "ERP 订单" },
    });
    // 默认源类型 = 库表/文件 → 无 REST 区
    expect(screen.queryByTestId("rest-source-fields")).toBeNull();
    fireEvent.submit(screen.getByTestId("whitelist-submit").closest("form")!);
    await waitFor(() =>
      expect(postCalls(backend, "/api/v1/datasets").length).toBe(1),
    );
    const body = postCalls(backend, "/api/v1/datasets")[0]!.body as {
      source: Record<string, unknown>;
    };
    expect("source_config" in body.source).toBe(false);
  });
});

describe("导航与路由接入（任务 3.1）", () => {
  it("ConsoleTab 加 datasets：CONSOLE_TABS 项与 /console/datasets 直达路由", () => {
    expect(CONSOLE_TABS).toContainEqual({
      id: "datasets",
      href: "#/console/datasets",
      labelKey: "console.nav.datasets",
    });
    expect(parseHash("#/console/datasets")).toEqual({
      name: "console",
      tab: "datasets",
    });
    // 模块文案全键化（中英键集合一致性由 i18nKeys.test.ts 全量守护）
    expect(i18n.t("console.nav.datasets")).toBe("我的数据");
    expect(i18n.t("console.datasets.title")).toBe("我的数据");
    expect(
      i18n.t("console.datasets.detail.pagination", {
        total: 3,
        page: 1,
        pages: 1,
      }),
    ).toContain("3");
  });
});

describe("DatasetsPage（免费档放行，wire-free-tier-data-gate 4.1-4.3）", () => {
  it("免费档完整渲染：标题/配额条/列表（含存储层徽标）/上传面；连接器区可见但标注付费且不发起连接器请求", async () => {
    const backend = install({
      planCode: "free",
      datasets: [
        makeDataset({ id: "ds-1", name: "轻量数据集", storage_tier: "api" }),
      ],
    });
    render(<DatasetsPage planCode="free" />);

    // 配额条内联（数值来自配额端点，不写死）：已用 1/3、每集 ≤10,000 行
    const quota = await screen.findByTestId("datasets-quota");
    expect(quota.getAttribute("data-exhausted")).toBe("false");
    expect(quota.textContent).toContain("1/3");
    expect(quota.textContent).toContain("10,000");
    // 列表卡片 + 存储层徽标（api 层数据集带 storage_tier；与 cell 层同列表）
    expect(await screen.findByTestId("dataset-card")).toBeTruthy();
    expect(
      screen.getByTestId("dataset-tier-badge").getAttribute("data-tier"),
    ).toBe("api");
    expect(screen.getByTestId("dataset-tier-badge").textContent).toBe(
      "轻量存储",
    );
    // 上传面照常渲染（免费通道 = CSV 上传）
    expect(screen.getByTestId("upload-section")).toBeTruthy();
    // 连接器区可见但标注付费：不渲染配对向导/白名单（不发起建集）
    expect(screen.getByTestId("connectors-section")).toBeTruthy();
    expect(screen.getByTestId("connectors-paid-badge").textContent).toBe(
      "付费档",
    );
    expect(screen.queryByTestId("pairing-wizard")).toBeNull();
    expect(screen.queryByTestId("whitelist-section")).toBeNull();
    // 免费档不发起任何连接器请求（连接器建集经引擎物化，属付费面）
    expect(
      backend.calls.filter((c) =>
        c.url.pathname.startsWith("/api/v1/connectors"),
      ),
    ).toEqual([]);
    // 点击付费徽标展开升档说明（mailto CTA）
    fireEvent.click(screen.getByTestId("connectors-paid-badge"));
    const note = await screen.findByTestId("connectors-paid-note");
    expect(note.textContent).toContain("付费档");
    expect(
      screen.getByTestId("connectors-paid-cta").getAttribute("href"),
    ).toMatch(/^mailto:/);
  });

  it("免费档上传建档：同接口列表刷新出新卡片（api 层带存储徽标），配额条同步 0/3 → 1/3", async () => {
    const backend = install({ planCode: "free", datasets: [] });
    render(<DatasetsPage planCode="free" />);
    await waitFor(() =>
      expect(screen.getByTestId("datasets-quota").textContent).toContain("0/3"),
    );

    fireEvent.change(
      screen.getByTestId("upload-file-input"),
      {
        target: {
          files: [
            new File(["id,amount\n1,2"], "sales.csv", { type: "text/csv" }),
          ],
        },
      },
    );
    await screen.findByTestId("upload-preview");
    fireEvent.click(screen.getByTestId("upload-create"));

    expect(await screen.findByTestId("upload-created")).toBeTruthy();
    await waitFor(() =>
      expect(screen.getAllByTestId("dataset-card").length).toBe(1),
    );
    // api 层数据集与 cell 层同列表：响应带 storage_tier 即显徽标
    expect(
      screen.getByTestId("dataset-tier-badge").getAttribute("data-tier"),
    ).toBe("api");
    // 配额条随建档刷新
    await waitFor(() =>
      expect(screen.getByTestId("datasets-quota").textContent).toContain("1/3"),
    );
    expect(postCalls(backend, "/api/v1/datasets").length).toBe(1);
  });

  it("免费档建集触限：400 配额语义错误 → 错误如实 + 内联升档提示（amber），页面不退墙", async () => {
    install({
      planCode: "free",
      datasets: [],
      failCreateDatasetStatus: 400,
      failCreateDatasetDetail:
        "免费档数据集数量已达上限（3 个），升档可解锁更多",
    });
    render(<DatasetsPage planCode="free" />);
    await screen.findByTestId("datasets-quota");

    fireEvent.change(screen.getByTestId("upload-file-input"), {
      target: { files: [new File(["x"], "fourth.csv")] },
    });
    await screen.findByTestId("upload-preview");
    fireEvent.click(screen.getByTestId("upload-create"));

    // api 错误 detail 如实呈现
    const error = await screen.findByTestId("upload-error");
    expect(error.textContent).toContain("已达上限");
    // 内联升档提示（非整页墙）
    const hint = screen.getByTestId("upload-quota-hint");
    expect(hint.textContent).toContain("已达免费档限额");
    expect(
      screen.getByTestId("upload-quota-cta").getAttribute("href"),
    ).toMatch(/^mailto:/);
    expect(screen.getByTestId("upload-section")).toBeTruthy();
    expect(screen.getByTestId("connectors-section")).toBeTruthy();
  });

  it("免费档上传行数触限：错误如实 + 内联升档提示", async () => {
    install({
      planCode: "free",
      datasets: [],
      failUploadStatus: 400,
      failUploadDetail: "行数超出免费档单集上限（10,000 行），已拒收",
    });
    render(<DatasetsPage planCode="free" />);
    await screen.findByTestId("datasets-empty");

    fireEvent.change(screen.getByTestId("upload-file-input"), {
      target: { files: [new File(["x"], "big.csv")] },
    });
    expect(await screen.findByTestId("upload-error")).toBeTruthy();
    expect(screen.getByTestId("upload-quota-hint")).toBeTruthy();
  });

  it("配额端点未就绪：配额条静默隐藏，页面其余照常（不阻塞）", async () => {
    install({ planCode: "free", datasets: [], failQuotaStatus: 404 });
    render(<DatasetsPage planCode="free" />);

    expect(await screen.findByTestId("datasets-empty")).toBeTruthy();
    expect(screen.queryByTestId("datasets-quota")).toBeNull();
    expect(screen.getByTestId("upload-section")).toBeTruthy();
    expect(screen.getByTestId("connectors-section")).toBeTruthy();
  });

  it("配额触达上限态：配额条转 amber（exhausted）并附升档 CTA", async () => {
    install({
      planCode: "free",
      datasets: [
        makeDataset({ id: "ds-1", name: "集一" }),
        makeDataset({ id: "ds-2", name: "集二" }),
        makeDataset({ id: "ds-3", name: "集三" }),
      ],
    });
    render(<DatasetsPage planCode="free" />);

    const quota = await screen.findByTestId("datasets-quota");
    expect(quota.getAttribute("data-exhausted")).toBe("true");
    expect(quota.textContent).toContain("3/3");
    expect(
      screen.getByTestId("datasets-quota-upgrade").getAttribute("href"),
    ).toMatch(/^mailto:/);
  });

  it("付费档零变化：无配额条/付费徽标/存储徽标，连接器向导照常且不取配额", async () => {
    const backend = install({
      datasets: [makeDataset({ id: "ds-1", name: "门店销量" })],
      connectors: [makeConnector({ id: "cn-1", name: "erp-connector" })],
    });
    render(<DatasetsPage planCode="basic" />);

    expect(await screen.findByTestId("dataset-card")).toBeTruthy();
    expect(screen.queryByTestId("datasets-quota")).toBeNull();
    expect(screen.queryByTestId("dataset-tier-badge")).toBeNull();
    expect(screen.queryByTestId("connectors-paid-badge")).toBeNull();
    expect(screen.getByTestId("pairing-wizard")).toBeTruthy();
    expect(screen.getByTestId("whitelist-section")).toBeTruthy();
    expect(
      backend.calls.filter(
        (c) => c.url.pathname === "/api/v1/datasets/quota",
      ),
    ).toEqual([]);
  });

  it("plan_code 未下发（空串）→ 不按免费档阻塞，正常渲染数据面", async () => {
    install();
    render(<DatasetsPage planCode="" />);
    expect(await screen.findByTestId("upload-section")).toBeTruthy();
  });
});

// ── 定时刷新小节（wire-refresh-scheduler 任务 4.1/4.2）──────────────────────
// derived 数据集详情页：创建流（POST body 断言）/暂停恢复切换/错误展示；
// 非 derived 数据集不出现该小节。端点走 testing.ts 假后端 schedules 路由。
describe("DatasetDetail 定时刷新小节（wire-refresh-scheduler 4.1/4.2）", () => {
  /** 调度行种子公共段（next_run_at/上次运行/上次错误等）。 */
  const SCHEDULE_BASE = {
    next_run_at: "2026-10-11T09:00:00",
    last_status: "ok" as const,
    last_run_at: "2026-10-11T08:00:00",
    last_error: null,
    created_at: "2026-10-10T08:00:00",
  };

  it("创建流：derived 数据集出小节，预设 1h/6h/24h，选 24h 创建 → POST body 断言 → 状态行", async () => {
    const backend = install({
      datasets: [
        makeDataset({ id: "ds-1", name: "派生集", source_kind: "derived" }),
      ],
    });
    render(<DatasetsPage planCode="basic" />);
    fireEvent.click(await screen.findByTestId("dataset-open"));

    const section = await screen.findByTestId("schedule-section");
    // 等列表落地（无调度 → 创建面）再交互，避开 loading 态竞态。
    const create = await within(section).findByTestId("schedule-create");
    // 预设档 = dataset 地板（60 分钟）起步：1h/6h/24h
    expect(
      within(create)
        .getAllByTestId("schedule-interval")
        .map((el) => el.getAttribute("data-minutes")),
    ).toEqual(["60", "360", "1440"]);
    // 默认选中首档；改选 24h（1440）后创建
    fireEvent.click(within(create).getAllByTestId("schedule-interval")[2]!);
    fireEvent.click(within(create).getByTestId("schedule-create-submit"));

    expect(await screen.findByTestId("schedule-row")).toBeTruthy();
    const post = postCalls(backend, "/api/v1/refresh-schedules");
    expect(post).toHaveLength(1);
    expect(post[0]!.body).toEqual({
      target_type: "dataset",
      target_id: "ds-1",
      interval_minutes: 1440,
    });
    // 创建后状态行：调度中徽标 + 下次运行时间
    expect(
      screen.getByTestId("schedule-row").getAttribute("data-paused"),
    ).toBe("false");
    expect(screen.getByTestId("schedule-state-badge").textContent).toContain(
      "调度中",
    );
    expect(screen.getByTestId("schedule-next-run").textContent).toContain(
      "下次运行",
    );
  });

  it("上传来源数据集：详情页无定时刷新小节（也不发起 schedules 请求）", async () => {
    const backend = install({
      datasets: [makeDataset({ id: "ds-1", name: "上传集" })],
    });
    render(<DatasetsPage planCode="basic" />);
    fireEvent.click(await screen.findByTestId("dataset-open"));
    await screen.findByTestId("preview-grid");

    expect(screen.queryByTestId("schedule-section")).toBeNull();
    expect(
      backend.calls.filter(
        (c) => c.url.pathname === "/api/v1/refresh-schedules",
      ),
    ).toEqual([]);
  });

  it("暂停/恢复：POST /{id}/pause → 已暂停徽标；再点恢复 → 回调度中（含上次结果/错误展示）", async () => {
    const backend = install({
      datasets: [
        makeDataset({ id: "ds-1", name: "派生集", source_kind: "derived" }),
      ],
      refreshSchedules: [
        {
          id: 7,
          target_type: "dataset",
          target_id: "ds-1",
          interval_minutes: 360,
          paused: false,
          ...SCHEDULE_BASE,
          last_status: "failed",
          last_error: "上游列缺失：price",
        },
      ],
    });
    render(<DatasetsPage planCode="basic" />);
    fireEvent.click(await screen.findByTestId("dataset-open"));

    // 状态行如实呈现：上次结果 failed + 上次错误
    await screen.findByTestId("schedule-row");
    expect(
      screen.getByTestId("schedule-last-status").getAttribute("data-status"),
    ).toBe("failed");
    expect(screen.getByTestId("schedule-last-error").textContent).toContain(
      "price",
    );

    // 暂停：POST /7/pause → 已暂停徽标
    fireEvent.click(screen.getByTestId("schedule-toggle"));
    await waitFor(() =>
      expect(
        postCalls(backend, "/api/v1/refresh-schedules/7/pause"),
      ).toHaveLength(1),
    );
    await waitFor(() => {
      expect(
        screen.getByTestId("schedule-row").getAttribute("data-paused"),
      ).toBe("true");
      expect(screen.getByTestId("schedule-state-badge").textContent).toContain(
        "已暂停",
      );
    });

    // 恢复：POST /7/resume → 回调度中
    fireEvent.click(screen.getByTestId("schedule-toggle"));
    await waitFor(() =>
      expect(
        postCalls(backend, "/api/v1/refresh-schedules/7/resume"),
      ).toHaveLength(1),
    );
    await waitFor(() =>
      expect(
        screen.getByTestId("schedule-row").getAttribute("data-paused"),
      ).toBe("false"),
    );
  });

  it("创建被拒（422 地板）：后端 detail 如实呈现，停留在创建面可重试", async () => {
    install({
      datasets: [
        makeDataset({ id: "ds-1", name: "派生集", source_kind: "derived" }),
      ],
      failScheduleCreateStatus: 422,
      failScheduleCreateDetail: "刷新间隔不能低于 60 分钟（当前 30）",
    });
    render(<DatasetsPage planCode="basic" />);
    fireEvent.click(await screen.findByTestId("dataset-open"));

    const create = await screen.findByTestId("schedule-create");
    fireEvent.click(within(create).getByTestId("schedule-create-submit"));

    expect(
      (await screen.findByTestId("schedule-op-error")).textContent,
    ).toContain("刷新间隔不能低于 60 分钟");
    // 失败后不切换状态行，创建面仍在（可改选重试）
    expect(screen.getByTestId("schedule-create")).toBeTruthy();
    expect(screen.queryByTestId("schedule-row")).toBeNull();
  });
});

// ── 管线子视图（wire-transform-pipeline-view 任务 2.2/2.3）──────────────────
// 数据页「数据集 / 管线」分段切换；管线视图由种子数据集在假后端组装成图
//（上传源 ds-1 ← derived ds-2，source_ref 指父 key），点击节点进详情。
describe("DatasetsPage 管线子视图（wire-transform-pipeline-view 2.2/2.3）", () => {
  it("子视图入口与切换：默认列表；切「管线」默认工作台，进「图谱」拉图（Bearer），列表/上传/连接器区让位", async () => {
    const backend = install({
      datasets: [
        makeDataset({ id: "ds-1", name: "上传源" }),
        makeDataset({
          id: "ds-2",
          name: "派生集",
          source_kind: "derived",
          source_ref: "ds_1",
        }),
      ],
    });
    render(<DatasetsPage planCode="basic" />);

    // 默认列表视图：列表在、管线未请求
    expect(await screen.findAllByTestId("dataset-card")).toHaveLength(2);
    expect(screen.getByTestId("datasets-subview")).toBeTruthy();
    expect(
      backend.calls.filter((c) => c.url.pathname === "/api/v1/pipeline/graph"),
    ).toEqual([]);

    fireEvent.click(screen.getByTestId("datasets-subview-pipeline"));
    // 默认进「管线」工作台（图谱不预取）
    expect(await screen.findByTestId("pipeline-view")).toBeTruthy();
    expect(await screen.findByTestId("pipeline-workbench")).toBeTruthy();
    expect(
      backend.calls.filter((c) => c.url.pathname === "/api/v1/pipeline/graph"),
    ).toEqual([]);

    // 切「图谱」页签：图请求带 Bearer 登录头；假后端组装：两节点两深度、
    // 一条父→子边
    fireEvent.click(screen.getByTestId("pipeline-tab-graph"));
    const columns = await screen.findAllByTestId("pipeline-column");
    const graphCalls = backend.calls.filter(
      (c) => c.url.pathname === "/api/v1/pipeline/graph",
    );
    expect(graphCalls).toHaveLength(1);
    expect(graphCalls[0]!.headers["authorization"]).toMatch(/^Bearer /);
    expect(
      columns.map((el) => el.getAttribute("data-depth")),
    ).toEqual(["0", "1"]);
    expect(screen.getAllByTestId("pipeline-edge")).toHaveLength(1);
    // 列表视图专属区块让位（管线视图自身管理加载/错误态）
    expect(screen.queryByTestId("dataset-list")).toBeNull();
    expect(screen.queryByTestId("upload-section")).toBeNull();
    expect(screen.queryByTestId("connectors-section")).toBeNull();
  });

  it("管线节点点击进详情，返回后仍停留在管线子视图", async () => {
    install({
      datasets: [
        makeDataset({ id: "ds-1", name: "上传源" }),
        makeDataset({
          id: "ds-2",
          name: "派生集",
          source_kind: "derived",
          source_ref: "ds_1",
        }),
      ],
      datasetRows: { "ds-2": { columns: [{ name: "id", type: "INTEGER" }], rows: [{ id: 1 }] } },
    });
    render(<DatasetsPage planCode="basic" />);
    fireEvent.click(screen.getByTestId("datasets-subview-pipeline"));
    fireEvent.click(screen.getByTestId("pipeline-tab-graph"));

    const node = (await screen.findAllByTestId("pipeline-node")).find(
      (el) => el.getAttribute("data-dataset-id") === "ds-2",
    );
    fireEvent.click(node!);
    expect(await screen.findByTestId("dataset-detail")).toBeTruthy();
    expect(screen.getByTestId("detail-name").textContent).toBe("派生集");

    fireEvent.click(screen.getByTestId("detail-back"));
    // 返回后回到管线子视图（子视图选择在详情态期间保持）
    expect(await screen.findByTestId("pipeline-view")).toBeTruthy();
    expect(screen.queryByTestId("dataset-list")).toBeNull();
  });

  it("切回列表视图：列表重取渲染，管线视图卸载", async () => {
    const backend = install({
      datasets: [
        makeDataset({ id: "ds-1", name: "上传源" }),
        makeDataset({
          id: "ds-2",
          name: "派生集",
          source_kind: "derived",
          source_ref: "ds_1",
        }),
      ],
    });
    render(<DatasetsPage planCode="basic" />);
    await screen.findAllByTestId("dataset-card");
    fireEvent.click(screen.getByTestId("datasets-subview-pipeline"));
    await screen.findByTestId("pipeline-view");

    fireEvent.click(screen.getByTestId("datasets-subview-list"));
    expect(await screen.findAllByTestId("dataset-card")).toHaveLength(2);
    expect(screen.queryByTestId("pipeline-view")).toBeNull();
    expect(screen.getByTestId("upload-section")).toBeTruthy();
    // 切回列表重取数据集列表（第二次 GET /api/v1/datasets）
    await waitFor(() =>
      expect(
        backend.calls.filter(
          (c) =>
            c.method === "GET" && c.url.pathname === "/api/v1/datasets",
        ).length,
      ).toBe(2),
    );
  });
});

// ── 资产目录（wire-asset-catalog 任务 5.1 / wire-dataset-quality 列表徽章）──
// 目录=本页升级（D5）：筛选/搜索/分页联动 GET /api/v1/assets；徽章按本页
// id 批量拉 GET /api/v1/quality/summary（fresh/stale/unknown；缺失键或端点
// 失败=无徽章，绝不断列表）。假后端语义见 testing.ts assets/quality 路由。
describe("DatasetsPage 资产目录（筛选/搜索/分页 + 质量徽章，任务 5.1）", () => {
  function assetsCalls(
    backend: ReturnType<typeof installConsoleBackend>,
  ): FetchCall[] {
    return backend.calls.filter(
      (call) => call.method === "GET" && call.url.pathname === "/api/v1/assets",
    );
  }

  it("筛选与搜索联动 GET /api/v1/assets：下拉即席、文本经「筛选」提交、参数完整（Bearer）", async () => {
    const backend = install({
      datasets: [
        makeDataset({ id: "ds-1", name: "门店销量" }),
        makeDataset({ id: "ds-2", name: "ERP 订单", source_kind: "connector" }),
      ],
      assetMeta: {
        "ds-1": { domain: "销售", tags: ["月度"], description: "门店月度销量" },
        "ds-2": { domain: "供应链", sensitivity: "内部" },
      },
    });
    render(<DatasetsPage planCode="basic" />);
    await screen.findAllByTestId("dataset-card");
    expect(assetsCalls(backend)).toHaveLength(1);

    // 下拉即席提交（页码回 1）：敏感度 / 来源类型 / 新鲜度
    fireEvent.change(screen.getByTestId("assets-filter-sensitivity"), {
      target: { value: "内部" },
    });
    await waitFor(() => expect(assetsCalls(backend)).toHaveLength(2));
    let call = assetsCalls(backend)[1]!;
    expect(call.url.searchParams.get("sensitivity")).toBe("内部");
    expect(call.url.searchParams.get("page")).toBe("1");
    expect(call.headers["authorization"]).toMatch(/^Bearer /);

    fireEvent.change(screen.getByTestId("assets-filter-source-kind"), {
      target: { value: "connector" },
    });
    await waitFor(() => expect(assetsCalls(backend)).toHaveLength(3));
    fireEvent.change(screen.getByTestId("assets-filter-freshness"), {
      target: { value: "stale" },
    });
    await waitFor(() => expect(assetsCalls(backend)).toHaveLength(4));
    expect(assetsCalls(backend)[2]!.url.searchParams.get("source_kind")).toBe(
      "connector",
    );
    expect(assetsCalls(backend)[3]!.url.searchParams.get("freshness")).toBe(
      "stale",
    );

    // 文本草稿：不提交不发请求；点「筛选」后 domain/tag/q 进 query
    fireEvent.change(screen.getByTestId("assets-filter-domain"), {
      target: { value: "销售" },
    });
    fireEvent.change(screen.getByTestId("assets-filter-tag"), {
      target: { value: "月度" },
    });
    fireEvent.change(screen.getByTestId("assets-filter-q"), {
      target: { value: "门店" },
    });
    expect(assetsCalls(backend)).toHaveLength(4);
    fireEvent.click(screen.getByTestId("assets-filter-apply"));
    await waitFor(() => expect(assetsCalls(backend)).toHaveLength(5));
    call = assetsCalls(backend)[4]!;
    expect(call.url.searchParams.get("domain")).toBe("销售");
    expect(call.url.searchParams.get("tag")).toBe("月度");
    expect(call.url.searchParams.get("q")).toBe("门店");
    expect(call.url.searchParams.get("sensitivity")).toBe("内部");
    expect(call.url.searchParams.get("source_kind")).toBe("connector");
    expect(call.url.searchParams.get("freshness")).toBe("stale");

    // 重置：清空全部筛选（无过滤参数），列表回到全量
    fireEvent.click(screen.getByTestId("assets-filter-reset"));
    await waitFor(() => expect(assetsCalls(backend)).toHaveLength(6));
    call = assetsCalls(backend)[5]!;
    expect(call.url.searchParams.get("domain")).toBeNull();
    expect(call.url.searchParams.get("sensitivity")).toBeNull();
    expect(call.url.searchParams.get("q")).toBeNull();
    expect(await screen.findAllByTestId("dataset-card")).toHaveLength(2);
  });

  it("服务端过滤语义：组合筛选（域+敏感度）与搜索（名称/描述）只命中满足条件的数据集", async () => {
    install({
      datasets: [
        makeDataset({ id: "ds-1", name: "门店销量" }),
        makeDataset({ id: "ds-2", name: "ERP 订单", source_kind: "connector" }),
        makeDataset({ id: "ds-3", name: "门店库存" }),
      ],
      assetMeta: {
        "ds-1": { domain: "销售", sensitivity: "内部", description: "门店月度销量" },
        "ds-2": { domain: "销售", sensitivity: "敏感" },
        "ds-3": { domain: "供应链" },
      },
    });
    render(<DatasetsPage planCode="basic" />);
    await screen.findAllByTestId("dataset-card");

    // 组合筛选：业务域=销售 且 敏感度=内部 → 仅 ds-1
    fireEvent.change(screen.getByTestId("assets-filter-domain"), {
      target: { value: "销售" },
    });
    fireEvent.click(screen.getByTestId("assets-filter-apply"));
    await waitFor(() =>
      expect(screen.getAllByTestId("dataset-card")).toHaveLength(2),
    );
    fireEvent.change(screen.getByTestId("assets-filter-sensitivity"), {
      target: { value: "内部" },
    });
    await waitFor(() =>
      expect(screen.getAllByTestId("dataset-card")).toHaveLength(1),
    );
    expect(screen.getByTestId("dataset-name").textContent).toBe("门店销量");

    // 搜索命中描述关键字：重置后按「月度销量」检索 → ds-1
    fireEvent.click(screen.getByTestId("assets-filter-reset"));
    await waitFor(() =>
      expect(screen.getAllByTestId("dataset-card")).toHaveLength(3),
    );
    fireEvent.change(screen.getByTestId("assets-filter-q"), {
      target: { value: "月度销量" },
    });
    fireEvent.click(screen.getByTestId("assets-filter-apply"));
    await waitFor(() =>
      expect(screen.getAllByTestId("dataset-card")).toHaveLength(1),
    );
    expect(screen.getByTestId("dataset-name").textContent).toBe("门店销量");

    // 筛选无命中：filtered 空态 + 一键重置
    fireEvent.change(screen.getByTestId("assets-filter-domain"), {
      target: { value: "不存在域" },
    });
    fireEvent.click(screen.getByTestId("assets-filter-apply"));
    expect(await screen.findByTestId("assets-filtered-empty")).toBeTruthy();
    fireEvent.click(screen.getByTestId("assets-filtered-reset"));
    await waitFor(() =>
      expect(screen.getAllByTestId("dataset-card")).toHaveLength(3),
    );
  });

  it("卡片资产元数据：业务域/标签呈现，内部与敏感带标注（常规不占位）", async () => {
    install({
      datasets: [
        makeDataset({ id: "ds-1", name: "门店销量" }),
        makeDataset({ id: "ds-2", name: "ERP 订单" }),
        makeDataset({ id: "ds-3", name: "客服工单" }),
      ],
      assetMeta: {
        "ds-1": { domain: "销售", tags: ["月度", "门店"] },
        "ds-2": { sensitivity: "内部" },
        "ds-3": { sensitivity: "敏感" },
      },
    });
    render(<DatasetsPage planCode="basic" />);
    await screen.findAllByTestId("dataset-card");

    expect(screen.getByTestId("dataset-asset-domain").textContent).toBe("销售");
    expect(
      screen.getAllByTestId("dataset-asset-tag").map((el) => el.textContent),
    ).toEqual(["#月度", "#门店"]);
    const sensitivityBadges = screen.getAllByTestId("dataset-sensitivity-badge");
    expect(
      sensitivityBadges.map((el) => el.getAttribute("data-sensitivity")),
    ).toEqual(["内部", "敏感"]);
    // 常规（默认）不渲染敏感度标注
    expect(sensitivityBadges).toHaveLength(2);
  });

  it("质量徽章：fresh=正常 / stale=过期 / unknown=未知，summary 缺失键无徽章", async () => {
    const backend = install({
      datasets: [
        makeDataset({ id: "ds-1", name: "新鲜集" }),
        makeDataset({ id: "ds-2", name: "过期集" }),
        makeDataset({ id: "ds-3", name: "未知集" }),
        makeDataset({ id: "ds-4", name: "无种子集" }),
      ],
      quality: {
        "ds-1": { freshness: { freshness: "fresh", last_refresh_at: NOW } },
        "ds-2": { freshness: { freshness: "stale", last_refresh_at: NOW } },
        "ds-3": { freshness: "unknown" },
      },
    });
    render(<DatasetsPage planCode="basic" />);
    await screen.findAllByTestId("dataset-card");

    // 汇总请求带本页全部 dataset ids（逗号拼接）
    const summaryCall = backend.calls.find(
      (call) => call.url.pathname === "/api/v1/quality/summary",
    );
    expect(summaryCall?.url.searchParams.get("dataset_ids")).toBe(
      "ds-1,ds-2,ds-3,ds-4",
    );

    const badges = await screen.findAllByTestId("dataset-quality-badge");
    expect(badges.map((el) => el.getAttribute("data-freshness"))).toEqual([
      "fresh",
      "stale",
      "unknown",
    ]);
    expect(badges.map((el) => el.textContent)).toEqual(["正常", "过期", "未知"]);
    // 无质量种子的条目（summary 缺失键）不渲染徽章
    expect(badges).toHaveLength(3);
  });

  it("summary 端点失败：徽章静默缺席，列表照常渲染（不报错不断页）", async () => {
    install({
      datasets: [makeDataset({ id: "ds-1", name: "门店销量" })],
      quality: { "ds-1": { freshness: "fresh" } },
      failQualitySummaryStatus: 500,
    });
    render(<DatasetsPage planCode="basic" />);

    expect(await screen.findByTestId("dataset-card")).toBeTruthy();
    await waitFor(() =>
      expect(
        screen
          .getAllByTestId("dataset-card")
          .filter((el) =>
            el.querySelector('[data-testid="dataset-quality-badge"]'),
          ).length,
      ).toBe(0),
    );
    expect(screen.queryByTestId("datasets-error")).toBeNull();
  });

  it("分页：超页大小出分页条，翻页带 page 参数且只渲染当页条目", async () => {
    const backend = install({
      datasets: Array.from({ length: 21 }, (_, i) =>
        makeDataset({ id: `ds-${i + 1}`, name: `数据集 ${i + 1}` }),
      ),
    });
    render(<DatasetsPage planCode="basic" />);
    await screen.findAllByTestId("dataset-card");

    // 首页：20 条 + 分页信息（1/2 页）
    expect(screen.getAllByTestId("dataset-card")).toHaveLength(20);
    expect(screen.getByTestId("assets-pagination-info").textContent).toContain(
      "21",
    );
    expect(screen.getByTestId("assets-pagination-info").textContent).toContain(
      "1 / 2",
    );
    expect(
      (screen.getByTestId("assets-prev-page") as HTMLButtonElement).disabled,
    ).toBe(true);

    // 翻页：page=2 → 第二页仅 1 条；next 禁用
    fireEvent.click(screen.getByTestId("assets-next-page"));
    await waitFor(() =>
      expect(screen.getAllByTestId("dataset-card")).toHaveLength(1),
    );
    const paged = backend.calls
      .filter(
        (call) =>
          call.method === "GET" && call.url.pathname === "/api/v1/assets",
      )
      .at(-1)!;
    expect(paged.url.searchParams.get("page")).toBe("2");
    expect(
      (screen.getByTestId("assets-next-page") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("目录端点失败：错误态 + 重试（既有错误面语义）", async () => {
    install({ datasets: [], failAssetsStatus: 500 });
    render(<DatasetsPage planCode="basic" />);

    expect(await screen.findByTestId("datasets-error")).toBeTruthy();
    expect(screen.queryByTestId("dataset-card")).toBeNull();
  });

  it("目录条目缺 asset 键（wire-console-ux-fixes 任务 1.2）：卡片照常渲染、无元数据行、不白屏", async () => {
    // 契约切换期后端可能漏发 asset 段——前端容错读法（asset?.）是本次任务
    // 的验收面。假后端形状（testing.ts /assets 嵌套口径）不动，这里在
    // fetchImpl 外围包一层：仅对 /api/v1/assets 响应注入畸形条目。
    const backend = install({ datasets: [makeDataset({ id: "ds-1", name: "门店销量" })] });
    const inner = backend.fetchImpl;
    const stripAsset = ((input: RequestInfo | URL, init?: RequestInit) =>
      Promise.resolve(inner(input, init)).then((response) => {
        const url = new URL(String(input), "http://console.test");
        if (url.pathname !== "/api/v1/assets" || response.status >= 400) {
          return response;
        }
        return response.json().then((body) => {
          const items = (body as { items?: Array<Record<string, unknown>> }).items ?? [];
          const stripped = items.map(({ asset: _asset, ...rest }) => rest);
          return { ...response, json: () => Promise.resolve({ ...body, items: stripped }) };
        });
      })) as unknown as typeof fetch;
    vi.stubGlobal("fetch", stripAsset);

    render(<DatasetsPage planCode="basic" />);

    // 列表照常：卡片名/来源/行数在，无 asset 元数据行（域/标签/敏感度全缺席）
    expect(await screen.findByTestId("dataset-card")).toBeTruthy();
    expect(screen.getByTestId("dataset-name").textContent).toBe("门店销量");
    expect(screen.getByTestId("dataset-rows").textContent).toContain("120");
    expect(screen.queryByTestId("dataset-asset-meta")).toBeNull();
    expect(screen.queryByTestId("dataset-asset-domain")).toBeNull();
    expect(screen.queryByTestId("dataset-sensitivity-badge")).toBeNull();
    // 不白屏：错误面也没被触发（容错而非报错）
    expect(screen.queryByTestId("datasets-error")).toBeNull();
  });
});

// ── 配额条档位未知降级（wire-console-resilience D4）──────────────────────
describe("DatasetsPage 配额条：档位未知 null 降级", () => {
  it("max_* 为 null（档位 fail-closed）→ 中性提示行，不渲染数值条/CTA 且不崩", async () => {
    install({
      datasets: [],
      datasetQuota: {
        plan_code: "",
        used_datasets: 0,
        max_datasets: null,
        max_rows_per_dataset: null,
      } as unknown as DatasetQuota,
    });
    render(<DatasetsPage planCode="free" />);

    const bar = await screen.findByTestId("datasets-quota");
    expect(bar.getAttribute("data-unknown")).toBe("true");
    expect(bar.textContent).toContain("当前档位限额信息不可用");
    // 降级态不渲染升档引导（数值未知时 CTA 语义不成立）
    expect(screen.queryByTestId("datasets-quota-upgrade")).toBeNull();
    // 页面其余功能不受影响（未触发错误面）
    expect(screen.queryByTestId("datasets-error")).toBeNull();
  });

  it("限额齐全 → 数值条逐字零漂移（含触达上限 amber + 升档引导）", async () => {
    install({
      datasets: [],
      datasetQuota: {
        plan_code: "free",
        used_datasets: 3,
        max_datasets: 3,
        max_rows_per_dataset: 10000,
      },
    });
    render(<DatasetsPage planCode="free" />);

    const bar = await screen.findByTestId("datasets-quota");
    expect(bar.getAttribute("data-unknown")).toBeNull();
    expect(bar.getAttribute("data-exhausted")).toBe("true");
    expect(bar.textContent).toContain("已用 3/3");
    expect(bar.textContent).toContain("10,000");
    expect(screen.getByTestId("datasets-quota-upgrade")).toBeTruthy();
  });
});

// ── 执行成功事件刷新（wire-copilot-action-gate 任务 4.1，design D9）────────
// 闸执行成功后 api.ts 广播 wire:action-executed；「我的数据」命中 resources
// 才重取资产目录（filters/page 原样参与请求，不重置筛选）。
describe("DatasetsPage（执行成功事件刷新，wire-copilot-action-gate 4.1）", () => {
  it("wire:action-executed 命中 datasets 即重取目录", async () => {
    const backend = install({
      datasets: [makeDataset({ id: "ds-1", name: "销售明细" })],
    });
    render(<DatasetsPage planCode="basic" />);
    await screen.findAllByTestId("dataset-card");

    const catalogCalls = () =>
      backend.calls.filter(
        (c) => c.method === "GET" && c.url.pathname === "/api/v1/assets",
      ).length;
    const before = catalogCalls();

    window.dispatchEvent(
      new CustomEvent("wire:action-executed", {
        detail: { resources: ["datasets"] },
      }),
    );

    await waitFor(() => expect(catalogCalls()).toBeGreaterThan(before));
  });

  it("resources 不含 datasets：不重取", async () => {
    const backend = install({
      datasets: [makeDataset({ id: "ds-1", name: "销售明细" })],
    });
    render(<DatasetsPage planCode="basic" />);
    await screen.findAllByTestId("dataset-card");

    const before = backend.calls.filter(
      (c) => c.method === "GET" && c.url.pathname === "/api/v1/assets",
    ).length;

    window.dispatchEvent(
      new CustomEvent("wire:action-executed", {
        detail: { resources: ["artifacts"] },
      }),
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(
      backend.calls.filter(
        (c) => c.method === "GET" && c.url.pathname === "/api/v1/assets",
      ).length,
    ).toBe(before);
  });
});
