// 数据面 key 管理页组件测试（wire-customer-data-engine 6.2）：
// - 列表渲染（指纹脱敏展示、allowlist 作用域、在役状态）；
// - 铸造流：标签输入 → 明文一次性展示（复制提示）→ 列表刷新出现新行；
// - 吊销即时生效：行状态翻转、吊销按钮退场；
// - allowlist 选择：数据集芯片切换改变铸造载荷。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { KeysPage } from "./KeysPage";
import { installConsoleBackend, seedConsoleSession } from "./testing";

function install() {
  const backend = installConsoleBackend([], {});
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  sessionStorage.clear();
  seedConsoleSession({ sub: "demo-customer" });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("KeysPage（数据面 key · wire-customer-data-engine 6.2）", () => {
  it("铸造流：标签 → 明文一次性展示 → 列表出现新行（指纹脱敏）", async () => {
    const backend = install();
    render(<KeysPage />);

    const label = await screen.findByTestId("keys-mint-label");
    fireEvent.change(label, { target: { value: "订单系统" } });
    fireEvent.click(screen.getByTestId("keys-mint-submit"));

    const once = await screen.findByTestId("keys-once");
    expect(once.textContent).toContain("wdk-test-");
    // 明文只出现一次的提示在案
    expect(once.textContent).toContain("仅此一次");

    const row = await screen.findByTestId(/^keys-row-/);
    expect(row.textContent).toContain("订单系统");
    expect(row.textContent).toContain("wdk-fp");
    expect(row.textContent).toContain("在役");
    // 铸造载荷带标签
    const mintCall = backend.calls.find(
      (c) => c.url.pathname === "/api/v1/data-plane-keys" && c.method === "POST",
    );
    expect(mintCall?.body).toMatchObject({ label: "订单系统" });
  });

  it("allowlist：选中数据集芯片后铸造载荷带 dataset_ids", async () => {
    const backend = installConsoleBackend([], {});
    backend.addDataset({
      id: "ds-1",
      key: "orders",
      name: "订单表",
      source_kind: "declared",
      connector_id: null,
      source_ref: null,
      columns: [{ name: "order_no", type: "TEXT" }],
      row_count: 0,
      size_bytes: 0,
      sync_cron: null,
      sync_status: "ok",
      paused_reason: null,
      created_at: "2026-10-10T00:00:00Z",
      updated_at: "2026-10-10T00:00:00Z",
    });
    vi.stubGlobal("fetch", backend.fetchImpl);
    render(<KeysPage />);

    await screen.findByTestId("keys-table");
    const chip = await screen.findByTestId("keys-allow-orders");
    expect(chip.textContent).toContain("订单表");
    fireEvent.click(chip);

    fireEvent.change(screen.getByTestId("keys-mint-label"), {
      target: { value: "scoped-app" },
    });
    fireEvent.click(screen.getByTestId("keys-mint-submit"));

    await screen.findByTestId("keys-once");
    const mints = backend.calls.filter(
      (c) => c.url.pathname === "/api/v1/data-plane-keys" && c.method === "POST",
    );
    expect(mints.length).toBeGreaterThan(0);
    const ids = (mints.at(-1)?.body as { dataset_ids?: string[] })
      ?.dataset_ids;
    expect(Array.isArray(ids) && ids.length).toBe(1);
  });

  it("吊销：状态翻转为已吊销、吊销按钮退场", async () => {
    install();
    render(<KeysPage />);

    fireEvent.change(await screen.findByTestId("keys-mint-label"), {
      target: { value: "to-revoke" },
    });
    fireEvent.click(screen.getByTestId("keys-mint-submit"));
    const revokeBtn = await screen.findByTestId(/^keys-revoke-/);
    fireEvent.click(revokeBtn);

    await waitFor(() => {
      const row = screen.getByTestId(/^keys-row-/);
      expect(row.textContent).toContain("已吊销");
      expect(
        screen.queryByTestId(/^keys-revoke-/),
      ).toBeNull();
    });
  });

  it("空态与持钥纪律提示在案", async () => {
    install();
    render(<KeysPage />);
    expect(await screen.findByTestId("keys-empty")).toBeTruthy();
    expect(document.body.textContent).toContain("持钥纪律");
  });
});
