import { describe, expect, it } from "vitest";

import { apiKeyKind, clearApiKey, hasApiKey, setApiKey } from "./api";

describe("credential slot", () => {
  it("is empty in a fresh tab", () => {
    expect(hasApiKey()).toBe(false);
    expect(apiKeyKind()).toBeNull();
  });

  it("records a pasted key as api_key by default", () => {
    setApiKey("secret");
    expect(hasApiKey()).toBe(true);
    expect(apiKeyKind()).toBe("api_key");
  });

  it("records where an OIDC token came from", () => {
    setApiKey("eyJ.token.sig", "oidc");
    expect(apiKeyKind()).toBe("oidc");
  });

  it("clear removes both the credential and its kind", () => {
    setApiKey("secret", "oidc");
    clearApiKey();
    expect(hasApiKey()).toBe(false);
    expect(apiKeyKind()).toBeNull();
  });

  it("never persists past the tab: sessionStorage, not localStorage", () => {
    setApiKey("secret");
    expect(window.localStorage.getItem("finance_rag_api_key")).toBeNull();
    expect(window.sessionStorage.getItem("finance_rag_api_key")).toBe("secret");
  });
});
