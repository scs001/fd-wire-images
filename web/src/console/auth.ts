// 控制台登录：Logto SPA authorization code + PKCE（浏览器侧完成，无客户端密钥）。
// 平移自 fd-lex/web/src/console/auth.ts 样板（wire-self-serve-launch 任务 4.4），
// 差异点：wire 不用 API resource 参数（授权面走 wgk- key 组快照，不依赖
// Logto audience），登录参数由后端 /api/console/auth/config 运行时下发。
//
// 令牌卫生（console-auth spec）：
// - ID token 仅存 sessionStorage（关标签即失效），不落 localStorage、不落服务端日志；
// - refresh token（offline_access）随会话存取，仅用于静默续期；
// - 受保护请求一律 Authorization: Bearer <ID token>；X-Customer-Id 彻底弃用；
// - 未配置（configured=false）时前端如实显示阻塞态，绝不发起假登录。
//
// 首登开户：拿到 ID token 后 POST /api/console/session 触发 sub→Customer 映射
// 与既有开户 ceremony；key_once 仅完成开户那次非 null，UI 明文展示一次。

import { API_BASE } from "../catalog/api";

export interface ConsoleAuthConfig {
  configured: boolean;
  issuer: string;
  client_id: string;
  scopes: string[];
  organization_required: boolean;
}

/** 登录映射到的客户（会话建立响应的 customer 投影）。 */
export interface ConsoleCustomerInfo {
  id: number;
  name: string;
  plan_code: string;
}

export interface ConsoleSession {
  idToken: string;
  refreshToken?: string;
  /** 过期时刻（ms epoch，按 token 响应 expires_in 折算）。 */
  expiresAt: number;
  /** 展示身份（来自 ID token claims，仅供显示）。 */
  identity: string;
  email?: string;
  name?: string;
  /** Logto 角色 claim（admin 导航门禁认 wire-admin）。 */
  roles: string[];
  /** 会话建立后回填的客户信息（含 plan_code，免费档门禁用）。 */
  customer?: ConsoleCustomerInfo;
}

/** POST /api/console/session 的结果：客户信息 + 仅一次的明文密钥。 */
export interface SessionEstablishment {
  customer: ConsoleCustomerInfo;
  /** 仅完成开户那次非 null；UI 明文展示一次，此后服务端不再下发。 */
  keyOnce: string | null;
}

export class ConsoleAuthError extends Error {
  readonly status: number | undefined;
  readonly detail: string | undefined;

  constructor(message: string, status?: number, detail?: string) {
    super(message);
    this.name = "ConsoleAuthError";
    this.status = status;
    this.detail = detail;
  }
}

export const SESSION_STORAGE_KEY = "wire.console.session";
export const PKCE_STORAGE_KEY = "wire.console.pkce";
/**
 * 管理员分流落地标记（wire-admin-console-routing 任务 3.2）：
 * 裸 #/console 的角色分流把 wire-admin 会话送进 #/admin，而管理员同时是客户——
 * 若不记这一笔，其从后台（或文档页）回到裸 #/console 会被同一条规则再送回
 * 后台，控制台永不可达。故分流落地即置标记，标记在场时裸入口按普通客户的
 * 偏好落地序处理；要后台则手敲 #/admin——显式直达从不走分流，不受本标记
 * 影响。标记随登出/新登录清除（只属于那一次会话），换账号重新享有首登分流。
 */
export const ADMIN_SPLIT_STORAGE_KEY = "wire.console.adminSplit";
/** 内部后台角色门禁（ID token roles claim；admin 双闸的前端半边）。 */
export const ADMIN_ROLE = "wire-admin";
/**
 * 静默续期等待上限（ms，wire-console-boot-ux）：iframe 沙箱等环境下
 * token 端点可能无限挂起——超时按续期失败处理（null，回落登录引导）。
 */
export const REFRESH_TIMEOUT_MS = 15_000;

// ---------------------------------------------------------------------------
// 配置（运行时下发；未配置 → 前端阻塞态，不发起假登录）
// ---------------------------------------------------------------------------

export async function fetchAuthConfig(): Promise<ConsoleAuthConfig> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/console/auth/config`, {
      headers: { Accept: "application/json" },
    });
  } catch (err) {
    throw new ConsoleAuthError(
      `network error fetching auth config: ${(err as Error).message}`,
    );
  }
  if (!response.ok) {
    throw new ConsoleAuthError(`auth config ${response.status}`, response.status);
  }
  return (await response.json()) as ConsoleAuthConfig;
}

// ---------------------------------------------------------------------------
// 会话存取（sessionStorage：关标签即失效；token 不落 localStorage）
// ---------------------------------------------------------------------------

export function loadSession(): ConsoleSession | null {
  try {
    const raw = sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as ConsoleSession;
    if (!parsed.idToken) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function saveSession(session: ConsoleSession): void {
  try {
    sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session));
  } catch {
    /* 存储不可用时仅本次会话生效 */
  }
}

export function clearSession(): void {
  try {
    sessionStorage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

export function isExpired(session: ConsoleSession, skewMs = 60_000): boolean {
  return session.expiresAt <= Date.now() + skewMs;
}

/** admin 导航/页面共用：会话是否带内部后台角色。 */
export function hasAdminRole(session: ConsoleSession | null): boolean {
  return !!session?.roles?.includes(ADMIN_ROLE);
}

/**
 * 标记本次会话的裸 #/console 角色分流已落地过（管理员落后台后不再二次分流，
 * 免得控制台恒不可达）。存储不可用时退化为「每次都分流」的旧行为。
 */
export function markAdminSplitDone(): void {
  try {
    sessionStorage.setItem(ADMIN_SPLIT_STORAGE_KEY, "1");
  } catch {
    /* 存储不可用时退化为每次分流（旧行为） */
  }
}

/** 裸 #/console 的角色分流是否已在本次会话落地过。 */
export function hasAdminSplitDone(): boolean {
  try {
    return sessionStorage.getItem(ADMIN_SPLIT_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

/** 清除分流落地标记（登出 / 新登录时：标记只属于那一次会话）。 */
export function clearAdminSplitDone(): void {
  try {
    sessionStorage.removeItem(ADMIN_SPLIT_STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

// ---------------------------------------------------------------------------
// PKCE 工具
// ---------------------------------------------------------------------------

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString(length = 32): string {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return base64Url(bytes);
}

async function sha256(input: string): Promise<Uint8Array> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(input),
  );
  return new Uint8Array(digest);
}

interface PkceState {
  state: string;
  verifier: string;
  nonce: string;
  returnTo: string;
}

function savePkce(value: PkceState): void {
  try {
    sessionStorage.setItem(PKCE_STORAGE_KEY, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}

function loadPkce(): PkceState | null {
  try {
    const raw = sessionStorage.getItem(PKCE_STORAGE_KEY);
    return raw ? (JSON.parse(raw) as PkceState) : null;
  } catch {
    return null;
  }
}

function dropPkce(): void {
  try {
    sessionStorage.removeItem(PKCE_STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

/** 登录回跳地址（必须与 Logto 应用登记的 redirect URI 完全一致）。 */
export function consoleCallbackUrl(): string {
  return `${window.location.origin}/console/callback`;
}

/** 组装 Logto authorize URL（PKCE 参数由调用方先 savePkce 落会话）。 */
async function buildAuthorizeUrl(
  config: ConsoleAuthConfig,
  pkce: PkceState,
): Promise<string> {
  const challenge = base64Url(await sha256(pkce.verifier));
  const params = new URLSearchParams({
    client_id: config.client_id,
    response_type: "code",
    scope: config.scopes.join(" "),
    redirect_uri: consoleCallbackUrl(),
    state: pkce.state,
    nonce: pkce.nonce,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  return `${config.issuer}/oidc/auth?${params.toString()}`;
}

/** 发起登录：生成 PKCE 参数落 sessionStorage 并跳转 Logto authorize。 */
export async function beginLogin(
  config: ConsoleAuthConfig,
  returnTo = "/console",
): Promise<void> {
  if (!config.configured) {
    // 阻塞态：未下发登录配置绝不发起认证跳转（console-auth spec 场景）。
    throw new ConsoleAuthError("login not configured on server", 503);
  }
  const pkce: PkceState = {
    state: randomString(16),
    verifier: randomString(48),
    nonce: randomString(16),
    returnTo,
  };
  savePkce(pkce);
  const url = await buildAuthorizeUrl(config, pkce);
  window.location.assign(url);
}

function claimString(
  claims: Record<string, unknown>,
  key: string,
): string | undefined {
  const value = claims[key];
  return typeof value === "string" && value.trim() ? value : undefined;
}

function claimList(claims: Record<string, unknown>, key: string): string[] {
  const value = claims[key];
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.map((item) => String(item));
  return [];
}

/** base64url 解码 JWT payload（仅展示用；校验在服务端）。 */
export function decodeJwtClaims(token: string): Record<string, unknown> {
  try {
    const payload = token.split(".")[1];
    if (!payload) return {};
    const padded = payload.replace(/-/g, "+").replace(/_/g, "/");
    const binary = atob(
      padded.padEnd(padded.length + ((4 - (padded.length % 4)) % 4), "="),
    );
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes)) as Record<string, unknown>;
  } catch {
    return {};
  }
}

interface TokenResponse {
  id_token: string;
  access_token?: string;
  refresh_token?: string;
  expires_in?: number;
}

function sessionFromTokens(tokens: TokenResponse): ConsoleSession {
  const claims = decodeJwtClaims(tokens.id_token);
  const identity =
    claimString(claims, "username") ??
    claimString(claims, "email") ??
    claimString(claims, "sub") ??
    "unknown";
  const expiresIn =
    typeof tokens.expires_in === "number" ? tokens.expires_in : 3600;
  return {
    idToken: tokens.id_token,
    refreshToken: tokens.refresh_token,
    expiresAt: Date.now() + expiresIn * 1000,
    identity,
    email: claimString(claims, "email"),
    name: claimString(claims, "name"),
    roles: claimList(claims, "roles"),
  };
}

async function tokenRequest(
  config: ConsoleAuthConfig,
  body: URLSearchParams,
): Promise<TokenResponse> {
  let response: Response;
  try {
    response = await fetch(`${config.issuer}/oidc/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
  } catch (err) {
    throw new ConsoleAuthError(
      `token endpoint unreachable: ${(err as Error).message}`,
    );
  }
  if (!response.ok) {
    let detail: string | undefined;
    try {
      const payload = (await response.json()) as {
        error_description?: string;
        error?: string;
      };
      detail = payload.error_description ?? payload.error;
    } catch {
      /* ignore */
    }
    throw new ConsoleAuthError(
      `token exchange failed (${response.status})`,
      response.status,
      detail,
    );
  }
  return (await response.json()) as TokenResponse;
}

// ---------------------------------------------------------------------------
// 会话建立（POST /api/console/session：sub→Customer 映射 + 首登自动开户）
// ---------------------------------------------------------------------------

interface SessionEstablishResponse {
  status: "ready" | "failed";
  customer?: ConsoleCustomerInfo;
  key_once?: string | null;
  reason?: string;
}

/**
 * 以 ID token 建立控制台会话：服务端本地校验令牌后解析 sub→Customer，
 * 无映射的首登 sub 自动触发开户 ceremony（free 档跳过 cell 配给）。
 * 200 ready → 客户信息 +（仅开户那次）明文 wgk- key；
 * 502 failed → 开户链路失败（reason 如实上抛，UI 呈现 + 可重试）；
 * 401 → 令牌无效/过期（调用方应清除本地会话要求重新登录）。
 */
export async function establishSession(
  idToken: string,
): Promise<SessionEstablishment> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/console/session`, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ id_token: idToken }),
    });
  } catch (err) {
    throw new ConsoleAuthError(
      `network error establishing session: ${(err as Error).message}`,
    );
  }
  if (response.status === 401) {
    throw new ConsoleAuthError("session rejected (401)", 401);
  }
  let payload: SessionEstablishResponse;
  try {
    payload = (await response.json()) as SessionEstablishResponse;
  } catch (err) {
    throw new ConsoleAuthError(
      `invalid session response: ${(err as Error).message}`,
      response.status,
    );
  }
  if (!response.ok || payload.status !== "ready" || !payload.customer) {
    throw new ConsoleAuthError(
      "session establishment failed",
      response.status,
      payload.reason,
    );
  }
  return { customer: payload.customer, keyOnce: payload.key_once ?? null };
}

/** 回调页：校验 state、兑换 code、落会话；返回 returnTo 路径。 */
export async function completeLogin(
  config: ConsoleAuthConfig,
  query: URLSearchParams,
): Promise<{ returnTo: string; session: ConsoleSession }> {
  const error = query.get("error");
  if (error) {
    throw new ConsoleAuthError(
      `login failed: ${error}`,
      400,
      query.get("error_description") ?? undefined,
    );
  }
  const code = query.get("code");
  const state = query.get("state");
  const pkce = loadPkce();
  if (!code || !state || !pkce || pkce.state !== state) {
    throw new ConsoleAuthError("login state mismatch or missing PKCE state", 400);
  }
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    client_id: config.client_id,
    redirect_uri: consoleCallbackUrl(),
    code_verifier: pkce.verifier,
  });
  const tokens = await tokenRequest(config, body);
  dropPkce();
  const session = sessionFromTokens(tokens);
  saveSession(session);
  // 新登录即重置分流标记：换账号登录仍享有首登分流（标记只属于那一次会话）。
  clearAdminSplitDone();
  return { returnTo: pkce.returnTo || "/console", session };
}

/** 过期前用 refresh token 静默续期；失败返回 null（前端显示重新登录）。 */
export async function refreshSession(
  config: ConsoleAuthConfig,
  session: ConsoleSession,
): Promise<ConsoleSession | null> {
  if (!session.refreshToken) return null;
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: session.refreshToken,
    client_id: config.client_id,
  });
  // 超时兜底：REFRESH_TIMEOUT_MS 内 token 端点未回即按续期失败处理，
  // 不再无限等待（挂起的浏览器环境此前表现为整页无反馈）。
  let timeoutHandle: ReturnType<typeof setTimeout> | undefined;
  try {
    const tokens = await Promise.race([
      tokenRequest(config, body),
      new Promise<never>((_, reject) => {
        timeoutHandle = setTimeout(
          () => reject(new ConsoleAuthError("refresh timed out")),
          REFRESH_TIMEOUT_MS,
        );
      }),
    ]);
    const next = sessionFromTokens({
      refresh_token: session.refreshToken,
      ...tokens,
    });
    // 续期不丢已回填的客户信息（plan_code 等与令牌无关）。
    const merged: ConsoleSession = { ...next, customer: session.customer };
    saveSession(merged);
    return merged;
  } catch {
    return null;
  } finally {
    // 先到者胜出后清掉另一侧定时器，避免迟到的 reject 变成未处理拒绝。
    if (timeoutHandle !== undefined) clearTimeout(timeoutHandle);
  }
}

export function logoutLocal(): void {
  clearSession();
  dropPkce();
  // 分流标记随登出清除：换账号登录重新享有首登分流（标记只属于那一次会话）。
  clearAdminSplitDone();
}
