/**
 * The OIDC glue, with the provider replaced by a fake UserManager.
 *
 * The bug these pin: sign-in copied the token into the credential slot, and
 * session expiry removed the OIDC record but not the copy. The gate saw a
 * credential, the badge said "API key set", and every request 401'd.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

type FakeUser = {
  access_token: string;
  id_token?: string;
  expired: boolean;
  expires_at?: number;
  profile: { sub: string; email?: string; name?: string };
};

// One shared fake so each test can shape what getUser / signinCallback return.
const fake = {
  user: null as FakeUser | null,
  callbackUser: null as FakeUser | null,
  removed: 0,
  signinRedirects: 0,
};

vi.mock("oidc-client-ts", () => ({
  WebStorageStateStore: class {},
  UserManager: class {
    async getUser() {
      return fake.user;
    }
    async signinCallback() {
      return fake.callbackUser;
    }
    async removeUser() {
      fake.removed += 1;
      fake.user = null;
    }
    async signinRedirect() {
      fake.signinRedirects += 1;
    }
    async signoutRedirect() {
      throw new Error("no end_session_endpoint");
    }
  },
}));

async function load() {
  vi.stubEnv("NEXT_PUBLIC_OIDC_ISSUER", "https://idp.example.test/");
  vi.stubEnv("NEXT_PUBLIC_OIDC_CLIENT_ID", "client-1");
  vi.stubEnv("NEXT_PUBLIC_OIDC_TOKEN", "id");
  const api = await import("./api");
  const oidc = await import("./oidc");
  return { api, oidc };
}

const alive: FakeUser = {
  access_token: "access.x.y",
  id_token: "id.x.y",
  expired: false,
  expires_at: Math.floor(Date.now() / 1000) + 3600,
  profile: { sub: "user-42", email: "u@example.test" },
};

beforeEach(() => {
  fake.user = null;
  fake.callbackUser = null;
  fake.removed = 0;
  fake.signinRedirects = 0;
  window.history.replaceState({}, "", "/");
});

describe("resume()", () => {
  it("is a no-op without an issuer configured", async () => {
    vi.stubEnv("NEXT_PUBLIC_OIDC_ISSUER", "");
    const oidc = await import("./oidc");
    expect(oidc.oidcEnabled).toBe(false);
    expect(await oidc.resume()).toBeNull();
  });

  it("restores a live session and stores the ID token as the credential", async () => {
    fake.user = alive;
    const { api, oidc } = await load();

    const who = await oidc.resume();

    expect(who?.subject).toBe("user-42");
    expect(who?.name).toBe("u@example.test");
    expect(window.sessionStorage.getItem("finance_rag_api_key")).toBe("id.x.y");
    expect(api.apiKeyKind()).toBe("oidc");
  });

  it("clears an OIDC credential when its session has expired", async () => {
    fake.user = { ...alive, expired: true };
    const { api, oidc } = await load();
    api.setApiKey("id.x.y", "oidc"); // what the previous sign-in left behind

    expect(await oidc.resume()).toBeNull();

    expect(fake.removed).toBe(1);
    expect(api.hasApiKey()).toBe(false);
  });

  it("leaves a pasted API key alone when there is no OIDC session", async () => {
    fake.user = null;
    const { api, oidc } = await load();
    api.setApiKey("operator-key"); // kind: api_key

    expect(await oidc.resume()).toBeNull();

    expect(api.hasApiKey()).toBe(true);
    expect(api.apiKeyKind()).toBe("api_key");
  });

  it("completes the callback and strips the code from the URL", async () => {
    fake.callbackUser = alive;
    window.history.replaceState({}, "", "/?code=abc&state=xyz");
    const { api, oidc } = await load();

    const who = await oidc.resume();

    expect(who?.subject).toBe("user-42");
    expect(window.location.search).toBe("");
    expect(api.apiKeyKind()).toBe("oidc");
  });

  it("surfaces a provider error and clears it from the URL", async () => {
    window.history.replaceState({}, "", "/?error=access_denied&error_description=Nope");
    const { oidc } = await load();

    await expect(oidc.resume()).rejects.toThrow("Nope");
    expect(window.location.search).toBe("");
  });
});

describe("signOut()", () => {
  it("clears the credential even when the provider has no logout endpoint", async () => {
    fake.user = alive;
    const { api, oidc } = await load();
    await oidc.resume();
    expect(api.hasApiKey()).toBe(true);

    await oidc.signOut();

    expect(api.hasApiKey()).toBe(false);
    expect(fake.removed).toBe(1);
  });
});
