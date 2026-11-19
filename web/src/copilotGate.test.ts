// 副驾去重的验收（unify-wire-copilot-surface 2.1/2.2）：一个界面只有一个对话入口。
import { describe, expect, it } from "vitest";
import { shouldMountWanxingCopilot } from "./copilotGate";
import { parseHash } from "./App";

describe("shouldMountWanxingCopilot", () => {
  it("控制台与内部后台：萬星退场（原生助手在场）", () => {
    expect(shouldMountWanxingCopilot("#/console")).toBe(false);
    expect(shouldMountWanxingCopilot("#/console/datasets")).toBe(false);
    expect(shouldMountWanxingCopilot("#/console/chat")).toBe(false);
    expect(shouldMountWanxingCopilot("#/console/demands/new")).toBe(false);
    expect(shouldMountWanxingCopilot("#/admin/customers")).toBe(false);
  });

  it("公开只读面：两个都不出现", () => {
    expect(shouldMountWanxingCopilot("#/share/dashboard/1")).toBe(false);
    expect(shouldMountWanxingCopilot("#/share/form/1")).toBe(false);
    expect(shouldMountWanxingCopilot("#/showcase")).toBe(false);
    expect(shouldMountWanxingCopilot("#/showcase/12")).toBe(false);
    expect(shouldMountWanxingCopilot("#/login")).toBe(false);
  });

  it("公开面：萬星在场（那里没有原生助手）", () => {
    expect(shouldMountWanxingCopilot("#/")).toBe(true);
    expect(shouldMountWanxingCopilot("")).toBe(true);
    expect(shouldMountWanxingCopilot("#/pricing")).toBe(true);
    expect(shouldMountWanxingCopilot("#/docs")).toBe(true);
    expect(shouldMountWanxingCopilot("#/catalog")).toBe(true);
    expect(shouldMountWanxingCopilot("#/catalog/ind-001")).toBe(true);
  });

  it("未知 hash 回落首页 → 萬星在场（与 parseHash 的回落一致）", () => {
    expect(parseHash("#/nope").name).toBe("home");
    expect(shouldMountWanxingCopilot("#/nope")).toBe(true);
  });
});
