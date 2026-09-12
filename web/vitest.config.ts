import path from "node:path";

import { defineConfig } from "vitest/config";

export default defineConfig({
  // No React plugin: it exists for Fast Refresh, which tests do not use, and
  // its Vite major drifts from vitest's. JSX is handled by esbuild below.
  //
  // Next's tsconfig says jsx: "preserve" for its own compiler; esbuild needs
  // to be told to use the automatic runtime or tests with JSX see no React.
  esbuild: { jsx: "automatic" },
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // CSS modules resolve to their class names, so `styles.gate` is "gate"
    // and assertions can target it.
    css: { modules: { classNameStrategy: "non-scoped" } },
  },
});
