// 「本体版本区」组件测试（add-ontology-versioning 任务 5.4）：
// - 时间线渲染：版本号 / digest 前 8 位 / trigger 徽章 / label / 规模计数 /
//   未发布变更徽章（含 since_version）；
// - 空态：无快照时如实文案（不出回滚入口的前提——角色未知时也不出）；
// - diff 面板三组：新增 / 删除 / 字段级 before→after（实体与关系）；
// - 打发布版：成功（201 新版本）与 unchanged=true（如实显示后端 message，
//   不谎称新版本）；
// - 回滚（owner）：plan 预览（summary 计数 + 字段级差异）→ 确认执行 →
//   digest_match 如实；422 对象 detail {message,executed,note} 显示 message
//   原文与已执行步数——绝不出现 [object Object]；
// - 非 owner：回滚入口不渲染（getTeam 403 → 角色未知 → 不可见）；
// - 完整性自检：一致绿 / 不一致红（差异计数 + escape_hint 原文）。
// 假后端形状逐端点对齐 api 仓（真后端 201 对新版本与 unchanged 同码）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { OntologyVersionSection } from "./OntologyVersionSection";
import {
  installConsoleBackend,
  seedConsoleSession,
  type FakeOntologyDiffSeed,
} from "./testing";

beforeEach(async () => {
  i18n.changeLanguage("zh");
  localStorage.clear();
  sessionStorage.clear();
  await seedConsoleSession();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

/** 版本时间线种子（两条：v2 手动版带 label、v1 日版）。 */
const twoVersions = [
  {
    version: 2,
    trigger: "manual" as const,
    label: "冒烟本体定稿",
    created_by: "sub-owner",
    created_at: "2026-11-01T10:00:00",
    entity_count: 2,
    relationship_count: 1,
  },
  {
    version: 1,
    trigger: "daily" as const,
    created_at: "2026-10-31T03:30:00",
    entity_count: 2,
    relationship_count: 0,
  },
];

/** 安装假后端 + stub 全局 fetch（EntitiesPage.test 同款接线；漏 stub 会让
 *  组件打真 fetch 而全数失败）。 */
function setup(options: Parameters<typeof installConsoleBackend>[1] = {}) {
  const backend = installConsoleBackend([], options);
  vi.stubGlobal("fetch", backend.fetchImpl);
  return backend;
}

/** diff 种子：+1 实体、-1 关系、1 条实体字段级改（spec 场景形状）。 */
const diffSeed: FakeOntologyDiffSeed = {
  entities: {
    added: [
      {
        id: 9,
        name: "员工",
        dataset_id: "ds-3",
        pk_columns: ["emp_id"],
        description: null,
        display_column: null,
        column_notes: null,
      },
    ],
    removed: [],
    changed: [
      {
        id: 1,
        name: "订单",
        before: { id: 1, name: "订单", display_column: null },
        after: { id: 1, name: "订单", display_column: "order_id" },
        fields: { display_column: { before: null, after: "order_id" } },
      },
    ],
  },
  relationships: {
    added: [],
    removed: [
      {
        id: 3,
        from_entity_id: 1,
        from_column: "customer_id",
        to_entity_id: 2,
        to_column: "customer_id",
        kind: "n:1",
        declared_kind: null,
        label: null,
        junction_dataset_id: null,
        junction_source_column: null,
        junction_target_column: null,
      },
    ],
    changed: [],
  },
};

describe("OntologyVersionSection", () => {
  it("时间线渲染：版本号/digest 前 8/trigger/label/规模计数/未发布徽章", async () => {
    setup({
      ontologyVersions: twoVersions,
      ontologyUnpublished: 3,
    });
    render(<OntologyVersionSection />);

    await waitFor(() => {
      expect(screen.queryByTestId("ontology-version-loading")).toBeNull();
    });
    const rows = screen.getAllByTestId("ontology-version-row");
    expect(rows).toHaveLength(2);
    // 倒序：首行是 v2
    expect(rows[0].getAttribute("data-version")).toBe("2");

    const v2 = within(rows[0]);
    // digest 前 8 位（fakeOntologyDigest(2) = "00000002" 重复）
    expect(v2.getByTestId("ontology-version-digest").textContent).toBe(
      "00000002",
    );
    expect(v2.getByTestId("ontology-version-trigger").textContent).toBe(
      "发布版",
    );
    expect(v2.getByTestId("ontology-version-trigger").getAttribute("data-trigger")).toBe(
      "manual",
    );
    expect(v2.getByTestId("ontology-version-label").textContent).toBe(
      "冒烟本体定稿",
    );
    expect(v2.getByTestId("ontology-version-counts").textContent).toBe(
      "2 实体 / 1 关系",
    );
    // 次行是 v1 日版（无 label → 不渲染 label 元素）
    const v1 = within(rows[1]);
    expect(v1.getByTestId("ontology-version-trigger").textContent).toBe("日版");
    expect(v1.queryByTestId("ontology-version-label")).toBeNull();

    // 未发布变更徽章（相对 v2）
    const badge = screen.getByTestId("ontology-unpublished-badge");
    expect(badge.textContent).toBe("未发布变更 3 条");
    expect(screen.getByText("相对 v2")).toBeTruthy();
  });

  it("空态：尚无快照时如实文案", async () => {
    setup({});
    render(<OntologyVersionSection />);
    await waitFor(() => {
      expect(screen.getByTestId("ontology-version-empty")).toBeTruthy();
    });
    expect(screen.getByTestId("ontology-version-empty").textContent).toBe(
      "尚无版本快照——首次日快照或手动打发布版后出现。",
    );
    expect(screen.queryByTestId("ontology-version-row")).toBeNull();
  });

  it("diff 三组：新增/删除/字段级 before→after 如实", async () => {
    setup({
      ontologyVersions: twoVersions,
      ontologyDiff: diffSeed,
    });
    render(<OntologyVersionSection />);
    await waitFor(() => {
      expect(screen.getAllByTestId("ontology-version-row").length).toBe(2);
    });

    fireEvent.click(screen.getAllByTestId("ontology-diff-open")[0]);
    const panel = await screen.findByTestId("ontology-diff-panel");
    expect(panel.getAttribute("data-version")).toBe("2");

    const entities = within(screen.getByTestId("ontology-diff-entities"));
    expect(entities.getByTestId("ontology-diff-entities-added").textContent).toContain(
      "新增（1）",
    );
    expect(entities.getByTestId("ontology-diff-entities-added").textContent).toContain(
      "+ 员工 (#9)",
    );
    expect(entities.getByTestId("ontology-diff-entities-changed").textContent).toContain(
      "字段级改动（1）",
    );
    const fieldLine = entities.getAllByTestId(
      "ontology-diff-entities-field",
    )[0];
    expect(fieldLine.getAttribute("data-field")).toBe("display_column");
    expect(fieldLine.textContent).toBe("display_column: — → order_id");

    const relationships = within(
      screen.getByTestId("ontology-diff-relationships"),
    );
    expect(
      relationships.getByTestId("ontology-diff-relationships-removed").textContent,
    ).toContain("删除（1）");
    expect(
      relationships.getByTestId("ontology-diff-relationships-removed").textContent,
    ).toContain("#3 1.customer_id → 2.customer_id");
  });

  it("打发布版：新版本 201 如实；unchanged=true 显示后端 message 不谎称新版本", async () => {
    const backend = setup({
      ontologyVersions: [twoVersions[0]],
      // 有本体才可打版（假后端对空本体如实 422——真后端同语义）
      entities: [{ id: 1, name: "订单", datasetId: "ds-1", pkColumns: ["order_id"] }],
    });
    render(<OntologyVersionSection />);
    await waitFor(() => {
      expect(screen.getAllByTestId("ontology-version-row").length).toBe(1);
    });

    // 新版本：label 非空 → v3
    fireEvent.click(screen.getByTestId("ontology-publish"));
    fireEvent.change(screen.getByTestId("ontology-publish-label"), {
      target: { value: "回归定稿" },
    });
    fireEvent.click(screen.getByTestId("ontology-publish-submit"));
    const result = await screen.findByTestId("ontology-publish-result");
    expect(result.getAttribute("data-unchanged")).toBe("false");
    expect(result.textContent).toBe("已发布 v3");
    expect(backend.getOntologyVersions()[0].version).toBe(3);
    expect(backend.getOntologyVersions()[0].label).toBe("回归定稿");
  });

  it("打发布版 unchanged：如实呈现「与 vN 内容一致，未新增版本」", async () => {
    setup({
      ontologyVersions: [twoVersions[0]],
      ontologyPublishUnchanged: true,
      entities: [{ id: 1, name: "订单", datasetId: "ds-1", pkColumns: ["order_id"] }],
    });
    render(<OntologyVersionSection />);
    await waitFor(() => {
      expect(screen.getAllByTestId("ontology-version-row").length).toBe(1);
    });

    fireEvent.click(screen.getByTestId("ontology-publish"));
    fireEvent.click(screen.getByTestId("ontology-publish-submit"));
    const result = await screen.findByTestId("ontology-publish-result");
    expect(result.getAttribute("data-unchanged")).toBe("true");
    expect(result.textContent).toBe(
      "当前本体与 v2 内容一致（digest 相同），未新增版本",
    );
  });

  it("回滚（owner）：plan 预览 + 确认执行 digest_match 如实", async () => {
    setup({
      ontologyVersions: [twoVersions[0]],
      ontologyRollbackPlan: {
        update_entities: [
          {
            id: 1,
            name: "订单",
            fields: { description: { before: "改坏了", after: null } },
            before: { id: 1, name: "订单" },
            after: { id: 1, name: "订单" },
          },
        ],
      },
    });
    render(<OntologyVersionSection />);
    await waitFor(() => {
      expect(screen.getAllByTestId("ontology-version-row").length).toBe(1);
    });

    // owner 可见回滚入口
    fireEvent.click(screen.getByTestId("ontology-rollback-open"));
    await screen.findByTestId("ontology-rollback-plan");
    expect(screen.getByTestId("ontology-rollback-summary").textContent).toContain(
      "实体：建 0 / 改 1 / 删 0",
    );
    expect(screen.getByTestId("ontology-rollback-summary").textContent).toContain(
      "关系：建 0 / 删 0",
    );
    const update = screen.getByTestId("ontology-rollback-update");
    expect(update.textContent).toContain("订单 (#1)");
    expect(screen.getByTestId("ontology-rollback-field").textContent).toBe(
      "description: 改坏了 → —",
    );

    fireEvent.click(screen.getByTestId("ontology-rollback-confirm"));
    const executed = await screen.findByTestId("ontology-rollback-result");
    expect(executed.getAttribute("data-digest-match")).toBe("true");
    expect(
      screen.getByTestId("ontology-rollback-result-message").textContent,
    ).toBe("回滚完成，当前态与目标版本一致");
    expect(screen.getByTestId("ontology-rollback-result").textContent).toContain(
      "已执行 1 步",
    );
  });

  it("回滚中途失败：422 对象 detail 显示 message 原文与已执行步数", async () => {
    setup({
      ontologyVersions: [twoVersions[0]],
      ontologyRollbackPlan: {
        update_entities: [
          {
            id: 3,
            name: "订单",
            fields: { description: { before: "x", after: null } },
            before: { id: 3, name: "订单" },
            after: { id: 3, name: "订单" },
          },
        ],
      },
      failOntologyRollbackStatus: 422,
      failOntologyRollbackDetail: {
        message: "回滚在第 2 步失败（update_entity 3）：列快照漂移",
        executed: [{ action: "delete_relationship", target: 3 }],
        note: "回滚已如实停止；已执行条目已落 via=rollback 事件",
      },
    });
    render(<OntologyVersionSection />);
    await waitFor(() => {
      expect(screen.getAllByTestId("ontology-version-row").length).toBe(1);
    });

    fireEvent.click(screen.getByTestId("ontology-rollback-open"));
    // plan 成功（空计划也照常预览），确认执行 → 422
    await screen.findByTestId("ontology-rollback-plan");
    fireEvent.click(screen.getByTestId("ontology-rollback-confirm"));
    const error = await screen.findByTestId("ontology-rollback-error");
    // message 原文（绝不 [object Object]）
    expect(error.textContent).toContain(
      "回滚在第 2 步失败（update_entity 3）：列快照漂移",
    );
    expect(
      screen.getByTestId("ontology-rollback-error-executed").textContent,
    ).toBe("已执行 1 步");
    expect(error.textContent).toContain("已执行条目已落 via=rollback 事件");
  });

  it("非 owner（editor）：回滚入口不渲染", async () => {
    setup({
      ontologyVersions: [twoVersions[0]],
      // 默认 sub 落为 editor → GET /api/console/team 403 → 角色未知 → 不可见
      teamMembers: [{ logtoSub: "demo-customer", role: "editor" }],
    });
    render(<OntologyVersionSection />);
    await waitFor(() => {
      expect(screen.getAllByTestId("ontology-version-row").length).toBe(1);
    });
    // 时间线照常，回滚入口绝不渲染
    expect(screen.queryByTestId("ontology-rollback-open")).toBeNull();
    // 打发布版仍可见（design D8：只有回滚 owner-only）
    expect(screen.getByTestId("ontology-publish")).toBeTruthy();
  });

  it("完整性自检：不一致红态 + 差异计数 + escape_hint 原文；一致绿态", async () => {
    setup({
      ontologyVersions: [twoVersions[0]],
      ontologyIntegrity: {
        consistent: false,
        baseline: {
          kind: "snapshot",
          version: 1,
          digest: "a".repeat(64),
          events_folded: 5,
        },
        current: { digest: "b".repeat(64), entity_count: 2, relationship_count: 1 },
        differences: {
          entities: {
            added: [],
            removed: [],
            changed: [
              {
                id: 1,
                name: "订单",
                before: { id: 1, name: "订单" },
                after: { id: 1, name: "订单（直改）" },
                fields: { name: { before: "订单", after: "订单（直改）" } },
              },
            ],
          },
          relationships: { added: [], removed: [], changed: [] },
        },
        escape_hint:
          "当前态与事件流不一致：可能存在绕过 entities/service.py 的直改（裸 SQL）",
      },
    });
    render(<OntologyVersionSection />);
    const badge = await screen.findByTestId("ontology-integrity-badge");
    await waitFor(() => {
      expect(badge.getAttribute("data-consistent")).toBe("false");
    });
    expect(badge.textContent).toBe("与事件流不一致（1 处差异）");

    fireEvent.click(badge);
    const panel = await screen.findByTestId("ontology-integrity-panel");
    expect(
      screen.getByTestId("ontology-integrity-summary").textContent,
    ).toContain("与事件流不一致（1 处差异）");
    expect(screen.getByTestId("ontology-integrity-hint").textContent).toBe(
      "当前态与事件流不一致：可能存在绕过 entities/service.py 的直改（裸 SQL）",
    );
    expect(panel.textContent).toContain("基线：最早快照，已折叠 5 条事件");
    // 差异条目复用 diff 三组视图
    expect(
      screen.getByTestId("ontology-integrity-entities-changed").textContent,
    ).toContain("字段级改动（1）");
  });

  it("完整性自检一致：绿态徽章与基线摘要", async () => {
    setup({
      ontologyVersions: [twoVersions[0]],
      ontologyIntegrity: {
        consistent: true,
        baseline: {
          kind: "empty",
          version: null,
          digest: null,
          events_folded: 3,
        },
        current: { digest: "b".repeat(64), entity_count: 2, relationship_count: 1 },
      },
    });
    render(<OntologyVersionSection />);
    const badge = await screen.findByTestId("ontology-integrity-badge");
    await waitFor(() => {
      expect(badge.getAttribute("data-consistent")).toBe("true");
    });
    expect(badge.textContent).toBe("与事件流一致");
    fireEvent.click(badge);
    await screen.findByTestId("ontology-integrity-panel");
    expect(screen.getByTestId("ontology-integrity-summary").textContent).toContain(
      "与事件流一致",
    );
    expect(screen.getByTestId("ontology-integrity-panel").textContent).toContain(
      "基线：空基线，已折叠 3 条事件",
    );
  });
});
