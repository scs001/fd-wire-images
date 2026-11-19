// 质量规则区块测试（wire-quality-rules / v2）：规则列表 + 每条规则最新结果徽章
// （pass/fail）、空态、错误态（fetch 500 + 重试）、canEdit=false 只读档
// （无添加/启停/删除/立即检查/改名入口）、添加流程（POST 契约载荷 + 列表
// 刷新 + 表单复位）、添加 422 后端 detail 如实、立即检查（POST run → 返回
// 结果就地刷新）、删除（window.confirm 用 removeConfirm 文案；取消不发）、
// 启停开关 PATCH、行内改名 save/cancel、snapshot 通道结果显示基准时刻。
// v2 扩充：metric 下拉两代 optgroup 分组（汇总 6 + 内容 3）、内容代三 kind
// （regex/col_compare/range）条件表单字段与六符 op、提交前轻量校验
// （同列/区间倒序/pattern 空）、内容代 POST 载荷形状、内容代检查定义摘要
// 渲染、内容代结果行「未达标 N 行」而非 observed vs threshold 假数字。
// fetch 为本文件内置契约假后端（零网络，不改 testing.ts——DatasetDetail 手法）；
// 新 i18n 键译文未落地前 t() 回落键名本身，断言只锚 testid/data-* 与语言
// 中立内容（检查表达式、数值），不锚文案。
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
import {
  DatasetQualityRulesSection,
  QUALITY_RULE_CONTENT_METRICS,
  QUALITY_RULE_METRICS,
  QUALITY_RULE_OPS_WITH_NE,
  describeQualityCheck,
  isContentCheck,
  latestResultFor,
  metricNeedsColumn,
  validateContentCheckForm,
  type QualityRule,
  type QualityRuleCheck,
  type QualityRuleResult,
} from "./DatasetQualityRulesSection";
import { seedConsoleSession } from "./testing";

const RULES_PATH = "/api/v1/datasets/ds-1/quality-rules";
const RUN_PATH = `${RULES_PATH}/run`;
const RESULTS_PATH = `${RULES_PATH}/results`;

function makeCheck(partial: Record<string, unknown> = {}): QualityRuleCheck {
  return {
    metric: "null_rate",
    column: "age",
    op: "<=",
    threshold: 0.05,
    ...partial,
  } as QualityRuleCheck;
}

/** 内容代（v2）检查体构造：regex/col_compare/range 三 kind。 */
function makeContentCheck(
  partial: Record<string, unknown> & {
    metric: "regex" | "col_compare" | "range";
  },
): QualityRuleCheck {
  return { ...partial } as QualityRuleCheck;
}

function makeRule(partial: Partial<QualityRule> = {}): QualityRule {
  return {
    id: "r1",
    dataset_id: "ds-1",
    name: null,
    enabled: true,
    check: makeCheck(),
    updated_by: "demo-customer",
    updated_at: "2026-10-12T09:00:00",
    ...partial,
  };
}

function makeResult(
  partial: Partial<QualityRuleResult> = {},
): QualityRuleResult {
  return {
    id: 101,
    rule_id: "r1",
    status: "pass",
    element_count: 1000,
    unexpected_count: 0,
    pass_rate: 1,
    observed: 0.012,
    threshold: null,
    channel: "query",
    basis_at: null,
    error_text: null,
    created_at: "2026-10-12T10:00:00",
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

/** 契约假后端：规则 CRUD（内存维护）+ run（按启用规则追加结果，可注入
 *  每规则状态）+ results 列表（最近在前，limit 生效）。 */
function installBackend(options: {
  rules?: QualityRule[];
  results?: QualityRuleResult[];
  note?: string;
  /** 强制 GET 规则列表返回该状态（错误态测试）。 */
  failRulesGetStatus?: number;
  /** 强制 POST 新增规则返回该状态（422 detail 回显测试）。 */
  failAddStatus?: number;
  failAddDetail?: string;
  /** run 时按规则注入结果状态（缺省 pass）。 */
  runStatuses?: Record<string, QualityRuleResult["status"]>;
} = {}) {
  const rules = [...(options.rules ?? [])];
  let results = [...(options.results ?? [])];
  let nextRuleId = 1;
  let nextResultId =
    results.reduce((max, row) => Math.max(max, row.id), 0) + 1;
  const calls: FetchCall[] = [];

  const fetchImpl = ((_input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(_input), "http://console.test");
    const method = (init?.method ?? "GET").toUpperCase();
    const headers: Record<string, string> = {};
    if (init?.headers) {
      new Headers(init.headers as HeadersInit).forEach((value, key) => {
        headers[key.toLowerCase()] = value;
      });
    }
    calls.push({
      method,
      url,
      headers,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    });

    const path = url.pathname;
    if (path === RULES_PATH && method === "GET") {
      if (options.failRulesGetStatus) {
        return jsonResponse(
          { detail: `quality rules ${options.failRulesGetStatus}` },
          options.failRulesGetStatus,
        );
      }
      return jsonResponse({ items: rules, note: options.note ?? "" });
    }
    if (path === RULES_PATH && method === "POST") {
      if (options.failAddStatus) {
        return jsonResponse(
          { detail: options.failAddDetail ?? `add ${options.failAddStatus}` },
          options.failAddStatus,
        );
      }
      const payload = calls[calls.length - 1]!.body as {
        check: QualityRuleCheck;
      };
      const rule: QualityRule = {
        id: `rule-${nextRuleId++}`,
        dataset_id: "ds-1",
        name: null,
        enabled: true,
        check: payload.check,
        updated_by: "demo-customer",
        updated_at: "2026-10-13T10:00:00",
      };
      rules.push(rule);
      return jsonResponse(rule, 201);
    }
    const ruleMatch = path.match(/^\/api\/v1\/datasets\/ds-1\/quality-rules\/([^/]+)$/);
    if (ruleMatch && (method === "PATCH" || method === "DELETE")) {
      const ruleId = ruleMatch[1]!;
      const index = rules.findIndex((row) => row.id === ruleId);
      if (index < 0) return jsonResponse({ detail: "no such rule" }, 404);
      if (method === "DELETE") {
        rules.splice(index, 1);
        return jsonResponse(null, 204);
      }
      const payload = (calls[calls.length - 1]!.body ?? {}) as Partial<QualityRule>;
      rules[index] = { ...rules[index]!, ...payload };
      return jsonResponse(rules[index]);
    }
    if (path === RUN_PATH && method === "POST") {
      const ranAt = "2026-10-13T11:00:00";
      for (const rule of rules) {
        if (!rule.enabled) continue;
        const status = options.runStatuses?.[rule.id] ?? "pass";
        const content = isContentCheck(rule.check);
        // 内容代（v2）契约：channel=pandera、observed=未达标行数、
        // threshold=检查定义原文（design D5b）；计数代 observed=实测阈值。
        const observed = isContentCheck(rule.check)
          ? status === "fail"
            ? 3
            : 0
          : rule.check.threshold;
        results = [
          makeResult({
            id: nextResultId++,
            rule_id: rule.id,
            status,
            element_count: 120,
            unexpected_count: status === "fail" ? 3 : 0,
            pass_rate: content ? 0.975 : status === "fail" ? 0.9 : 1,
            observed,
            // 后端契约：threshold_json = 检查定义原文（两代同形）。
            threshold: { ...rule.check },
            channel: content ? "pandera" : "query",
            error_text: status === "error" ? "engine timeout" : null,
            created_at: ranAt,
          }),
          ...results,
        ];
      }
      return jsonResponse({ results, ranAt }, 201);
    }
    if (path === RESULTS_PATH && method === "GET") {
      const limit = Number(url.searchParams.get("limit") ?? "20");
      return jsonResponse({ items: results.slice(0, limit) });
    }
    return jsonResponse({ detail: "not found" }, 404);
  }) as unknown as typeof fetch;
  vi.stubGlobal("fetch", fetchImpl);
  return { calls, getRules: () => rules, getResults: () => results };
}

function callsOf(calls: FetchCall[], method: string, path: string): FetchCall[] {
  return calls.filter((call) => call.method === method && call.url.pathname === path);
}

beforeEach(() => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  // 登录会话种子：请求自动附 Authorization: Bearer（假后端不校验值）。
  seedConsoleSession();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

describe("DatasetQualityRulesSection 渲染与三态（wire-quality-rules）", () => {
  it("两条规则 + 各自最新结果徽章（r1 两条结果取最新 pass；r2 fail），请求带 Bearer", async () => {
    const backend = installBackend({
      rules: [
        makeRule({ id: "r1", check: makeCheck() }),
        makeRule({
          id: "r2",
          check: makeCheck({ metric: "row_count", column: undefined, op: ">=", threshold: 100 }),
        }),
      ],
      results: [
        makeResult({ id: 102, rule_id: "r1", status: "pass", created_at: "2026-10-12T12:00:00" }),
        makeResult({ id: 101, rule_id: "r1", status: "fail", created_at: "2026-10-12T10:00:00" }),
        makeResult({ id: 100, rule_id: "r2", status: "fail", unexpected_count: 5 }),
      ],
    });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);

    // 加载态先行（契约 testid 在场），随后规则落位
    expect(screen.getByTestId("quality-rules-loading")).toBeTruthy();
    const item1 = await screen.findByTestId("quality-rule-item-r1");
    expect(item1.textContent).toContain("null_rate(age) <= 0.05");
    expect(item1.getAttribute("data-enabled")).toBe("true");
    expect(screen.getByTestId("quality-rule-item-r2").textContent).toContain(
      "row_count >= 100",
    );
    // 徽章取最新一条：r1（pass 覆盖更早的 fail）、r2（唯一一条 fail）
    expect(
      screen.getByTestId("quality-rule-result-badge-r1").getAttribute("data-status"),
    ).toBe("pass");
    expect(
      screen.getByTestId("quality-rule-result-badge-r2").getAttribute("data-status"),
    ).toBe("fail");
    // 结果列表在场（最近在前）
    const list = screen.getByTestId("quality-rules-results");
    expect(
      within(list)
        .getAllByTestId("quality-rule-result-row")
        .map((row) => row.getAttribute("data-rule-id")),
    ).toEqual(["r1", "r1", "r2"]);
    // 全部请求自动附 Bearer
    expect(backend.calls.length).toBeGreaterThan(0);
    expect(
      backend.calls.every((call) =>
        (call.headers["authorization"] ?? "").startsWith("Bearer "),
      ),
    ).toBe(true);
  });

  it("空态：无规则 → empty 提示；无结果 → results-empty（无结果列表）", async () => {
    installBackend({ rules: [], results: [] });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);

    expect(await screen.findByTestId("quality-rules-empty")).toBeTruthy();
    expect(screen.getByTestId("quality-rules-results-empty")).toBeTruthy();
    expect(screen.queryByTestId("quality-rules-results")).toBeNull();
    // 添加表单在场（canEdit）
    expect(screen.getByTestId("quality-rule-add")).toBeTruthy();
  });

  it("错误态：GET 500 → 错误面 + 重试再拉；无加载残留", async () => {
    const backend = installBackend({ failRulesGetStatus: 500 });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);

    expect(await screen.findByTestId("quality-rules-error")).toBeTruthy();
    expect(screen.queryByTestId("quality-rules-loading")).toBeNull();
    expect(screen.queryByTestId("quality-rule-add")).toBeNull();

    fireEvent.click(screen.getByTestId("quality-rules-retry"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "GET", RULES_PATH).length).toBe(2),
    );
  });

  it("canEdit=false：只读——无添加/启停/删除/立即检查/改名入口，规则与徽章仍呈现", async () => {
    installBackend({
      rules: [makeRule({ id: "r1" })],
      results: [makeResult({ id: 101, rule_id: "r1" })],
    });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit={false} />);

    await screen.findByTestId("quality-rule-item-r1");
    expect(screen.queryByTestId("quality-rule-add")).toBeNull();
    expect(screen.queryByTestId("quality-rule-metric")).toBeNull();
    expect(screen.queryByTestId("quality-rules-run")).toBeNull();
    expect(screen.queryByTestId("quality-rule-toggle-r1")).toBeNull();
    expect(screen.queryByTestId("quality-rule-delete-r1")).toBeNull();
    expect(screen.queryByTestId("quality-rule-rename-r1")).toBeNull();
    // 只读展示仍在：检查表达式 + 最新徽章 + 结果列表
    expect(screen.getByTestId("quality-rule-item-r1").textContent).toContain(
      "null_rate(age) <= 0.05",
    );
    expect(
      screen.getByTestId("quality-rule-result-badge-r1").getAttribute("data-status"),
    ).toBe("pass");
    expect(screen.getByTestId("quality-rules-results")).toBeTruthy();
  });
});

describe("DatasetQualityRulesSection 添加与删除（wire-quality-rules）", () => {
  it("添加流程：metric 选 null_rate → column 框出现；POST 契约载荷 → 列表刷新、表单复位", async () => {
    const backend = installBackend({ rules: [], results: [] });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);
    await screen.findByTestId("quality-rules-empty");

    // row_count 默认无 column 框；切 null_rate 后出现
    expect(screen.queryByTestId("quality-rule-column")).toBeNull();
    fireEvent.change(screen.getByTestId("quality-rule-metric"), {
      target: { value: "null_rate" },
    });
    fireEvent.change(screen.getByTestId("quality-rule-column"), {
      target: { value: "age" },
    });
    fireEvent.change(screen.getByTestId("quality-rule-op"), {
      target: { value: "<=" },
    });
    fireEvent.change(screen.getByTestId("quality-rule-threshold"), {
      target: { value: "0.05" },
    });
    fireEvent.click(screen.getByTestId("quality-rule-add"));

    await waitFor(() =>
      expect(callsOf(backend.calls, "POST", RULES_PATH).length).toBe(1),
    );
    const post = callsOf(backend.calls, "POST", RULES_PATH)[0]!;
    expect(post.body).toEqual({
      check: { metric: "null_rate", column: "age", op: "<=", threshold: 0.05 },
    });
    expect(post.headers["authorization"]).toMatch(/^Bearer /);
    // 列表刷新：新规则出现（假后端分配 id=rule-1）
    const item = await screen.findByTestId("quality-rule-item-rule-1");
    expect(item.textContent).toContain("null_rate(age) <= 0.05");
    expect(screen.queryByTestId("quality-rules-empty")).toBeNull();
    // 表单复位（threshold 清空、metric 回 row_count → column 框收起）
    expect(
      (screen.getByTestId("quality-rule-threshold") as HTMLInputElement).value,
    ).toBe("");
    expect(screen.queryByTestId("quality-rule-column")).toBeNull();
  });

  it("value_violations：allowed 框逗号拆数组随载荷上行；422 后端 detail 原样呈现", async () => {
    const backend = installBackend({
      failAddStatus: 422,
      failAddDetail: "允许值个数须在 2-32 之间",
    });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);
    await screen.findByTestId("quality-rules-empty");

    expect(screen.queryByTestId("quality-rule-allowed")).toBeNull();
    fireEvent.change(screen.getByTestId("quality-rule-metric"), {
      target: { value: "value_violations" },
    });
    fireEvent.change(screen.getByTestId("quality-rule-column"), {
      target: { value: "city" },
    });
    fireEvent.change(screen.getByTestId("quality-rule-threshold"), {
      target: { value: "0" },
    });
    fireEvent.change(screen.getByTestId("quality-rule-allowed"), {
      target: { value: "北京, 上海，杭州" },
    });
    fireEvent.click(screen.getByTestId("quality-rule-add"));

    await waitFor(() =>
      expect(callsOf(backend.calls, "POST", RULES_PATH).length).toBe(1),
    );
    // 载荷：allowed 逗号/中文逗号拆分 + 去空白；op 默认 >=
    expect(callsOf(backend.calls, "POST", RULES_PATH)[0]!.body).toEqual({
      check: {
        metric: "value_violations",
        column: "city",
        op: ">=",
        threshold: 0,
        allowed: ["北京", "上海", "杭州"],
      },
    });
    // 422 detail 如实回显（apiErrorText 语义）
    const error = await screen.findByTestId("quality-rules-action-error");
    expect(error.textContent).toContain("允许值个数须在 2-32 之间");
    // 列表仍空（未误入新规则），按钮复位可重试
    expect(screen.getByTestId("quality-rules-empty")).toBeTruthy();
    expect(screen.getByTestId("quality-rule-add").hasAttribute("disabled")).toBe(
      false,
    );
  });

  it("删除：confirm 用 removeConfirm 文案——确认后 DELETE → 列表刷新；取消不发", async () => {
    const confirmMock = vi.fn(() => true);
    vi.stubGlobal("confirm", confirmMock);
    const backend = installBackend({ rules: [makeRule({ id: "r1" })], results: [] });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);
    await screen.findByTestId("quality-rule-item-r1");

    // 取消：不发请求、规则保留
    vi.stubGlobal("confirm", vi.fn(() => false));
    fireEvent.click(screen.getByTestId("quality-rule-delete-r1"));
    expect(callsOf(backend.calls, "DELETE", `${RULES_PATH}/r1`).length).toBe(0);
    expect(screen.getByTestId("quality-rule-item-r1")).toBeTruthy();

    // 确认：confirm 收到 removeConfirm 文案
    vi.stubGlobal("confirm", confirmMock);
    fireEvent.click(screen.getByTestId("quality-rule-delete-r1"));
    expect(confirmMock).toHaveBeenCalledWith(
      "确定删除这条质量规则？（历史结果将保留）",
    );
    await waitFor(() =>
      expect(callsOf(backend.calls, "DELETE", `${RULES_PATH}/r1`).length).toBe(1),
    );
    expect(
      callsOf(backend.calls, "DELETE", `${RULES_PATH}/r1`)[0]!.headers[
        "authorization"
      ],
    ).toMatch(/^Bearer /);
    await waitFor(() =>
      expect(screen.queryByTestId("quality-rule-item-r1")).toBeNull(),
    );
    expect(screen.getByTestId("quality-rules-empty")).toBeTruthy();
  });
});

describe("DatasetQualityRulesSection 运行与启停/改名（wire-quality-rules）", () => {
  it("立即检查：POST run（Bearer）→ 用返回结果就地刷新（空态消失、徽章更新）", async () => {
    const backend = installBackend({ rules: [makeRule({ id: "r1" })], results: [] });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);
    await screen.findByTestId("quality-rule-item-r1");
    expect(screen.getByTestId("quality-rules-results-empty")).toBeTruthy();

    fireEvent.click(screen.getByTestId("quality-rules-run"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "POST", RUN_PATH).length).toBe(1),
    );
    expect(
      callsOf(backend.calls, "POST", RUN_PATH)[0]!.headers["authorization"],
    ).toMatch(/^Bearer /);
    // 结果就地刷新：新结果行出现、空态消失；徽章同步为 pass
    await screen.findByTestId("quality-rules-results");
    expect(screen.queryByTestId("quality-rules-results-empty")).toBeNull();
    expect(
      screen.getByTestId("quality-rule-result-badge-r1").getAttribute("data-status"),
    ).toBe("pass");
    await waitFor(() =>
      expect(screen.getByTestId("quality-rules-run").hasAttribute("disabled")).toBe(
        false,
      ),
    );
  });

  it("启停开关：点击 → PATCH {enabled:false} → 行态翻转、按钮文案切 disabled", async () => {
    const backend = installBackend({ rules: [makeRule({ id: "r1" })], results: [] });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);
    await screen.findByTestId("quality-rule-item-r1");

    fireEvent.click(screen.getByTestId("quality-rule-toggle-r1"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "PATCH", `${RULES_PATH}/r1`).length).toBe(1),
    );
    expect(
      callsOf(backend.calls, "PATCH", `${RULES_PATH}/r1`)[0]!.body,
    ).toEqual({ enabled: false });
    await waitFor(() =>
      expect(
        screen.getByTestId("quality-rule-item-r1").getAttribute("data-enabled"),
      ).toBe("false"),
    );
    expect(
      screen.getByTestId("quality-rule-toggle-r1").textContent,
    ).toBe("已停用");
  });

  it("行内改名：save → PATCH {name} 生效；cancel 不发请求", async () => {
    const backend = installBackend({ rules: [makeRule({ id: "r1" })], results: [] });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);
    await screen.findByTestId("quality-rule-item-r1");

    // cancel：退出编辑、零 PATCH
    fireEvent.click(screen.getByTestId("quality-rule-rename-r1"));
    fireEvent.change(screen.getByTestId("quality-rule-name-input"), {
      target: { value: "年龄空值率" },
    });
    fireEvent.click(screen.getByTestId("quality-rule-name-cancel"));
    expect(screen.queryByTestId("quality-rule-name-input")).toBeNull();
    expect(callsOf(backend.calls, "PATCH", `${RULES_PATH}/r1`).length).toBe(0);

    // save：PATCH {name} → 行内呈现新名（检查表达式仍并列在场）
    fireEvent.click(screen.getByTestId("quality-rule-rename-r1"));
    fireEvent.change(screen.getByTestId("quality-rule-name-input"), {
      target: { value: "年龄空值率" },
    });
    fireEvent.click(screen.getByTestId("quality-rule-name-save"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "PATCH", `${RULES_PATH}/r1`).length).toBe(1),
    );
    expect(
      callsOf(backend.calls, "PATCH", `${RULES_PATH}/r1`)[0]!.body,
    ).toEqual({ name: "年龄空值率" });
    await waitFor(() =>
      expect(screen.getByTestId("quality-rule-item-r1").textContent).toContain(
        "年龄空值率",
      ),
    );
    expect(screen.getByTestId("quality-rule-item-r1").textContent).toContain(
      "null_rate(age) <= 0.05",
    );
  });
});

describe("DatasetQualityRulesSection 结果呈现（wire-quality-rules）", () => {
  it("snapshot 通道结果显示基准时刻（basisAt）；query 通道不显示", async () => {
    installBackend({
      rules: [
        makeRule({ id: "r1", check: makeCheck() }),
        makeRule({
          id: "r2",
          check: makeCheck({ metric: "row_count", column: undefined, op: ">=", threshold: 10 }),
        }),
      ],
      results: [
        makeResult({
          id: 102,
          rule_id: "r2",
          observed: 12,
          channel: "snapshot",
          basis_at: "2026-10-12T08:00:00",
        }),
        makeResult({ id: 101, rule_id: "r1", channel: "query" }),
      ],
    });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);

    const list = await screen.findByTestId("quality-rules-results");
    const rows = within(list).getAllByTestId("quality-rule-result-row");
    const snapshotRow = rows.find(
      (row) => row.getAttribute("data-rule-id") === "r2",
    )!;
    const queryRow = rows.find(
      (row) => row.getAttribute("data-rule-id") === "r1",
    )!;

    // snapshot：基准时刻在场（本地化含年份），observed vs threshold 并列
    const basis = within(snapshotRow).getByTestId("quality-rule-result-basis");
    expect(basis.textContent).toContain("2026");
    expect(snapshotRow.textContent).toContain("12");
    expect(snapshotRow.textContent).toContain("10");
    // query：无基准时刻
    expect(within(queryRow).queryByTestId("quality-rule-result-basis")).toBeNull();
  });

  it("error 态结果：徽章与结果行均为 error，error_text 摘要呈现", async () => {
    installBackend({
      rules: [makeRule({ id: "r1" })],
      results: [
        makeResult({
          id: 101,
          rule_id: "r1",
          status: "error",
          observed: null,
          element_count: null,
          unexpected_count: null,
          pass_rate: null,
          error_text: "engine timeout: quality tool exceeded 30s budget",
        }),
      ],
    });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);

    await screen.findByTestId("quality-rule-item-r1");
    const badge = screen.getByTestId("quality-rule-result-badge-r1");
    expect(badge.getAttribute("data-status")).toBe("error");
    expect(badge.textContent).toContain("engine timeout");
    const row = within(screen.getByTestId("quality-rules-results"))
      .getAllByTestId("quality-rule-result-row")[0]!;
    expect(row.getAttribute("data-status")).toBe("error");
    expect(row.textContent).toContain("engine timeout");
  });
});

describe("纯逻辑：describeQualityCheck / latestResultFor / metricNeedsColumn", () => {
  it("describeQualityCheck：带列/不带列/value_violations 允许值清单", () => {
    expect(
      describeQualityCheck({ metric: "null_rate", column: "age", op: "<=", threshold: 0.05 }),
    ).toBe("null_rate(age) <= 0.05");
    expect(
      describeQualityCheck({ metric: "row_count", op: ">=", threshold: 100 }),
    ).toBe("row_count >= 100");
    expect(
      describeQualityCheck({
        metric: "value_violations",
        column: "city",
        op: ">",
        threshold: 0,
        allowed: ["北京", "上海"],
      }),
    ).toBe("value_violations(city) > 0 ∈ {北京, 上海}");
  });

  it("latestResultFor：同规则多条取 id 最大（最新）；无结果返回 null", () => {
    const results = [
      makeResult({ id: 1, rule_id: "r1", status: "fail" }),
      makeResult({ id: 2, rule_id: "r1", status: "pass" }),
      makeResult({ id: 3, rule_id: "r2", status: "fail" }),
    ];
    expect(latestResultFor(results, "r1")?.id).toBe(2);
    expect(latestResultFor(results, "r2")?.id).toBe(3);
    expect(latestResultFor(results, "r3")).toBeNull();
  });

  it("metricNeedsColumn：row_count/key_uniqueness 全表口径免列，其余必填", () => {
    expect(metricNeedsColumn("row_count")).toBe(false);
    expect(metricNeedsColumn("key_uniqueness")).toBe(false);
    expect(metricNeedsColumn("null_count")).toBe(true);
    expect(metricNeedsColumn("null_rate")).toBe(true);
    expect(metricNeedsColumn("filled_rate")).toBe(true);
    expect(metricNeedsColumn("value_violations")).toBe(true);
    // v2：col_compare 用 col_a/col_b 两列，无单列 column 字段
    expect(metricNeedsColumn("col_compare")).toBe(false);
    expect(metricNeedsColumn("regex")).toBe(true);
    expect(metricNeedsColumn("range")).toBe(true);
  });

  it("describeQualityCheck 内容代（v2）：regex/col_compare/range 摘要，不拼假阈值", () => {
    expect(
      describeQualityCheck(
        makeContentCheck({ metric: "regex", column: "code", pattern: "^\\d{3}$" }),
      ),
    ).toBe("regex(code): ^\\d{3}$");
    expect(
      describeQualityCheck(
        makeContentCheck({
          metric: "regex",
          column: "code",
          pattern: "^[A-Z]{2}\\d{4}$",
        }),
      ),
    ).toBe("regex(code): ^[A-Z]{2}\\d{4}$");
    expect(
      describeQualityCheck(
        makeContentCheck({
          metric: "col_compare",
          col_a: "end",
          op: ">=",
          col_b: "start",
        }),
      ),
    ).toBe("end >= start");
    expect(
      describeQualityCheck(
        makeContentCheck({
          metric: "col_compare",
          col_a: "adjust",
          op: "!=",
          col_b: "amount",
        }),
      ),
    ).toBe("adjust != amount");
    expect(
      describeQualityCheck(
        makeContentCheck({ metric: "range", column: "age", min: 20, max: 40 }),
      ),
    ).toBe("20 ≤ age ≤ 40");
  });

  it("isContentCheck：内容代三 kind 为真，计数代六项为假", () => {
    for (const metric of QUALITY_RULE_CONTENT_METRICS) {
      expect(
        isContentCheck(
          makeContentCheck({ metric } as Partial<QualityRuleCheck> & {
            metric: "regex" | "col_compare" | "range";
          }),
        ),
      ).toBe(true);
    }
    for (const metric of QUALITY_RULE_METRICS) {
      const check =
        metric === "row_count" || metric === "key_uniqueness"
          ? { metric, op: ">=" as const, threshold: 1 }
          : { metric, column: "c", op: ">=" as const, threshold: 1 };
      expect(isContentCheck(check as QualityRuleCheck)).toBe(false);
    }
    // 六符常量：含 != 且五项旧符全在（v2 col_compare 专用）
    expect([...QUALITY_RULE_OPS_WITH_NE]).toEqual([
      "<",
      "<=",
      "=",
      "!=",
      ">=",
      ">",
    ]);
  });

  it("validateContentCheckForm：同列/区间倒序/pattern 空 → 错误码；正常 → null", () => {
    const base = {
      metric: "regex" as const,
      column: "code",
      pattern: "^\\d{3}$",
      colA: "",
      colB: "",
      minText: "",
      maxText: "",
    };
    expect(validateContentCheckForm(base)).toBeNull();
    expect(validateContentCheckForm({ ...base, pattern: "   " })).toBe(
      "patternRequired",
    );
    expect(
      validateContentCheckForm({
        ...base,
        metric: "col_compare",
        colA: "end",
        colB: "end",
      }),
    ).toBe("sameColumn");
    expect(
      validateContentCheckForm({
        ...base,
        metric: "col_compare",
        colA: "end",
        colB: " start ",
      }),
    ).toBeNull();
    expect(
      validateContentCheckForm({
        ...base,
        metric: "range",
        minText: "50",
        maxText: "10",
      }),
    ).toBe("rangeOrder");
    expect(
      validateContentCheckForm({
        ...base,
        metric: "range",
        minText: "10",
        maxText: "50",
      }),
    ).toBeNull();
  });
});

// ── v2：检查项目录扩面（optgroup 分组 + 内容代条件表单）──────────────────
describe("DatasetQualityRulesSection v2 检查目录与表单（wire-quality-rules-v2）", () => {
  it("metric 下拉两级 optgroup：汇总检查 6 项 + 内容检查 3 项（共 9），内容项带慢提示", async () => {
    installBackend({ rules: [], results: [] });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);
    await screen.findByTestId("quality-rules-empty");

    const select = screen.getByTestId("quality-rule-metric");
    const options = select.querySelectorAll("option");
    expect(options.length).toBe(9);
    expect([...options].map((option) => option.value)).toEqual([
      "row_count",
      "null_count",
      "null_rate",
      "filled_rate",
      "value_violations",
      "key_uniqueness",
      "regex",
      "col_compare",
      "range",
    ]);
    // 分组：两个 optgroup，各 6 / 3 项
    const groups = select.querySelectorAll("optgroup");
    expect(groups.length).toBe(2);
    expect(groups[0]!.querySelectorAll("option").length).toBe(6);
    expect(groups[1]!.querySelectorAll("option").length).toBe(3);
    // 分组标签走 i18n 键（译文未落地时回落键名本身）
    expect(groups[0]!.getAttribute("label")).toBe(
      "汇总检查",
    );
    expect(groups[1]!.getAttribute("label")).toBe(
      "内容检查",
    );
    // 内容代三选项的中文名 + 「逐行执行，较慢」提示（option 文本后缀；
    // 键未落地时 t() 回落键名——断言以 i18n.t 活取，不硬编码文案）
    expect(
      [...groups[1]!.querySelectorAll("option")].map((o) => o.textContent),
    ).toEqual(
      ["regex", "col_compare", "range"].map(
        (metric) =>
          `${i18n.t(`console.dataset.qualityRules.metric.${metric}`)} · ${i18n.t(
            "console.dataset.qualityRules.contentSlowHint",
          )}`,
      ),
    );
  });

  it("选 regex：显示 column + pattern，隐藏 op/threshold；pattern 空提交被阻止", async () => {
    const backend = installBackend({ rules: [], results: [] });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);
    await screen.findByTestId("quality-rules-empty");

    fireEvent.change(screen.getByTestId("quality-rule-metric"), {
      target: { value: "regex" },
    });
    // 内容代表单字段集：column + pattern；op/threshold 不在场
    expect(screen.getByTestId("quality-rule-column")).toBeTruthy();
    expect(screen.getByTestId("quality-rule-pattern")).toBeTruthy();
    expect(screen.queryByTestId("quality-rule-op")).toBeNull();
    expect(screen.queryByTestId("quality-rule-threshold")).toBeNull();
    expect(screen.queryByTestId("quality-rule-allowed")).toBeNull();

    // column 填了、pattern 空 → 提交被阻止 + 明确错误（不发网络）
    fireEvent.change(screen.getByTestId("quality-rule-column"), {
      target: { value: "code" },
    });
    fireEvent.click(screen.getByTestId("quality-rule-add"));
    expect(
      screen.getByTestId("quality-rule-form-error").textContent,
    ).toContain("请填写正则模式");
    expect(callsOf(backend.calls, "POST", RULES_PATH).length).toBe(0);
  });

  it("选 col_compare：col_a + 六符 op（含 !=）+ col_b；col_a===col_b 被阻止", async () => {
    const backend = installBackend({ rules: [], results: [] });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);
    await screen.findByTestId("quality-rules-empty");

    fireEvent.change(screen.getByTestId("quality-rule-metric"), {
      target: { value: "col_compare" },
    });
    expect(screen.getByTestId("quality-rule-col-a")).toBeTruthy();
    expect(screen.getByTestId("quality-rule-col-b")).toBeTruthy();
    // 单列 column 框不在场（col_compare 用两列）
    expect(screen.queryByTestId("quality-rule-column")).toBeNull();
    expect(screen.queryByTestId("quality-rule-threshold")).toBeNull();
    const opSelect = screen.getByTestId("quality-rule-compare-op");
    const opValues = [...opSelect.querySelectorAll("option")].map(
      (option) => option.value,
    );
    expect(opValues).toEqual(["<", "<=", "=", "!=", ">=", ">"]);

    // 两列同名 → 提交被阻止 + sameColumnError（不发网络）
    fireEvent.change(screen.getByTestId("quality-rule-col-a"), {
      target: { value: "end" },
    });
    fireEvent.change(screen.getByTestId("quality-rule-col-b"), {
      target: { value: "end" },
    });
    fireEvent.click(screen.getByTestId("quality-rule-add"));
    expect(
      screen.getByTestId("quality-rule-form-error").textContent,
    ).toContain("两侧不能是同一列");
    expect(callsOf(backend.calls, "POST", RULES_PATH).length).toBe(0);

    // 改成不同列 + != → 放行（POST 载荷六符语义如实）
    fireEvent.change(screen.getByTestId("quality-rule-col-b"), {
      target: { value: "start" },
    });
    fireEvent.change(screen.getByTestId("quality-rule-compare-op"), {
      target: { value: "!=" },
    });
    fireEvent.click(screen.getByTestId("quality-rule-add"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "POST", RULES_PATH).length).toBe(1),
    );
    expect(callsOf(backend.calls, "POST", RULES_PATH)[0]!.body).toEqual({
      check: { metric: "col_compare", col_a: "end", op: "!=", col_b: "start" },
    });
  });

  it("选 range：column + min/max；min > max 被阻止（rangeOrderError）；正常放行", async () => {
    const backend = installBackend({ rules: [], results: [] });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);
    await screen.findByTestId("quality-rules-empty");

    fireEvent.change(screen.getByTestId("quality-rule-metric"), {
      target: { value: "range" },
    });
    expect(screen.getByTestId("quality-rule-column")).toBeTruthy();
    expect(screen.getByTestId("quality-rule-min")).toBeTruthy();
    expect(screen.getByTestId("quality-rule-max")).toBeTruthy();
    expect(screen.queryByTestId("quality-rule-op")).toBeNull();
    expect(screen.queryByTestId("quality-rule-threshold")).toBeNull();

    fireEvent.change(screen.getByTestId("quality-rule-column"), {
      target: { value: "age" },
    });
    fireEvent.change(screen.getByTestId("quality-rule-min"), {
      target: { value: "50" },
    });
    fireEvent.change(screen.getByTestId("quality-rule-max"), {
      target: { value: "10" },
    });
    fireEvent.click(screen.getByTestId("quality-rule-add"));
    expect(
      screen.getByTestId("quality-rule-form-error").textContent,
    ).toContain("最小值不能大于最大值");
    expect(callsOf(backend.calls, "POST", RULES_PATH).length).toBe(0);

    // 改回 min ≤ max（10 ≤ 50）→ 放行，载荷 min/max 为数字、无 op/threshold
    fireEvent.change(screen.getByTestId("quality-rule-min"), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByTestId("quality-rule-max"), {
      target: { value: "50" },
    });
    fireEvent.click(screen.getByTestId("quality-rule-add"));
    await waitFor(() =>
      expect(callsOf(backend.calls, "POST", RULES_PATH).length).toBe(1),
    );
    expect(callsOf(backend.calls, "POST", RULES_PATH)[0]!.body).toEqual({
      check: { metric: "range", column: "age", min: 10, max: 50 },
    });
    expect(screen.queryByTestId("quality-rule-form-error")).toBeNull();
  });
});

describe("DatasetQualityRulesSection v2 提交载荷与内容代呈现（wire-quality-rules-v2）", () => {
  it("regex 添加流程：POST 载荷 {metric, column, pattern} 形状正确 + 列表刷新", async () => {
    const backend = installBackend({ rules: [], results: [] });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);
    await screen.findByTestId("quality-rules-empty");

    fireEvent.change(screen.getByTestId("quality-rule-metric"), {
      target: { value: "regex" },
    });
    fireEvent.change(screen.getByTestId("quality-rule-column"), {
      target: { value: "code" },
    });
    fireEvent.change(screen.getByTestId("quality-rule-pattern"), {
      target: { value: "^\\d{3}$" },
    });
    fireEvent.click(screen.getByTestId("quality-rule-add"));

    await waitFor(() =>
      expect(callsOf(backend.calls, "POST", RULES_PATH).length).toBe(1),
    );
    // 契约形状：无 op/threshold/allowed，仅 metric/column/pattern
    expect(callsOf(backend.calls, "POST", RULES_PATH)[0]!.body).toEqual({
      check: { metric: "regex", column: "code", pattern: "^\\d{3}$" },
    });
    expect(
      callsOf(backend.calls, "POST", RULES_PATH)[0]!.headers["authorization"],
    ).toMatch(/^Bearer /);
    // 列表刷新：新规则行出现并渲染内容代摘要
    const item = await screen.findByTestId("quality-rule-item-rule-1");
    expect(item.textContent).toContain("regex(code): ^\\d{3}$");
    // 表单复位：metric 回 row_count → 内容代字段收起、计数代字段复出
    expect(screen.queryByTestId("quality-rule-pattern")).toBeNull();
    expect(screen.getByTestId("quality-rule-threshold")).toBeTruthy();
    expect(screen.queryByTestId("quality-rule-form-error")).toBeNull();
  });

  it("内容代规则列表项渲染摘要（regex/col_compare/range 三种）", async () => {
    installBackend({
      rules: [
        makeRule({
          id: "r-regex",
          check: makeContentCheck({
            metric: "regex",
            column: "code",
            pattern: "^\\d{3}$",
          }),
        }),
        makeRule({
          id: "r-compare",
          check: makeContentCheck({
            metric: "col_compare",
            col_a: "end",
            op: ">=",
            col_b: "start",
          }),
        }),
        makeRule({
          id: "r-range",
          check: makeContentCheck({
            metric: "range",
            column: "age",
            min: 20,
            max: 40,
          }),
        }),
      ],
      results: [],
    });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);

    expect(
      (await screen.findByTestId("quality-rule-item-r-regex")).textContent,
    ).toContain("regex(code): ^\\d{3}$");
    expect(
      screen.getByTestId("quality-rule-item-r-compare").textContent,
    ).toContain("end >= start");
    expect(screen.getByTestId("quality-rule-item-r-range").textContent).toContain(
      "20 ≤ age ≤ 40",
    );
  });

  it("内容代结果行：渲染「未达标 N 行」（observed 映射）而非 observed vs threshold 假数字", async () => {
    installBackend({
      rules: [
        makeRule({
          id: "r1",
          check: makeContentCheck({
            metric: "regex",
            column: "code",
            pattern: "^\\d{3}$",
          }),
        }),
        makeRule({
          id: "r2",
          check: makeCheck({ metric: "row_count", column: undefined, op: ">=", threshold: 100 }),
        }),
      ],
      results: [
        makeResult({
          id: 102,
          rule_id: "r1",
          status: "fail",
          observed: 7,
          unexpected_count: 7,
          pass_rate: 0.93,
          threshold: { metric: "regex", column: "code", pattern: "^\\d{3}$" },
          channel: "pandera",
        }),
        makeResult({
          id: 101,
          rule_id: "r2",
          observed: 120,
          unexpected_count: 0,
          pass_rate: 1,
          threshold: { metric: "row_count", op: ">=", threshold: 100 },
          channel: "query",
        }),
      ],
    });
    render(<DatasetQualityRulesSection datasetId="ds-1" canEdit />);

    const rows = within(await screen.findByTestId("quality-rules-results"))
      .getAllByTestId("quality-rule-result-row");
    const contentRow = rows.find(
      (row) => row.getAttribute("data-rule-id") === "r1",
    )!;
    const countRow = rows.find(
      (row) => row.getAttribute("data-rule-id") === "r2",
    )!;

    // 内容代：摘要 + 未达标 N 行 + 达标率；且不出现 observed/threshold 对比
    expect(contentRow.getAttribute("data-channel")).toBe("pandera");
    expect(contentRow.textContent).toContain("regex(code): ^\\d{3}$");
    const contentCount = within(contentRow).getByTestId(
      "quality-rule-result-content-count",
    );
    expect(contentCount.textContent).toContain("7");
    expect(contentRow.textContent).toContain("93.0%");
    expect(
      within(contentRow).queryByTestId("quality-rule-result-observed"),
    ).toBeNull();
    // 内容代不再并列「阈值：100」（100 是另一条规则的假阈值）也不出现 observed 标签
    expect(contentRow.textContent).not.toContain(
      "console.dataset.qualityRules.observed",
    );

    // 计数代照旧：observed vs threshold 数字对比 + 未达标行数独立展示
    const countObserved = within(countRow).getByTestId(
      "quality-rule-result-observed",
    );
    expect(countObserved.textContent).toContain("120");
    expect(countObserved.textContent).toContain("100");
    expect(
      within(countRow).queryByTestId("quality-rule-result-content-count"),
    ).toBeNull();
  });
});
