import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// registry 默认监听 :9000;后端无 CORS 中间件,开发模式用 dev proxy 零后端改动。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": {
        target: "http://localhost:9000",
        changeOrigin: true,
      },
      "/healthz": {
        target: "http://localhost:9000",
        changeOrigin: true,
      },
    },
  },
});
