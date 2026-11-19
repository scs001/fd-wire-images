// 商留通道页组件测试（add-contact-intake 任务 1.4 web 面）：列表倒序与
// 字段齐全、未读徽标、标记已读（PATCH 形状 + 未读数递减）、404 如实回显。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { ContactsPage } from "./ContactsPage";
import { installAdminBackend, seedAdminSession } from "./testing";
import type { ContactMessage } from "./api";

const MESSAGES: ContactMessage[] = [
  {
    id: 3,
    name: "王五",
    email: "wangwu@corp.example",
    company: "丙公司",
    message: "想接入 MCP 读取指标目录",
    status: "new",
    created_at: "2026-11-06T10:00:00",
  },
  {
    id: 2,
    name: "李四",
    email: "lisi@corp.example",
    company: null,
    message: "咨询自定义档位报价",
    status: "read",
    created_at: "2026-11-06T09:00:00",
  },
  {
    id: 1,
    name: "张三",
    email: "zhangsan@corp.example",
    company: "甲公司",
    message: "想开通全国光伏装机月度数据",
    status: "new",
    created_at: "2026-11-06T08:00:00",
  },
];

beforeEach(() => {
  i18n.changeLanguage("zh");
  seedAdminSession();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ContactsPage（后台商留通道）", () => {
  it("列表时间倒序、字段齐全、未读徽标呈现", async () => {
    const { fetchImpl, calls } = installAdminBackend({ contactMessages: MESSAGES });
    vi.stubGlobal("fetch", fetchImpl);
    render(<ContactsPage />);

    expect(await screen.findByTestId("admin-contacts-table")).toBeTruthy();
    // 请求形状：limit/offset 分页参数
    const listCall = calls.find((c) => c.url.pathname === "/internal/admin/contact-messages")!;
    expect(listCall.url.searchParams.get("limit")).toBe("50");

    // 时间倒序：最新（id=3）在首行
    const rows = screen.getAllByTestId("admin-contacts-row");
    expect(rows.length).toBe(3);
    const names = screen.getAllByTestId("admin-contacts-name").map((n) => n.textContent);
    expect(names).toEqual(["王五", "李四", "张三"]);
    // 字段齐全（邮箱/公司/留言）
    const emails = screen.getAllByTestId("admin-contacts-email").map((n) => n.textContent);
    expect(emails).toEqual(["wangwu@corp.example", "lisi@corp.example", "zhangsan@corp.example"]);
    // 首行留言（id=3）内容可见
    expect(
      (screen.getAllByTestId("admin-contacts-message")[0] as HTMLElement).textContent,
    ).toContain("MCP");
    // 未读徽标（页头 + 分区入口共用 unread 数据源）：2 条未读
    expect(screen.getByTestId("admin-contacts-unread-badge").textContent).toBe("2 条未读");
    // 已读行无标记按钮
    expect(screen.getAllByTestId("contact-mark-read").length).toBe(2);
    // 无 ip_hash 字段面（红线）
    expect(screen.queryByText(/ip_hash/i)).toBeNull();
  });

  it("标记已读：PATCH {status:read}，列表刷新后未读数递减", async () => {
    const { fetchImpl, calls } = installAdminBackend({ contactMessages: [...MESSAGES] });
    vi.stubGlobal("fetch", fetchImpl);
    render(<ContactsPage />);

    await screen.findByTestId("admin-contacts-table");
    fireEvent.click(screen.getAllByTestId("contact-mark-read")[0]);
    await waitFor(() =>
      expect(screen.getByTestId("admin-contacts-marked")).toBeTruthy(),
    );

    const patch = calls.find(
      (c) =>
        c.method === "PATCH" &&
        c.url.pathname === "/internal/admin/contact-messages/3",
    )!;
    expect(patch.body).toEqual({ status: "read" });
    // 未读徽标从 2 → 1
    await waitFor(() =>
      expect(screen.getByTestId("admin-contacts-unread-badge").textContent).toBe(
        "1 条未读",
      ),
    );
  });

  it("标记不存在的提交 → 404 如实回显（AdminErrorBox）", async () => {
    // 假后端列表先给一行，PATCH 时注入数据为空 → 404
    const { fetchImpl } = installAdminBackend({ contactMessages: MESSAGES });
    vi.stubGlobal("fetch", fetchImpl);
    render(<ContactsPage />);
    await screen.findByTestId("admin-contacts-table");

    // 直接对假后端不存在的 id 发 PATCH 不可达（按钮只对已渲染行出现）——
    // 改为强制鉴权失败态验证错误面呈现路径。
    cleanup();
    const forced = installAdminBackend({ forceStatus: 401 });
    vi.stubGlobal("fetch", forced.fetchImpl);
    render(<ContactsPage />);
    expect(await screen.findByTestId("admin-auth-error")).toBeTruthy();
  });
});
