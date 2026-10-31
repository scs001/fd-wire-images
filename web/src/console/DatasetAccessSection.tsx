// 数据集级访问授权面板（wire-dataset-acl D5 web 面）：数据集详情页内的
// 「访问」区块——owner-only（角色取既有团队面 GET /api/console/team 的
// me.role，editor/viewer 一律不渲染入口）。三块：
// - 模式切换 team（本队全员可见，默认）⇄ restricted（仅 owner 与显式授权
//   成员可见）；
// - 授权表（成员下拉取自既有团队成员名单，权限 view|edit，逐行移除）；
// - 保存 = PUT /api/v1/datasets/{id}/access **整表替换**
//   {mode, grants:[{logto_sub, permission}]}（mode=team 时 grants 空数组，
//   服务端同语义清空）。
// 判定语义只此一处（owner 恒有全权，不受名单约束）；本组件只做编辑面，
// 可见性/403 语义全由服务端单点裁决（前端不预判、不复制第二套授权规则）。
// i18n：console.dataset.acl.*（zh/en 键位对仗，i18nKeys.test 把关）。
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  fetchDatasetAccess,
  getTeam,
  updateDatasetAccess,
  type DatasetAccessInput,
  type DatasetAccessState,
  type DatasetAclMode,
  type DatasetAclPermission,
  type TeamMember,
  type TeamRole,
} from "./api";

/** sub 截短显示（前 8 位 + …；title 悬浮给全文）。 */
function shortSub(sub: string): string {
  return sub.length > 10 ? `${sub.slice(0, 8)}…` : sub;
}

/** 角色徽标 i18n 键（与团队页同一词表）。 */
const ROLE_KEYS: Record<TeamRole, string> = {
  owner: "console.team.roleBadge.owner",
  editor: "console.team.roleBadge.editor",
  viewer: "console.team.roleBadge.viewer",
};

/** 授权行草稿（编辑态；保存时按草稿整表提交）。 */
type GrantDraft = { logto_sub: string; permission: DatasetAclPermission };

export function DatasetAccessSection({ datasetId }: { datasetId: string }) {
  const { t } = useTranslation();
  // 角色源 = 既有团队面（GET /api/console/team me.role）；未拉到时按「不可见」
  // 处理（宁缺勿越权：owner-only 面板绝不向未知角色提前渲染）。
  const [role, setRole] = useState<TeamRole | null>(null);
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [state, setState] = useState<DatasetAccessState | null>(null);
  const [mode, setMode] = useState<DatasetAclMode>("team");
  const [grants, setGrants] = useState<GrantDraft[]>([]);
  const [newSub, setNewSub] = useState("");
  const [newPermission, setNewPermission] = useState<DatasetAclPermission>("view");
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);

  const isOwner = role === "owner";

  const load = useCallback(async () => {
    setLoadError(false);
    setLoading(true);
    try {
      const access = await fetchDatasetAccess(datasetId);
      setState(access);
      setMode(access.mode);
      setGrants(
        access.grants.map((grant) => ({
          logto_sub: grant.logto_sub,
          permission: grant.permission,
        })),
      );
    } catch {
      setState(null);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [datasetId]);

  // 角色与成员名单：owner 才继续读访问态（editor/viewer 零请求、零渲染）。
  useEffect(() => {
    let cancelled = false;
    setRole(null);
    setMembers([]);
    setState(null);
    setSaved(false);
    setOpError(null);
    setNewSub("");
    getTeam()
      .then((team) => {
        if (cancelled) return;
        setRole(team.me.role);
        setMembers(team.members);
        if (team.me.role === "owner") void load();
      })
      .catch(() => {
        if (cancelled) return;
        // 团队面失败 = 角色未知 → 面板不渲染（owner-only 面绝不猜角色）。
        setRole(null);
      });
    return () => {
      cancelled = true;
    };
  }, [datasetId, load]);

  /** 可被新授权的成员（排除已在名单里的 sub；owner 已恒有全权也排除）。 */
  const addableMembers = useMemo(() => {
    const granted = new Set(grants.map((grant) => grant.logto_sub));
    return members.filter(
      (member) => member.role !== "owner" && !granted.has(member.logtoSub),
    );
  }, [grants, members]);

  const memberBySub = useMemo(
    () => new Map(members.map((member) => [member.logtoSub, member])),
    [members],
  );

  if (!isOwner) return null;

  const addGrant = () => {
    if (newSub === "") return;
    if (grants.some((grant) => grant.logto_sub === newSub)) return;
    setGrants((prev) => [
      ...prev,
      { logto_sub: newSub, permission: newPermission },
    ]);
    setNewSub("");
    setNewPermission("view");
    setSaved(false);
  };

  const removeGrant = (sub: string) => {
    setGrants((prev) => prev.filter((grant) => grant.logto_sub !== sub));
    setSaved(false);
  };

  const save = async () => {
    if (saving) return;
    setSaving(true);
    setSaved(false);
    setOpError(null);
    // 整表替换：mode=team 时按契约提交空 grants（服务端同语义清空）。
    const payload: DatasetAccessInput = {
      mode,
      grants: mode === "restricted" ? grants : [],
    };
    try {
      const next = await updateDatasetAccess(datasetId, payload);
      setState(next);
      setMode(next.mode);
      setGrants(
        next.grants.map((grant) => ({
          logto_sub: grant.logto_sub,
          permission: grant.permission,
        })),
      );
      setSaved(true);
    } catch (err) {
      setOpError(apiErrorText(err, t("console.dataset.acl.saveError")));
    } finally {
      setSaving(false);
    }
  };

  const inputClass =
    "rounded border border-slate-300 px-2 py-1 text-xs disabled:cursor-not-allowed disabled:bg-slate-100";

  return (
    <section data-testid="dataset-access-section" className="mt-6">
      <h2 className="text-base font-semibold">{t("console.dataset.acl.title")}</h2>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.dataset.acl.hint")}
      </p>

      {loading && (
        <p data-testid="dataset-access-loading" className="mt-2 text-xs text-slate-400">
          {t("console.dataset.acl.loading")}
        </p>
      )}
      {loadError && (
        <div
          data-testid="dataset-access-load-error"
          className="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1.5 text-xs text-red-700"
        >
          {t("console.dataset.acl.loadError")}
          <button
            type="button"
            data-testid="dataset-access-retry"
            onClick={() => void load()}
            className="ml-2 rounded border border-red-300 bg-white px-2 py-0.5 text-[11px] text-red-700 hover:bg-red-100"
          >
            {t("console.dataset.acl.retry")}
          </button>
        </div>
      )}

      {state != null && (
        <>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <span className="text-xs text-slate-500">
              {t("console.dataset.acl.modeLabel")}
            </span>
            <div className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-slate-50/60 p-1">
              {(["team", "restricted"] as DatasetAclMode[]).map((value) => (
                <button
                  key={value}
                  type="button"
                  data-testid={`dataset-access-mode-${value}`}
                  data-mode={value}
                  aria-pressed={mode === value}
                  disabled={saving}
                  onClick={() => {
                    setMode(value);
                    setSaved(false);
                  }}
                  className={`rounded-md px-2.5 py-1 text-xs transition-colors ${
                    mode === value
                      ? "bg-slate-900 text-white"
                      : "text-slate-600 hover:bg-slate-100"
                  }`}
                >
                  {t(`console.dataset.acl.modes.${value}`)}
                </button>
              ))}
            </div>
          </div>

          {mode === "team" ? (
            <p
              data-testid="dataset-access-team-hint"
              className="mt-2 text-xs text-slate-500"
            >
              {t("console.dataset.acl.teamHint")}
            </p>
          ) : (
            <div
              data-testid="dataset-access-grants"
              className="mt-2 rounded border border-slate-200 bg-white p-2"
            >
              <p className="text-[11px] text-slate-500">
                {t("console.dataset.acl.grantsHint")}
              </p>
              {grants.length === 0 ? (
                <p
                  data-testid="dataset-access-grants-empty"
                  className="mt-1 text-[11px] text-slate-400"
                >
                  {t("console.dataset.acl.grantsEmpty")}
                </p>
              ) : (
                <table className="mt-1 w-full text-xs">
                  <thead>
                    <tr className="text-left text-[11px] text-slate-400">
                      <th className="py-1 font-normal">
                        {t("console.dataset.acl.memberLabel")}
                      </th>
                      <th className="py-1 font-normal">
                        {t("console.dataset.acl.permissionLabel")}
                      </th>
                      <th className="py-1 font-normal">
                        {t("console.dataset.acl.actionsLabel")}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {grants.map((grant) => (
                      <tr
                        key={grant.logto_sub}
                        data-testid="dataset-access-grant-row"
                        data-sub={grant.logto_sub}
                      >
                        <td className="py-1">
                          <span
                            title={grant.logto_sub}
                            className="font-mono text-[11px] text-slate-700"
                          >
                            {shortSub(grant.logto_sub)}
                          </span>
                          {memberBySub.get(grant.logto_sub) && (
                            <span className="ml-1.5 text-[11px] text-slate-400">
                              {t(
                                ROLE_KEYS[
                                  memberBySub.get(grant.logto_sub)!.role
                                ],
                              )}
                            </span>
                          )}
                        </td>
                        <td className="py-1">
                          <select
                            data-testid="dataset-access-grant-permission"
                            data-sub={grant.logto_sub}
                            value={grant.permission}
                            disabled={saving}
                            onChange={(e) => {
                              const permission = e.target
                                .value as DatasetAclPermission;
                              setGrants((prev) =>
                                prev.map((row) =>
                                  row.logto_sub === grant.logto_sub
                                    ? { ...row, permission }
                                    : row,
                                ),
                              );
                              setSaved(false);
                            }}
                            className={inputClass}
                          >
                            {(["view", "edit"] as DatasetAclPermission[]).map(
                              (permission) => (
                                <option key={permission} value={permission}>
                                  {t(`console.dataset.acl.permissions.${permission}`)}
                                </option>
                              ),
                            )}
                          </select>
                        </td>
                        <td className="py-1">
                          <button
                            type="button"
                            data-testid="dataset-access-grant-remove"
                            data-sub={grant.logto_sub}
                            disabled={saving}
                            onClick={() => removeGrant(grant.logto_sub)}
                            className="rounded border border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-30"
                          >
                            {t("console.dataset.acl.remove")}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              <div className="mt-2 flex flex-wrap items-center gap-2">
                <select
                  data-testid="dataset-access-member-select"
                  value={newSub}
                  disabled={saving || addableMembers.length === 0}
                  onChange={(e) => setNewSub(e.target.value)}
                  className={inputClass}
                >
                  <option value="">
                    {t("console.dataset.acl.memberPlaceholder")}
                  </option>
                  {addableMembers.map((member) => (
                    <option key={member.logtoSub} value={member.logtoSub}>
                      {shortSub(member.logtoSub)} ·{" "}
                      {t(ROLE_KEYS[member.role])}
                    </option>
                  ))}
                </select>
                <select
                  data-testid="dataset-access-member-permission"
                  value={newPermission}
                  disabled={saving}
                  onChange={(e) =>
                    setNewPermission(e.target.value as DatasetAclPermission)
                  }
                  className={inputClass}
                >
                  {(["view", "edit"] as DatasetAclPermission[]).map(
                    (permission) => (
                      <option key={permission} value={permission}>
                        {t(`console.dataset.acl.permissions.${permission}`)}
                      </option>
                    ),
                  )}
                </select>
                <button
                  type="button"
                  data-testid="dataset-access-grant-add"
                  disabled={saving || newSub === ""}
                  onClick={addGrant}
                  className="rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-30"
                >
                  {t("console.dataset.acl.add")}
                </button>
                {addableMembers.length === 0 && (
                  <span
                    data-testid="dataset-access-no-members"
                    className="text-[11px] text-slate-400"
                  >
                    {t("console.dataset.acl.noMembers")}
                  </span>
                )}
              </div>
            </div>
          )}

          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              data-testid="dataset-access-save"
              disabled={saving}
              onClick={() => void save()}
              className="rounded bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {saving
                ? t("console.dataset.acl.saving")
                : t("console.dataset.acl.save")}
            </button>
            {saved && (
              <span
                data-testid="dataset-access-saved"
                className="text-xs text-emerald-600"
              >
                {t("console.dataset.acl.saved")}
              </span>
            )}
            {opError && (
              <span
                data-testid="dataset-access-error"
                className="text-xs text-red-600"
              >
                {opError}
              </span>
            )}
          </div>
        </>
      )}
    </section>
  );
}
