import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // dev：目录 API 同源代理到本地 fd-wire-api（生产同源由网关承接，
    // 或以 VITE_API_BASE 覆盖基址）。
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
