/**
 * Frontend API client — CRUD operations via API Gateway, chat via AgentCore Runtime.
 *
 * CRUD (sessions, templates, reports) → API Gateway HTTP API with Cognito JWT
 * Chat → AgentCore Runtime with AG-UI protocol
 */

const RUNTIME_ARN = process.env.NEXT_PUBLIC_RUNTIME_ARN || "";
const AWS_REGION = process.env.NEXT_PUBLIC_AWS_REGION || "ap-southeast-1";
const FRONTEND_API_URL = process.env.NEXT_PUBLIC_FRONTEND_API_URL || "";

// Runtime config loaded from config.json (generated post-deploy)
let _runtimeConfig: Record<string, string> | null = null;

async function getRuntimeConfig(): Promise<Record<string, string>> {
    if (_runtimeConfig) return _runtimeConfig;
    try {
        const resp = await fetch("/config.json");
        if (resp.ok) {
            _runtimeConfig = await resp.json();
            return _runtimeConfig!;
        }
    } catch { /* ignore */ }
    _runtimeConfig = {};
    return _runtimeConfig;
}

async function getFrontendApiUrl(): Promise<string> {
    if (FRONTEND_API_URL) return FRONTEND_API_URL;
    const config = await getRuntimeConfig();
    return config.FRONTEND_API_URL || "";
}

export interface Session {
    session_id: string;
    created_at: string;
    message_count: number;
    preview: string;
}

export interface Message {
    role: "user" | "assistant";
    content: string;
    tool_invocations?: Array<{ tool_name: string; parameters: Record<string, unknown>; result?: string }>;
    /** True when this user turn was sent in report mode (templated, freeform,
     *  or edit). Set by the core-api history endpoint from the persisted
     *  <report-request/> marker so the prompt bubble can be badged on reload. */
    is_report?: boolean;
}

export interface Template {
    template_id: string;
    user_id: string;
    name: string;
    description: string;
    prompt?: string;
    sections?: Array<{ id: string; title: string; prompt: string }>;
    dependencies?: Record<string, string>;
    created_at: string;
    updated_at: string;
}

export interface ReportSummary {
    report_id: string;
    title: string;
    status: string;
    month: string;
    year: string;
    created_at: string;
}

export interface ReportTrace {
    tool_name: string;
    duration_s?: number;
    status?: string;
    input?: Record<string, unknown>;
    output?: string;
    tool_trace?: ReportTrace[];
}

export interface ReportSection {
    id: string;
    title: string;
    status: string;
    content: string;
    error: string;
    generated_at: string;
    traces?: ReportTrace[];
}

export interface Report {
    report_id: string;
    user_id: string;
    title: string;
    status: string;
    month: string;
    year: string;
    created_at: string;
    updated_at: string;
    sections: ReportSection[];
    current_section: number;
    total_sections: number;
}

type GetTokenFn = () => Promise<string | null>;

// ---------------------------------------------------------------------------
// API Gateway helpers (for CRUD operations)
// ---------------------------------------------------------------------------
async function apiCall<T = Record<string, unknown>>(
    method: string,
    path: string,
    getToken: GetTokenFn,
    body?: Record<string, unknown>,
): Promise<T> {
    const baseUrl = await getFrontendApiUrl();
    if (!baseUrl) throw new Error("Frontend API URL not configured");
    const token = await getToken();
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;

    const opts: RequestInit = { method, headers };
    if (body && method !== "GET") opts.body = JSON.stringify(body);

    const resp = await fetch(`${baseUrl}${path}`, opts);
    if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`API ${resp.status}: ${text}`);
    }
    return resp.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Session CRUD (via API Gateway)
// ---------------------------------------------------------------------------
export async function listSessions(actorId: string, getToken: GetTokenFn): Promise<Session[]> {
    const data = await apiCall<{ sessions?: Session[] }>("GET", "/sessions", getToken);
    return data.sessions || [];
}

export async function getSessionHistory(sessionId: string, actorId: string, getToken: GetTokenFn): Promise<Message[]> {
    const data = await apiCall<{ messages?: Message[] }>("GET", `/sessions/${sessionId}/history`, getToken);
    return data.messages || [];
}

export async function deleteSession(sessionId: string, actorId: string, getToken: GetTokenFn): Promise<void> {
    await apiCall("DELETE", `/sessions/${sessionId}`, getToken);
}

// ---------------------------------------------------------------------------
// Template CRUD (via API Gateway)
// ---------------------------------------------------------------------------
export async function listTemplates(userId: string, getToken: GetTokenFn): Promise<Template[]> {
    const data = await apiCall<{ templates?: Template[] }>("GET", "/templates", getToken);
    return data.templates || [];
}

export async function createTemplate(
    userId: string,
    template: { name: string; description: string; prompt?: string; sections?: Array<{ id: string; title: string; prompt: string }>; dependencies?: Record<string, string> },
    getToken: GetTokenFn,
): Promise<{ template_id: string }> {
    return apiCall<{ template_id: string }>("POST", "/templates", getToken, template);
}

export async function updateTemplate(
    userId: string,
    templateId: string,
    template: { name: string; description: string; prompt?: string; sections?: Array<{ id: string; title: string; prompt: string }>; dependencies?: Record<string, string> },
    getToken: GetTokenFn,
): Promise<void> {
    await apiCall("PUT", `/templates/${templateId}`, getToken, template);
}

export async function deleteTemplate(userId: string, templateId: string, getToken: GetTokenFn): Promise<void> {
    await apiCall("DELETE", `/templates/${templateId}`, getToken);
}

// ---------------------------------------------------------------------------
// Report operations (via API Gateway — read-only; generation stays in supervisor)
// ---------------------------------------------------------------------------
export async function listReports(userId: string, getToken: GetTokenFn): Promise<ReportSummary[]> {
    const data = await apiCall<{ reports?: ReportSummary[] }>("GET", "/reports", getToken);
    return data.reports || [];
}

export async function getReport(reportId: string, userId: string, getToken: GetTokenFn): Promise<Report> {
    return apiCall<Report>("GET", `/reports/${reportId}`, getToken);
}

export async function getReportStatus(reportId: string, userId: string, getToken: GetTokenFn): Promise<Record<string, unknown>> {
    return apiCall("GET", `/reports/${reportId}/status`, getToken);
}

export async function deleteReport(reportId: string, userId: string, getToken: GetTokenFn): Promise<void> {
    await apiCall("DELETE", `/reports/${reportId}`, getToken);
}

// ---------------------------------------------------------------------------
// Thread activity — per-thread busy state for cross-tab/navigate-away awareness
// ---------------------------------------------------------------------------
export interface ThreadActivity {
    status: "idle" | "running" | "error";
    current_step?: string;
    started_at?: string;
    updated_at?: string;
    run_id?: string;
    report_id?: string;
    error_msg?: string;
    stale?: boolean;
}

export async function getThreadActivity(threadId: string, getToken: GetTokenFn): Promise<ThreadActivity> {
    return apiCall<ThreadActivity>("GET", `/threads/${threadId}/activity`, getToken);
}
