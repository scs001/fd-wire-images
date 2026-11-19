// 4.3 的验收：navigate 白名单必须拒绝 parseHash 会回落成 home 的未知路由，
// 否则宿主回 ok:true 而实际停在首页——agent 会以为用户到了目标页。
import { describe, expect, it } from "vitest";
import { parseHash } from "./App";
import { resolveNavigateTarget } from "./copilotIntents";

const nav = (to: string) => resolveNavigateTarget(to, parseHash);

describe("resolveNavigateTarget", () => {
  it("接受已知路由", () => {
    expect(nav("#/pricing")).toBe("#/pricing");
    expect(nav("#/docs")).toBe("#/docs");
    expect(nav("#/showcase")).toBe("#/showcase");
    expect(nav("#/console/workbench")).toBe("#/console/workbench");
    // 无 # 前缀也接受，规范化成带 #
    expect(nav("/pricing")).toBe("#/pricing");
  });

  it("接受首页的两个写法", () => {
    expect(nav("#/")).toBe("#/");
    expect(nav("/")).toBe("#/");
  });

  it("拒绝会回落成 home 的未知路由", () => {
    // 这几个 parseHash 都返回 home，但目标明显不是首页 → 必须拒绝
    expect(nav("#/nonexistent-page")).toBeNull();
    expect(nav("#/console")).not.toBeNull(); // console 无 tab 是合法回落，见下条
  });

  it("拒绝站外与危险 scheme", () => {
    expect(nav("https://evil.example/x")).toBeNull();
    expect(nav("javascript:alert(1)")).toBeNull();
    expect(nav("//evil.example/x")).toBeNull();
    expect(nav("")).toBeNull();
  });
});
