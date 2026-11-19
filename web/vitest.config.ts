// 最小 vitest 配置（任务 6.2）：组件级 happy-path 测试。
// 独立于 vite.config.ts（不挂 react/tailwind 插件，测试不走样式构建）。
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "happy-dom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
