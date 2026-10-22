// 控制台「团队」页（#/console/team，wire-team-spaces 任务 5 + v2）。
// 成员制管理面，按本人角色分视角（me.role 来自 GET /api/console/team）：
// - owner：成员名单（sub 截短 + 角色徽标 + 加入时间 + 角色就地变更
//   editor↔viewer（PATCH）+ 移除按钮；owner 行徽标「所有者」不可撤不
//   可改）+ 席位用量条（used/limit；超配 grandfather 警示态——琥珀色 +
//   「移除成员前无法邀新成员」）+ 邀请区（席位二选一 → 生成 → **一次性
//   码醒目整段展示 + 「仅此一次显示」提示 + 复制按钮**；满/超配时按钮
//   禁用 + 升级引导，409 后端 detail 如实呈现）+ 待用邀请列表（码/角色/
//   过期倒计时 + 作废按钮）；
// - editor/viewer：只读名单 + 页顶自己的角色徽标说明（viewer 另加
//   「只读席位：变更操作不可用」文案）——零变更按钮渲染。
// 「我的团队」切换面（v2）：GET /api/console/teams 多队身份（长度 > 1）
// 才在页顶渲染队清单卡（名称/角色徽标/当前标记/切换按钮）——单队零
// 打扰；切换成功后整页 reload（切换 = 切数据面，所有缓存面一致重建）。
// 错误处理照既有页（失败条 + 重试）；403 单列「无权」提示（amber 块，
// 绝不崩溃）——后端 detail 如实附注。数据经 api.ts 边界翻译（UI 侧
// camelCase）。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ConsoleApiError,
  TEAM_INVITE_ROLES,
  apiErrorText,
  createTeamInvite,
  getTeam,
  getTeams,
  removeTeamMember,
  revokeTeamInvite,
  switchTeam,
  updateTeamMemberRole,
  type TeamInviteCreated,
  type TeamInviteRole,
  type TeamOverview,
  type TeamRole,
  type TeamsOut,
} from "./api";

/** sub 截短显示（前 8 位 + …；title 悬浮给全文）。 */
export function shortSub(sub: string): string {
  return sub.length > 10 ? `${sub.slice(0, 8)}…` : sub;
}

/** 加入时间呈现（naive UTC ISO → "YYYY-MM-DD HH:mm"；坏值原样）。 */
export function formatJoinedAt(iso: string): string {
  return iso ? iso.slice(0, 16).replace("T", " ") : iso;
}

/** 邀请剩余有效期（毫秒差 → 时/分；坏值 null 交调用方原样呈现）。 */
export function inviteRemaining(
  expiresAt: string,
  now = Date.now(),
): { hours: number; minutes: number } | null {
  const remainMs = new Date(expiresAt).getTime() - now;
  if (Number.isNaN(remainMs) || remainMs <= 0) return null;
  const minutes = Math.ceil(remainMs / 60_000);
  return { hours: Math.floor(minutes / 60), minutes: minutes % 60 };
}

/** 角色徽标 i18n 键。 */
const ROLE_KEYS: Record<TeamRole, string> = {
  owner: "console.team.roleBadge.owner",
  editor: "console.team.roleBadge.editor",
  viewer: "console.team.roleBadge.viewer",
};

export function TeamPage() {
  const { t } = useTranslation();
  const [team, setTeam] = useState<TeamOverview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // 403 无权态（amber 提示块，与普通失败条分流——不崩溃）。
  const [forbidden, setForbidden] = useState(false);
  // 邀请区状态（owner 视角）。
  const [inviteRole, setInviteRole] = useState<TeamInviteRole>("editor");
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<TeamInviteCreated | null>(null);
  // 变更类动作错误（生成/作废/移除/改角色共用，单行如实呈现后端 detail）。
  const [actionError, setActionError] = useState<string | null>(null);
  // 飞行中的作废/移除/改角色目标（禁重复提交）。
  const [revokingInviteId, setRevokingInviteId] = useState<number | null>(null);
  const [removingSub, setRemovingSub] = useState<string | null>(null);
  const [changingRoleSub, setChangingRoleSub] = useState<string | null>(null);
  // 「我的团队」清单（wire-team-spaces-v2）：独立于 GET /team（多队切换面
  // 对 editor/viewer 同样可用）；拉取失败静默降级（不渲染切换面）。
  const [teams, setTeams] = useState<TeamsOut | null>(null);
  // 飞行中的切换目标 + 切换失败（行内如实呈现后端 detail，如 404 目标无效）。
  const [switchingTeamId, setSwitchingTeamId] = useState<number | null>(null);
  const [switchError, setSwitchError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    setForbidden(false);
    try {
      setTeam(await getTeam());
    } catch (err) {
      // 403 = 无权查看（角色门禁/身份漂移）——amber 提示而非红色崩溃条。
      if (err instanceof ConsoleApiError && err.status === 403) {
        setForbidden(true);
        setLoadError(
          err.detail ?? t("console.team.forbiddenTitle"),
        );
      } else {
        setLoadError(apiErrorText(err, t("console.team.errorTitle")));
      }
    }
  }, [t]);

  const loadTeams = useCallback(async () => {
    try {
      setTeams(await getTeams());
    } catch {
      // 静默降级：清单拉不到就不渲染切换面（零打扰），绝不阻塞团队页。
      setTeams(null);
    }
  }, []);

  useEffect(() => {
    void load();
    void loadTeams();
  }, [load, loadTeams]);

  /** owner 生成邀请：201 后一次性码整段醒目展示（仅此一次）。 */
  const generateInvite = async () => {
    if (creating) return;
    setCreating(true);
    setActionError(null);
    setCreated(null);
    try {
      setCreated(await createTeamInvite(inviteRole));
      // 名单/待用邀请同步重载（新邀请入列）。
      await load();
    } catch (err) {
      setActionError(apiErrorText(err, t("console.team.invite.failed")));
    } finally {
      setCreating(false);
    }
  };

  /** owner 作废邀请（不动一次性码展示——那是新码的唯一复制窗口）。 */
  const revokeInvite = async (id: number) => {
    if (revokingInviteId != null) return;
    setRevokingInviteId(id);
    setActionError(null);
    try {
      await revokeTeamInvite(id);
      await load();
    } catch (err) {
      setActionError(apiErrorText(err, t("console.team.invites.revokeFailed")));
    } finally {
      setRevokingInviteId(null);
    }
  };

  /** owner 移除成员（owner 行不渲染按钮——409 防线在服务端）。 */
  const removeMember = async (sub: string) => {
    if (removingSub != null) return;
    setRemovingSub(sub);
    setActionError(null);
    try {
      await removeTeamMember(sub);
      await load();
    } catch (err) {
      setActionError(apiErrorText(err, t("console.team.members.removeFailed")));
    } finally {
      setRemovingSub(null);
    }
  };

  /** owner 就地变更成员角色 editor↔viewer（PATCH 成功后重载名单刷新行内
   *  角色；owner 行不渲染控件——409 防线在服务端）。 */
  const changeMemberRole = async (sub: string, role: TeamInviteRole) => {
    if (changingRoleSub != null) return;
    setChangingRoleSub(sub);
    setActionError(null);
    try {
      await updateTeamMemberRole(sub, role);
      await load();
    } catch (err) {
      setActionError(
        apiErrorText(err, t("console.team.members.roleChangeFailed")),
      );
    } finally {
      setChangingRoleSub(null);
    }
  };

  /** 切换活跃队（POST switch 幂等）：成功后整页 reload——切换 = 切数据面，
   *  控制台各页缓存一并重建（404 等失败行内如实呈现后端 detail）。 */
  const switchTo = async (customerId: number) => {
    if (switchingTeamId != null) return;
    setSwitchingTeamId(customerId);
    setSwitchError(null);
    try {
      await switchTeam(customerId);
      window.location.reload();
    } catch (err) {
      setSwitchError(apiErrorText(err, t("console.team.switcher.failed")));
      setSwitchingTeamId(null);
    }
  };

  const isOwner = team?.me.role === "owner";
  const myRole = team?.me.role;
  // 席位门（wire-team-spaces-v2）：满/超配停邀新（grandfather 不踢人）；
  // 超配另呈警示态（琥珀色用量 + 提示文案）。
  const seatsFull = team != null && team.seatsUsed >= team.seatsLimit;
  const overQuota = team != null && team.seatsUsed > team.seatsLimit;

  return (
    <div data-testid="team-page">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("console.team.title")}</h1>
          <p className="mt-1 max-w-2xl text-xs text-slate-500">
            {t("console.team.subtitle")}
          </p>
        </div>
        {/* 页顶自己的角色徽标说明（三视角同位；viewer 追加只读席位注记）。 */}
        {myRole && (
          <div className="flex flex-col items-end gap-1">
            <span
              data-testid="team-my-role-badge"
              data-role={myRole}
              className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs text-slate-700 ring-1 ring-slate-200"
            >
              {t("console.team.myRole")}：{t(ROLE_KEYS[myRole])}
            </span>
            {myRole === "viewer" && (
              <span
                data-testid="team-viewer-note"
                className="text-[11px] text-slate-400"
              >
                {t("console.team.viewerNote")}
              </span>
            )}
          </div>
        )}
      </div>

      {/* ── 「我的团队」切换面（wire-team-spaces-v2）：多队身份（长度 > 1）
          才渲染——单队零打扰；切换成功后整页 reload（切数据面）。── */}
      {teams != null && teams.teams.length > 1 && (
        <section
          data-testid="team-switcher"
          className="mt-4 rounded-lg border border-slate-200 bg-white p-4"
        >
          <h2 className="text-sm font-semibold text-slate-700">
            {t("console.team.switcher.title")}
          </h2>
          <p className="mt-1 text-xs text-slate-500">
            {t("console.team.switcher.subtitle")}
          </p>
          <ul data-testid="team-switcher-list" className="mt-2 space-y-1.5">
            {teams.teams.map((membership) => {
              const active = membership.customerId === teams.activeCustomerId;
              return (
                <li
                  key={membership.customerId}
                  data-testid="team-switcher-row"
                  data-customer-id={membership.customerId}
                  className={`flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border px-3 py-2 ${
                    active
                      ? "border-slate-300 bg-slate-50"
                      : "border-slate-200 bg-white"
                  }`}
                >
                  <span className="text-sm font-medium text-slate-800">
                    {membership.customerName}
                  </span>
                  <span
                    data-testid="team-switcher-role-badge"
                    data-role={membership.role}
                    className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 ring-1 ring-slate-200"
                  >
                    {t(ROLE_KEYS[membership.role])}
                  </span>
                  {active ? (
                    <span
                      data-testid="team-active-badge"
                      className="ml-auto rounded-full bg-slate-900 px-2 py-0.5 text-xs text-white"
                    >
                      {t("console.team.switcher.current")}
                    </span>
                  ) : (
                    <button
                      type="button"
                      data-testid={`team-switch-${membership.customerId}`}
                      disabled={switchingTeamId != null}
                      onClick={() => void switchTo(membership.customerId)}
                      className="ml-auto rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {switchingTeamId === membership.customerId
                        ? t("console.team.switcher.switching")
                        : t("console.team.switcher.switchTo")}
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
          {switchError && (
            <p
              data-testid="team-switch-error"
              className="mt-2 break-all text-xs text-red-600"
            >
              {switchError}
            </p>
          )}
        </section>
      )}

      {/* 403 无权：amber 提示块（不崩溃）+ 重试。 */}
      {forbidden && (
        <div
          data-testid="team-forbidden"
          className="mt-4 rounded-md border border-amber-200 bg-amber-50/60 p-4"
        >
          <p className="text-sm font-medium text-amber-800">
            {t("console.team.forbiddenTitle")}
          </p>
          <p className="mt-1 text-xs text-slate-600">
            {t("console.team.forbiddenBody")}
          </p>
          {loadError && loadError !== t("console.team.forbiddenTitle") && (
            <p className="mt-1 break-all font-mono text-[11px] text-amber-700">
              {loadError}
            </p>
          )}
          <button
            type="button"
            data-testid="team-forbidden-retry"
            onClick={() => void load()}
            className="mt-2 rounded border border-amber-300 px-2 py-0.5 text-xs text-amber-800 hover:bg-amber-100"
          >
            {t("console.team.retry")}
          </button>
        </div>
      )}

      {/* 普通 load 失败：红色失败条 + 重试（照既有页范式）。 */}
      {loadError && !forbidden && (
        <div
          data-testid="team-error"
          className="mt-4 rounded-md border border-red-200 bg-red-50 p-4"
        >
          <p className="text-sm text-red-700">{t("console.team.errorTitle")}</p>
          {loadError !== t("console.team.errorTitle") && (
            <p className="mt-1 break-all font-mono text-[11px] text-red-500">
              {loadError}
            </p>
          )}
          <button
            type="button"
            data-testid="team-retry"
            onClick={() => void load()}
            className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.team.retry")}
          </button>
        </div>
      )}

      {team == null && !loadError && (
        <p className="mt-6 text-xs text-slate-400" data-testid="team-loading">
          {t("console.team.loading")}
        </p>
      )}

      {team != null && (
        <>
          {/* ── 成员名单（三视角同渲染；owner 才有改角色/移除按钮）── */}
          <section className="mt-5" data-testid="team-members-section">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <h2 className="text-sm font-semibold text-slate-700">
                {t("console.team.members.title")}
              </h2>
              {/* 席位用量条（v2）：used / limit；超配 grandfather 警示态。 */}
              <span
                data-testid="team-seats-usage"
                data-over-quota={overQuota ? "true" : "false"}
                className={`text-xs ${
                  overQuota
                    ? "font-medium text-amber-700"
                    : "text-slate-500"
                }`}
              >
                {t("console.team.seats.usage", {
                  used: team.seatsUsed,
                  limit: team.seatsLimit,
                })}
              </span>
              {overQuota && (
                <span
                  data-testid="team-seats-over-quota"
                  className="text-xs text-amber-700"
                >
                  {t("console.team.seats.overQuota")}
                </span>
              )}
            </div>
            <ul data-testid="team-member-list" className="mt-2 space-y-2">
              {team.members.map((member) => {
                const removable = isOwner && member.role !== "owner";
                return (
                  <li
                    key={member.logtoSub}
                    data-testid="team-member-row"
                    data-sub={member.logtoSub}
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-slate-200 bg-white px-4 py-3"
                  >
                    <span
                      data-testid="team-member-sub"
                      title={member.logtoSub}
                      className="font-mono text-sm text-slate-800"
                    >
                      {shortSub(member.logtoSub)}
                    </span>
                    <span
                      data-testid="team-member-role-badge"
                      data-role={member.role}
                      className={`rounded-full px-2 py-0.5 text-xs ring-1 ${
                        member.role === "owner"
                          ? "bg-indigo-50 text-indigo-700 ring-indigo-100"
                          : "bg-slate-100 text-slate-600 ring-slate-200"
                      }`}
                    >
                      {t(ROLE_KEYS[member.role])}
                    </span>
                    <span
                      data-testid="team-member-joined"
                      className="text-xs text-slate-400"
                    >
                      {t("console.team.members.joinedAt", {
                        at: formatJoinedAt(member.createdAt),
                      })}
                    </span>
                    {/* 角色就地变更 editor↔viewer（v2，owner-only；owner 行
                        不渲染——409 防线在服务端）；成功后重载就地刷新。 */}
                    {removable && (
                      <select
                        data-testid={`team-role-change-${member.logtoSub}`}
                        data-sub={member.logtoSub}
                        aria-label={t("console.team.members.roleChangeLabel")}
                        title={t("console.team.members.roleChangeLabel")}
                        value={member.role}
                        disabled={changingRoleSub === member.logtoSub}
                        onChange={(e) =>
                          void changeMemberRole(
                            member.logtoSub,
                            e.target.value as TeamInviteRole,
                          )
                        }
                        className="ml-auto rounded-md border border-slate-300 px-1.5 py-1 text-xs text-slate-700 disabled:opacity-50"
                      >
                        {TEAM_INVITE_ROLES.map((role) => (
                          <option key={role} value={role}>
                            {t(ROLE_KEYS[role])}
                          </option>
                        ))}
                      </select>
                    )}
                    {removable ? (
                      <button
                        type="button"
                        data-testid="team-member-remove"
                        data-sub={member.logtoSub}
                        disabled={removingSub != null}
                        onClick={() => void removeMember(member.logtoSub)}
                        className="rounded border border-rose-300 px-2.5 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {removingSub === member.logtoSub
                          ? t("console.team.members.removing")
                          : t("console.team.members.remove")}
                      </button>
                    ) : (
                      <span className="ml-auto" />
                    )}
                  </li>
                );
              })}
            </ul>
          </section>

          {/* ── 邀请区 + 待用邀请（owner-only；editor/viewer 不渲染）── */}
          {isOwner && (
            <section className="mt-8" data-testid="team-invite-section">
              <h2 className="text-sm font-semibold text-slate-700">
                {t("console.team.invite.title")}
              </h2>
              <p className="mt-1 max-w-2xl text-xs text-slate-500">
                {t("console.team.invite.subtitle")}
              </p>
              <div className="mt-2 flex flex-wrap items-end gap-3">
                <div>
                  <label
                    htmlFor="team-invite-role"
                    className="block text-[11px] text-slate-500"
                  >
                    {t("console.team.invite.roleLabel")}
                  </label>
                  <select
                    id="team-invite-role"
                    data-testid="team-invite-role-select"
                    value={inviteRole}
                    disabled={creating}
                    onChange={(e) =>
                      setInviteRole(e.target.value as TeamInviteRole)
                    }
                    className="mt-1 w-36 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
                  >
                    {TEAM_INVITE_ROLES.map((role) => (
                      <option key={role} value={role}>
                        {t(ROLE_KEYS[role])}
                      </option>
                    ))}
                  </select>
                </div>
                <button
                  type="button"
                  data-testid="team-invite-cta"
                  disabled={creating || seatsFull}
                  title={seatsFull ? t("console.team.invite.fullHint") : undefined}
                  onClick={() => void generateInvite()}
                  className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {creating
                    ? t("console.team.invite.creating")
                    : t("console.team.invite.submit")}
                </button>
              </div>

              {/* 满/超配引导（v2）：移除成员或升级档位（grandfather 不踢人）。 */}
              {seatsFull && (
                <p
                  data-testid="team-invite-full-hint"
                  className="mt-2 text-xs text-amber-700"
                >
                  {t("console.team.invite.fullHint")}
                </p>
              )}

              {/* 一次性码：醒目整段展示 + 仅此一次提示 + 复制（照 keyOnce 范式）。 */}
              {created && (
                <div
                  data-testid="team-invite-once"
                  className="mt-3 rounded-lg border border-emerald-200 bg-emerald-50/60 p-4"
                >
                  <h3 className="text-sm font-semibold text-emerald-800">
                    {t("console.team.invite.onceTitle")}
                  </h3>
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <code
                      data-testid="team-invite-code"
                      className="break-all rounded bg-white px-3 py-1.5 font-mono text-base tracking-wider text-slate-800 ring-1 ring-emerald-100"
                    >
                      {created.code}
                    </code>
                    <button
                      type="button"
                      data-testid="team-invite-copy"
                      onClick={() =>
                        void navigator.clipboard?.writeText(created.code)
                      }
                      className="rounded border border-emerald-300 bg-white px-2 py-1 text-xs text-emerald-700 hover:bg-emerald-100"
                    >
                      {t("console.team.invite.copy")}
                    </button>
                  </div>
                  <p
                    data-testid="team-invite-once-hint"
                    className="mt-2 text-xs text-emerald-700"
                  >
                    {t("console.team.invite.onceHint")}
                  </p>
                </div>
              )}

              {/* 待用邀请列表：码 / 角色 / 过期倒计时 + 作废。 */}
              <h3 className="mt-5 text-xs font-semibold text-slate-600">
                {t("console.team.invites.title")}
              </h3>
              {team.invites.length === 0 ? (
                <p
                  data-testid="team-invites-empty"
                  className="mt-1.5 text-xs text-slate-400"
                >
                  {t("console.team.invites.empty")}
                </p>
              ) : (
                <ul data-testid="team-invite-list" className="mt-2 space-y-1.5">
                  {team.invites.map((invite) => {
                    const remaining = inviteRemaining(invite.expiresAt);
                    return (
                      <li
                        key={invite.id}
                        data-testid="team-invite-row"
                        data-invite-id={invite.id}
                        className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-slate-200 bg-white px-3 py-2 text-xs text-slate-700"
                      >
                        <code
                          data-testid="team-invite-row-code"
                          className="font-mono text-slate-800"
                        >
                          {invite.code}
                        </code>
                        <span
                          data-testid="team-invite-role-badge"
                          data-role={invite.role}
                          className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 ring-1 ring-slate-200"
                        >
                          {t(ROLE_KEYS[invite.role])}
                        </span>
                        <span
                          data-testid="team-invite-remaining"
                          data-expired={remaining == null ? "true" : "false"}
                          className="text-slate-400"
                        >
                          {remaining == null
                            ? t("console.team.invites.expired")
                            : t("console.team.invites.remaining", {
                                hours: remaining.hours,
                                minutes: remaining.minutes,
                              })}
                        </span>
                        <button
                          type="button"
                          data-testid="team-invite-revoke"
                          data-invite-id={invite.id}
                          disabled={revokingInviteId != null}
                          onClick={() => void revokeInvite(invite.id)}
                          className="ml-auto rounded border border-rose-300 px-2 py-0.5 text-xs text-rose-700 hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {revokingInviteId === invite.id
                            ? t("console.team.invites.revoking")
                            : t("console.team.invites.revoke")}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
          )}

          {/* 变更类动作失败：单行如实呈现（403 detail / 409 owner 不可撤等）。 */}
          {actionError && (
            <p
              data-testid="team-action-error"
              className="mt-3 break-all text-xs text-red-600"
            >
              {actionError}
            </p>
          )}
        </>
      )}
    </div>
  );
}
