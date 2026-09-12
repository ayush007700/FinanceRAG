// OIDC sign-in for the browser, using Authorization Code with PKCE.
//
// This is a static export with no server, which rules out every flow that
// needs a client secret. PKCE is the one designed for exactly this: a public
// client proves it started the login by presenting a hash it generated, so an
// intercepted authorization code is useless without the verifier that never
// left this tab.
//
// The API already accepts OIDC tokens in the same Authorization header as API
// keys, so a successful login simply stores the token where the key would go.
// Nothing downstream in api.ts changes -- authHeaders() does not know or care
// which kind of credential it is sending.
//
// Configuration is NEXT_PUBLIC_* on purpose. Unlike an API key, none of it is
// secret: the issuer URL and client id of a public client are, by definition,
// things every browser that logs in must know.

import { User, UserManager, WebStorageStateStore } from "oidc-client-ts";

import { apiKeyKind, clearApiKey, setApiKey } from "./api";

const ISSUER = process.env.NEXT_PUBLIC_OIDC_ISSUER || "";
const CLIENT_ID = process.env.NEXT_PUBLIC_OIDC_CLIENT_ID || "";
const SCOPES = process.env.NEXT_PUBLIC_OIDC_SCOPES || "openid profile email";
// Which token to send to the API. Access tokens are the correct choice -- they
// are minted for the API's audience -- but Cognito's access tokens carry
// `client_id` rather than `aud`, and the API pins audience. For Cognito, send
// the ID token, whose `aud` is the client id, and set AUTH_JWT_AUDIENCE to it.
const TOKEN_KIND = (process.env.NEXT_PUBLIC_OIDC_TOKEN || "access") as "access" | "id";

export const oidcEnabled = Boolean(ISSUER && CLIENT_ID);

let manager: UserManager | null = null;

function redirectUri(): string {
  // The page itself is the callback: with trailingSlash and a static host,
  // a dedicated /callback route would need its own index.html and a rewrite
  // rule on S3. Landing back on / and reading the query is simpler and works
  // on every static host.
  return `${window.location.origin}/`;
}

function getManager(): UserManager {
  if (manager) return manager;
  manager = new UserManager({
    authority: ISSUER,
    client_id: CLIENT_ID,
    redirect_uri: redirectUri(),
    post_logout_redirect_uri: redirectUri(),
    response_type: "code",
    scope: SCOPES,
    // sessionStorage, matching the API key: a credential that survives the
    // tab is a credential that survives the person walking away.
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
    // Refresh via the refresh token when the provider issues one; otherwise
    // the user signs in again when the access token expires. Silent iframe
    // renew is deliberately off -- it depends on third-party-cookie behaviour
    // that browsers are removing.
    automaticSilentRenew: false,
  });
  return manager;
}

function tokenOf(user: User): string {
  return TOKEN_KIND === "id" ? user.id_token ?? "" : user.access_token;
}

export type SignedIn = {
  subject: string;
  name: string;
  expiresAt: number | undefined;
};

function describe(user: User): SignedIn {
  const p = user.profile;
  return {
    subject: p.sub,
    name: (p.name || p.preferred_username || p.email || p.sub) as string,
    expiresAt: user.expires_at,
  };
}

/** Start the redirect to the identity provider. */
export async function signIn(): Promise<void> {
  await getManager().signinRedirect();
}

/** Sign out locally and at the provider. */
export async function signOut(): Promise<void> {
  clearApiKey();
  const m = getManager();
  try {
    await m.signoutRedirect();
  } catch {
    // Providers without an end_session_endpoint reject this; the local
    // session is already gone, which is what matters for this tab.
    await m.removeUser();
  }
}

/**
 * Complete a login if this page load is the provider redirecting back, and
 * otherwise report any session already in this tab.
 *
 * Called once on mount. Returns who is signed in, or null.
 */
export async function resume(): Promise<SignedIn | null> {
  if (!oidcEnabled || typeof window === "undefined") return null;
  const m = getManager();
  const params = new URLSearchParams(window.location.search);

  if (params.has("code") && params.has("state")) {
    // The library validates `state` against what it stored before the
    // redirect, which is what stops a login response being planted on us.
    const user = await m.signinCallback();
    // Strip the code from the URL: it is single-use, but leaving it in the
    // address bar puts it in history and in anything that logs referrers.
    window.history.replaceState({}, "", redirectUri());
    if (!user) return null;
    setApiKey(tokenOf(user), "oidc");
    return describe(user);
  }

  if (params.has("error")) {
    const detail = params.get("error_description") || params.get("error") || "sign-in failed";
    window.history.replaceState({}, "", redirectUri());
    throw new Error(detail);
  }

  const user = await m.getUser();
  if (!user || user.expired) {
    if (user) await m.removeUser();
    // The token was copied into the credential slot at sign-in. Removing the
    // OIDC session without clearing that copy left an expired token where a
    // key would be: the gate saw "a credential is set", let the visitor
    // through to a dashboard on which every request 401'd, and the badge
    // said "API key set". Only an OIDC-sourced credential is cleared here; a
    // pasted key is the operator's and outlives any provider session.
    if (apiKeyKind() === "oidc") clearApiKey();
    return null;
  }
  setApiKey(tokenOf(user), "oidc");
  return describe(user);
}
