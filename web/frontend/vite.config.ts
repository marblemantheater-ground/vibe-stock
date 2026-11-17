import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 빌드 결과는 web/dist 로 → 매매 프로그램(FastAPI)이 그대로 제공한다.
// 개발 중(npm run dev)에는 /api 요청을 실행 중인 매매 프로그램으로 넘긴다.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../dist", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
