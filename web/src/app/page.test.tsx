/**
 * The front door. Which screen a visitor gets is decided by three things --
 * is a provider configured, is there a session, is there a key -- and the
 * wrong answer is a dashboard on which every button fails.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const health = {
  status: "ok",
  service: "finance-rag",
  company: "Source Advisors",
  multimodal: true,
};

// The page calls getHealth on mount; a real fetch would fail in jsdom.
const fakeResume = vi.fn();
vi.mock("@/lib/oidc", async () => {
  const actual = await vi.importActual<typeof import("@/lib/oidc")>("@/lib/oidc");
  return {
    ...actual,
    // Read at import time from env; the tests stub env before importing.
    get oidcEnabled() {
      return Boolean(process.env.NEXT_PUBLIC_OIDC_ISSUER);
    },
    resume: () => fakeResume(),
    signIn: vi.fn(),
    signOut: vi.fn(),
  };
});

async function renderPage() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, json: async () => health }))
  );
  const { default: Page } = await import("./page");
  return render(<Page />);
}

beforeEach(() => {
  fakeResume.mockReset();
});

describe("without an identity provider", () => {
  it("shows the dashboard with the API key field in the header", async () => {
    vi.stubEnv("NEXT_PUBLIC_OIDC_ISSUER", "");
    fakeResume.mockResolvedValue(null);

    await renderPage();

    expect(await screen.findByRole("heading", { name: "Ask" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Paste your API key")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign in/i })).not.toBeInTheDocument();
  });
});

describe("with an identity provider", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_OIDC_ISSUER", "https://idp.example.test/");
    vi.stubEnv("NEXT_PUBLIC_OIDC_CLIENT_ID", "client-1");
  });

  it("holds the screen until the session check has settled", async () => {
    let settle: (v: null) => void = () => {};
    fakeResume.mockReturnValue(new Promise((r) => (settle = r)));

    await renderPage();

    expect(screen.getByText(/checking session/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign in/i })).not.toBeInTheDocument();

    settle(null);
    expect(await screen.findByRole("button", { name: /sign in/i })).toBeInTheDocument();
  });

  it("is the sign-in screen when there is no session and no key", async () => {
    fakeResume.mockResolvedValue(null);

    await renderPage();

    expect(await screen.findByRole("button", { name: /sign in/i })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Ask" })).not.toBeInTheDocument();
    // The operator path is still there, folded away.
    expect(screen.getByText(/have an api key instead/i)).toBeInTheDocument();
  });

  it("is the dashboard when signed in, attributed by name", async () => {
    fakeResume.mockImplementation(async () => {
      window.sessionStorage.setItem("finance_rag_api_key", "id.x.y");
      window.sessionStorage.setItem("finance_rag_api_key_kind", "oidc");
      return { subject: "user-42", name: "u@example.test", expiresAt: undefined };
    });

    await renderPage();

    expect(await screen.findByRole("heading", { name: "Ask" })).toBeInTheDocument();
    expect(screen.getByText(/signed in: u@example\.test/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign out/i })).toBeInTheDocument();
    // No key field for a signed-in person; the token is their credential.
    expect(screen.queryByPlaceholderText(/api key/i)).not.toBeInTheDocument();
  });

  it("is the dashboard for an operator with a pasted key and no session", async () => {
    window.sessionStorage.setItem("finance_rag_api_key", "operator-key");
    window.sessionStorage.setItem("finance_rag_api_key_kind", "api_key");
    fakeResume.mockResolvedValue(null);

    await renderPage();

    expect(await screen.findByRole("heading", { name: "Ask" })).toBeInTheDocument();
    expect(screen.getByText(/api key set/i)).toBeInTheDocument();
  });

  it("shows a provider error on the sign-in screen rather than hiding it", async () => {
    fakeResume.mockRejectedValue(new Error("access_denied"));

    await renderPage();

    await waitFor(() => expect(screen.getByText("access_denied")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /sign in/i })).toBeInTheDocument();
  });
});
