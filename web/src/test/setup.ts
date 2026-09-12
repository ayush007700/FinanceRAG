import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";

// Every test starts with an empty tab: no credential, no OIDC session.
afterEach(() => {
  window.sessionStorage.clear();
  vi.unstubAllEnvs();
  vi.resetModules();
});
