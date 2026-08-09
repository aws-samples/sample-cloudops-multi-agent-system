/**
 * OIDC authentication module for the static Next.js frontend.
 * Handles Cognito authorization code flow, token management, and dev bypass.
 */

const OIDC_DISCOVERY_URL = process.env.NEXT_PUBLIC_OIDC_DISCOVERY_URL || "";
const OIDC_CLIENT_ID = process.env.NEXT_PUBLIC_OIDC_CLIENT_ID || "";
const OIDC_CALLBACK_URL = process.env.NEXT_PUBLIC_OIDC_CALLBACK_URL || "";
const COGNITO_DOMAIN = process.env.NEXT_PUBLIC_COGNITO_DOMAIN || "";
const DEV_AUTH_BYPASS = process.env.NEXT_PUBLIC_DEV_AUTH_BYPASS === "true";

interface OidcConfig {
    authorization_endpoint: string;
    token_endpoint: string;
    end_session_endpoint?: string;
}

let cachedConfig: OidcConfig | null = null;
let idToken: string | null = null;
let accessToken: string | null = null;
let refreshToken: string | null = null;
let tokenExpiry = 0;
let userEmail: string | null = null;

// ----- Session persistence -----------------------------------------------
//
// Tokens live in sessionStorage (not localStorage) so they die with the tab.
// Losing them on page refresh — the prior behavior — would bounce the user
// back through Cognito on every reload, drop their chat thread list, and
// silently 401 any in-flight panel polls whose tab sat idle past token expiry.
// sessionStorage is a same-origin, per-tab store: XSS scope is identical to
// having the tokens in-memory, but the convenience payoff is large.
//
// We eagerly rehydrate at module load so `isAuthenticated()` can answer
// synchronously on the first call without racing an async restore.

const SESSION_KEY = "cloudops-auth-session";

interface PersistedSession {
    idToken: string | null;
    accessToken: string | null;
    refreshToken: string | null;
    tokenExpiry: number;
    userEmail: string | null;
}

function persistSession(): void {
    if (typeof window === "undefined") return;
    try {
        const session: PersistedSession = {
            idToken,
            accessToken,
            refreshToken,
            tokenExpiry,
            userEmail,
        };
        sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
    } catch {
        /* quota exceeded / private mode — fall back to in-memory only */
    }
}

function clearPersistedSession(): void {
    if (typeof window === "undefined") return;
    try {
        sessionStorage.removeItem(SESSION_KEY);
    } catch {
        /* ignore */
    }
}

function rehydrateSession(): void {
    if (typeof window === "undefined") return;
    try {
        const raw = sessionStorage.getItem(SESSION_KEY);
        if (!raw) return;
        const session = JSON.parse(raw) as PersistedSession;
        // Skip rehydration if the token is already expired — the getToken()
        // refresh path will handle the refresh_token flow on first use.
        if (session.tokenExpiry && Date.now() < session.tokenExpiry) {
            idToken = session.idToken;
            accessToken = session.accessToken;
            refreshToken = session.refreshToken;
            tokenExpiry = session.tokenExpiry;
            userEmail = session.userEmail;
        } else if (session.refreshToken) {
            // Token expired but we still have a refresh_token — keep it so
            // getToken() can exchange it for a fresh id_token.
            refreshToken = session.refreshToken;
            userEmail = session.userEmail;
        }
    } catch {
        /* malformed json — nothing to rehydrate */
    }
}

// Hydrate once at module load (client-side only).
if (typeof window !== "undefined") {
    rehydrateSession();
}

async function fetchOidcConfig(): Promise<OidcConfig> {
    if (cachedConfig) return cachedConfig;
    if (!OIDC_DISCOVERY_URL) throw new Error("OIDC_DISCOVERY_URL not configured");
    const resp = await fetch(OIDC_DISCOVERY_URL);
    cachedConfig = await resp.json();
    return cachedConfig!;
}

export function isDevBypass(): boolean {
    return DEV_AUTH_BYPASS;
}

export function isAuthenticated(): boolean {
    if (DEV_AUTH_BYPASS) return true;
    // A valid id_token (used for JWT-authorized API calls) OR a refresh_token
    // we can use to get a fresh id_token counts as authenticated. Without the
    // refresh_token check, a user who returned to a tab after id_token expiry
    // would get bounced through Cognito even though the refresh flow would
    // have worked silently.
    if (idToken && Date.now() < tokenExpiry) return true;
    if (refreshToken) return true;
    return false;
}

export async function login(): Promise<void> {
    if (DEV_AUTH_BYPASS) return;
    const config = await fetchOidcConfig();
    const params = new URLSearchParams({
        response_type: "code",
        client_id: OIDC_CLIENT_ID,
        redirect_uri: OIDC_CALLBACK_URL,
        scope: "openid email profile",
    });
    window.location.href = `${config.authorization_endpoint}?${params}`;
}

// Deduped by code: React StrictMode (on in `next dev` under Next 16) mounts effects
// twice, so the callback effect calls this twice with the SAME one-time authorization
// code. The second exchange hit Cognito after the code was redeemed → 400 → the
// caller's catch() restarted login() → an infinite "Authenticating..." loop. Reusing
// the in-flight promise makes the double-invoke harmless. Module-level is safe: both
// StrictMode invocations share this module instance.
let callbackInFlight: { code: string; promise: Promise<void> } | null = null;

export function handleCallback(code: string): Promise<void> {
    if (callbackInFlight?.code === code) return callbackInFlight.promise;
    callbackInFlight = { code, promise: doHandleCallback(code) };
    return callbackInFlight.promise;
}

async function doHandleCallback(code: string): Promise<void> {
    console.log("[auth] handleCallback starting with code:", code.slice(0, 10) + "...");
    const config = await fetchOidcConfig();
    const resp = await fetch(config.token_endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
            grant_type: "authorization_code",
            client_id: OIDC_CLIENT_ID,
            redirect_uri: OIDC_CALLBACK_URL,
            code,
        }),
    });
    if (!resp.ok) throw new Error(`Token exchange failed: ${resp.status}`);
    const tokens = await resp.json();
    accessToken = tokens.access_token;
    idToken = tokens.id_token;
    refreshToken = tokens.refresh_token || null;
    tokenExpiry = Date.now() + (tokens.expires_in || 3600) * 1000;
    // Decode email from ID token payload
    try {
        const payload = JSON.parse(atob(idToken!.split(".")[1]));
        userEmail = payload.email || null;
        console.log("[auth] handleCallback complete, userEmail:", userEmail);
    } catch {
        userEmail = null;
        console.error("[auth] Failed to decode email from token");
    }
    persistSession();
}

export async function getToken(): Promise<string | null> {
    if (DEV_AUTH_BYPASS) return null;
    // Refresh if the token is missing, expired, or expires within 5 minutes,
    // provided we still have a refresh_token to exchange.
    const needsRefresh = !idToken || Date.now() > tokenExpiry - 300_000;
    if (needsRefresh && refreshToken) {
        try {
            const config = await fetchOidcConfig();
            const resp = await fetch(config.token_endpoint, {
                method: "POST",
                headers: { "Content-Type": "application/x-www-form-urlencoded" },
                body: new URLSearchParams({
                    grant_type: "refresh_token",
                    client_id: OIDC_CLIENT_ID,
                    refresh_token: refreshToken,
                }),
            });
            if (resp.ok) {
                const tokens = await resp.json();
                idToken = tokens.id_token;
                if (tokens.refresh_token) refreshToken = tokens.refresh_token;
                tokenExpiry = Date.now() + (tokens.expires_in || 3600) * 1000;
                // Re-derive userEmail from the new id_token in case it shifted.
                try {
                    const payload = JSON.parse(atob(idToken!.split(".")[1]));
                    userEmail = payload.email || userEmail;
                } catch {
                    /* keep existing email */
                }
                persistSession();
            } else if (resp.status === 400 || resp.status === 401) {
                // Refresh_token rejected — clear everything so the UI redirects
                // the user to a fresh login instead of looping silently.
                idToken = null;
                accessToken = null;
                refreshToken = null;
                tokenExpiry = 0;
                userEmail = null;
                clearPersistedSession();
            }
        } catch { /* network error — keep existing token */ }
    }
    return idToken;
}

export function getActorId(): string {
    if (DEV_AUTH_BYPASS) return "dev-user";
    if (!userEmail) {
        console.warn("[auth] getActorId called but userEmail is null - returning 'anonymous'");
        return "anonymous";
    }
    const actorId = sanitizeActorId(userEmail);
    console.log("[auth] getActorId:", actorId);
    return actorId;
}

export function getUserEmail(): string | null {
    if (DEV_AUTH_BYPASS) return "dev-user@localhost";
    return userEmail;
}

export function sanitizeActorId(email: string): string {
    return email.replace(/@/g, "_at_").replace(/\./g, "_");
}

export function logout(): void {
    idToken = null;
    accessToken = null;
    refreshToken = null;
    tokenExpiry = 0;
    userEmail = null;
    clearPersistedSession();
}

export async function signOut(): Promise<void> {
    // Clear local state
    logout();

    if (DEV_AUTH_BYPASS) {
        window.location.reload();
        return;
    }

    // Redirect to Cognito logout endpoint
    // Two options:
    // 1. logout_uri + client_id - redirects to custom sign-out page (must be in Allowed sign-out URLs)
    // 2. redirect_uri + client_id + response_type + scope - redirects back to login page
    // We use option 2 to redirect back to login
    if (COGNITO_DOMAIN) {
        const params = new URLSearchParams({
            client_id: OIDC_CLIENT_ID,
            redirect_uri: OIDC_CALLBACK_URL,
            response_type: "code",
            scope: "openid email profile",
        });
        const logoutUrl = `${COGNITO_DOMAIN}/logout?${params}`;
        window.location.href = logoutUrl;
        return;
    }

    // Fallback: redirect to callback URL which will trigger login
    window.location.href = OIDC_CALLBACK_URL || window.location.origin;
}
