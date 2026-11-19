// 数据集级访问授权测试（wire-dataset-acl D5 web 面）：
// - owner 才渲染「访问」面板（角色取既有团队面 me.role）；editor 隐藏且
//   零访问态请求；
// - 模式切换（team ⇄ restricted）与整表替换保存载荷
//   {mode, grants:[{logto_sub, permission}]}（mode=team 时 grants 空数组）；
// - 授权表增删、保存成功/失败如实回显；
// - 列表卡片与详情头部的「受限」徽标（acl_mode=restricted 才渲染）；
// - 新 i18n 键在 zh/en 两语言均存在（i18nKeys.test 另把关全键对仗）。
// fetch 一律走 testing.ts 内存假后端（零网络）。
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
import zh from "../i18n/zh";
import en from "../i18n/en";
import { DatasetAccessSection } from "./DatasetAccessSection";
import { DatasetsPage } from "./DatasetsPage";
import {
  installConsoleBackend,
  seedConsoleSession,
  type FetchCall,
} from "./testing";
import type { Dataset } from "./api";

const CUSTOMER = "demo-customer";
const EDITOR_SUB = "editor-sub";
const NOW = "2026-10-31T10:00:00";

function makeDataset(
  partial: Partial<Dataset> & Pick<Dataset, "id" | "name">,
): Dataset {
  return {
    key: partial.id.replace(/-/g, "_"),
    source_kind: "upload",
    connector_id: null,
    source_ref: null,
    columns: [
      { name: "id", type: "INTEGER" },
      { name: "city", type: "TEXT" },
    ],
    row_count: 10,
    size_bytes: 512,
    sync_cron: null,
    sync_status: "ok",
    paused_reason: null,
    created_at: NOW,
    updated_at: NOW,
    customer_id: CUSTOMER,
    ...partial,
  };
}

function install(options?: Parameters<typeof installConsoleBackend>[1]) {
  const backend = installConsoleBackend([], options);
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

function callsOf(
  backend: ReturnType<typeof install>,
  filter: { method?: string; path: string },
): FetchCall[] {
  return backend.calls.filter(
    (call) =>
      call.url.pathname === filter.path &&
      (filter.method == null || call.method === filter.method),
  );
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  seedConsoleSession({ sub: CUSTOMER });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

describe("DatasetAccessSection（owner-only 访问面板）", () => {
  it("owner：渲染模式切换与授权表，读取既有访问态", async () => {
    install({
      datasets: [makeDataset({ id: "ds-1", name: "门店销量" })],
      teamMembers: [{ logtoSub: EDITOR_SUB, role: "editor" }],
      datasetAcl: {
        "ds-1": {
          mode: "restricted",
          grants: [{ logto_sub: EDITOR_SUB, permission: "view" }],
        },
      },
    });
    render(<DatasetAccessSection datasetId="ds-1" />);

    const section = await screen.findByTestId("dataset-access-section");
    await waitFor(() =>
      expect(
        screen.getByTestId("dataset-access-mode-restricted").getAttribute(
          "aria-pressed",
        ),
      ).toBe("true"),
    );
    expect(
      screen.getByTestId("dataset-access-mode-team").getAttribute("aria-pressed"),
    ).toBe("false");

    const row = await screen.findByTestId("dataset-access-grant-row");
    expect(row.getAttribute("data-sub")).toBe(EDITOR_SUB);
    expect(
      (within(row).getByTestId(
        "dataset-access-grant-permission",
      ) as HTMLSelectElement).value,
    ).toBe("view");
    expect(section.getAttribute("data-testid")).toBe("dataset-access-section");
  });

  it("editor：不渲染入口，且不发起访问态请求", async () => {
    const backend = install({
      datasets: [makeDataset({ id: "ds-1", name: "门店销量" })],
      teamMembers: [{ logtoSub: CUSTOMER, role: "editor" }],
    });
    render(<DatasetAccessSection datasetId="ds-1" />);

    // 等团队面请求落地（角色已解析）后仍是零渲染。
    await waitFor(() =>
      expect(
        callsOf(backend, { path: "/api/console/team" }).length,
      ).toBeGreaterThan(0),
    );
    await waitFor(() =>
      expect(screen.queryByTestId("dataset-access-section")).toBeNull(),
    );
    expect(callsOf(backend, { path: "/api/v1/datasets/ds-1/access" })).toHaveLength(
      0,
    );
  });

  it("切受限 + 授权成员 + 保存：PUT 整表替换载荷 {mode, grants:[{logto_sub, permission}]}", async () => {
    const backend = install({
      datasets: [makeDataset({ id: "ds-1", name: "门店销量" })],
      teamMembers: [{ logtoSub: EDITOR_SUB, role: "editor" }],
    });
    render(<DatasetAccessSection datasetId="ds-1" />);
    await screen.findByTestId("dataset-access-section");
    await waitFor(() =>
      expect(
        screen.getByTestId("dataset-access-mode-team").getAttribute(
          "aria-pressed",
        ),
      ).toBe("true"),
    );

    fireEvent.click(screen.getByTestId("dataset-access-mode-restricted"));
    await screen.findByTestId("dataset-access-grants");
    expect(screen.getByTestId("dataset-access-grants-empty")).toBeTruthy();

    fireEvent.change(screen.getByTestId("dataset-access-member-select"), {
      target: { value: EDITOR_SUB },
    });
    fireEvent.change(screen.getByTestId("dataset-access-member-permission"), {
      target: { value: "edit" },
    });
    fireEvent.click(screen.getByTestId("dataset-access-grant-add"));

    const row = screen.getByTestId("dataset-access-grant-row");
    expect(row.getAttribute("data-sub")).toBe(EDITOR_SUB);
    expect(
      (within(row).getByTestId(
        "dataset-access-grant-permission",
      ) as HTMLSelectElement).value,
    ).toBe("edit");

    fireEvent.click(screen.getByTestId("dataset-access-save"));
    await screen.findByTestId("dataset-access-saved");

    const puts = callsOf(backend, {
      method: "PUT",
      path: "/api/v1/datasets/ds-1/access",
    });
    expect(puts).toHaveLength(1);
    expect(puts[0]!.body).toEqual({
      mode: "restricted",
      grants: [{ logto_sub: EDITOR_SUB, permission: "edit" }],
    });
    // 假后端回写后内存态与提交一致（整表替换幂等）。
    expect(backend.getDatasetAccess("ds-1")).toEqual({
      mode: "restricted",
      grants: [{ logto_sub: EDITOR_SUB, permission: "edit" }],
    });
  });

  it("移除授权行后保存：载荷名单同步收缩", async () => {
    const backend = install({
      datasets: [makeDataset({ id: "ds-1", name: "门店销量" })],
      teamMembers: [{ logtoSub: EDITOR_SUB, role: "editor" }],
      datasetAcl: {
        "ds-1": {
          mode: "restricted",
          grants: [{ logto_sub: EDITOR_SUB, permission: "edit" }],
        },
      },
    });
    render(<DatasetAccessSection datasetId="ds-1" />);
    await screen.findByTestId("dataset-access-section");
    const row = await screen.findByTestId("dataset-access-grant-row");

    fireEvent.click(within(row).getByTestId("dataset-access-grant-remove"));
    expect(screen.queryByTestId("dataset-access-grant-row")).toBeNull();
    fireEvent.click(screen.getByTestId("dataset-access-save"));
    await screen.findByTestId("dataset-access-saved");

    const puts = callsOf(backend, {
      method: "PUT",
      path: "/api/v1/datasets/ds-1/access",
    });
    expect(puts[0]!.body).toEqual({ mode: "restricted", grants: [] });
  });

  it("切回团队可见并保存：grants 按契约提交空数组（服务端同语义清空）", async () => {
    const backend = install({
      datasets: [makeDataset({ id: "ds-1", name: "门店销量" })],
      teamMembers: [{ logtoSub: EDITOR_SUB, role: "editor" }],
      datasetAcl: {
        "ds-1": {
          mode: "restricted",
          grants: [{ logto_sub: EDITOR_SUB, permission: "view" }],
        },
      },
    });
    render(<DatasetAccessSection datasetId="ds-1" />);
    await screen.findByTestId("dataset-access-section");
    await screen.findByTestId("dataset-access-grant-row");

    fireEvent.click(screen.getByTestId("dataset-access-mode-team"));
    await screen.findByTestId("dataset-access-team-hint");
    fireEvent.click(screen.getByTestId("dataset-access-save"));
    await screen.findByTestId("dataset-access-saved");

    const puts = callsOf(backend, {
      method: "PUT",
      path: "/api/v1/datasets/ds-1/access",
    });
    expect(puts[0]!.body).toEqual({ mode: "team", grants: [] });
  });

  it("保存失败：后端 detail 如实回显", async () => {
    install({
      datasets: [makeDataset({ id: "ds-1", name: "门店销量" })],
      failDatasetAclPutStatus: 400,
      failDatasetAclPutDetail: "授权名单包含 1 个不属于本企业的成员：ghost-sub",
    });
    render(<DatasetAccessSection datasetId="ds-1" />);
    await screen.findByTestId("dataset-access-section");
    fireEvent.click(screen.getByTestId("dataset-access-save"));
    const error = await screen.findByTestId("dataset-access-error");
    expect(error.textContent).toContain("ghost-sub");
  });
});

describe("受限徽标（列表卡片与详情头部）", () => {
  it("acl_mode=restricted 才渲染；team 数据集无徽标；详情头部同口径", async () => {
    install({
      datasets: [
        makeDataset({ id: "ds-1", name: "机密表", acl_mode: "restricted" }),
        makeDataset({ id: "ds-2", name: "普通表", acl_mode: "team" }),
      ],
      datasetAcl: { "ds-1": { mode: "restricted", grants: [] } },
    });
    render(<DatasetsPage planCode="basic" />);

    const cards = await screen.findAllByTestId("dataset-card");
    const restrictedCard = cards.find(
      (card) =>
        within(card).getByTestId("dataset-name").textContent === "机密表",
    )!;
    const teamCard = cards.find(
      (card) =>
        within(card).getByTestId("dataset-name").textContent === "普通表",
    )!;
    expect(within(restrictedCard).getByTestId("dataset-acl-badge")).toBeTruthy();
    expect(within(teamCard).queryByTestId("dataset-acl-badge")).toBeNull();

    fireEvent.click(within(restrictedCard).getByTestId("dataset-open"));
    const badge = await screen.findByTestId("detail-acl-badge");
    expect(badge.getAttribute("data-acl-mode")).toBe("restricted");
  });
});

describe("i18n 键位（zh/en 双语言同批）", () => {
  const keys = [
    "console.datasets.aclBadge.restricted",
    "console.dataset.acl.title",
    "console.dataset.acl.hint",
    "console.dataset.acl.loading",
    "console.dataset.acl.loadError",
    "console.dataset.acl.retry",
    "console.dataset.acl.save",
    "console.dataset.acl.saving",
    "console.dataset.acl.saved",
    "console.dataset.acl.saveError",
    "console.dataset.acl.modeLabel",
    "console.dataset.acl.modes.team",
    "console.dataset.acl.modes.restricted",
    "console.dataset.acl.teamHint",
    "console.dataset.acl.grantsHint",
    "console.dataset.acl.grantsEmpty",
    "console.dataset.acl.memberLabel",
    "console.dataset.acl.permissionLabel",
    "console.dataset.acl.actionsLabel",
    "console.dataset.acl.permissions.view",
    "console.dataset.acl.permissions.edit",
    "console.dataset.acl.remove",
    "console.dataset.acl.add",
    "console.dataset.acl.memberPlaceholder",
    "console.dataset.acl.noMembers",
  ];

  function read(locale: unknown, path: string): unknown {
    return path
      .split(".")
      .reduce<unknown>(
        (node, part) =>
          node && typeof node === "object"
            ? (node as Record<string, unknown>)[part]
            : undefined,
        locale,
      );
  }

  it("两语言均含全部访问面板键位且为非空字符串", () => {
    for (const key of keys) {
      expect(typeof read(zh, key), `zh 缺 ${key}`).toBe("string");
      expect(typeof read(en, key), `en 缺 ${key}`).toBe("string");
      expect((read(zh, key) as string).trim().length).toBeGreaterThan(0);
      expect((read(en, key) as string).trim().length).toBeGreaterThan(0);
    }
  });
});
