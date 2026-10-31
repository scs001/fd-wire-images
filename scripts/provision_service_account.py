#!/usr/bin/env python3
"""服务账号发号（change: add-fleet-service-accounts）。

两库统一经 ssh + docker exec psql 操作，本机不落任何口令/DSN：
- Logto 库：ssh $FD_SVC_XINRU（默认 ubuntu@100.64.0.3）→ docker exec fd-postgres
- wire 平台库：ssh $FD_SVC_CHEAP3（默认 root@100.64.0.12）→ docker exec wire-postgres

子命令：
  ensure-org              建/复用 svc-fleet 组织（幂等）
  provision <key>         发号（幂等）：key 形如 fleet-core-1 →
                          svc-fleet-core-1@finddatatech.cloud + org 成员 +
                          billing_customers(name='svc/<key>', logto_sub, plan free)
  retire <email>          停用（is_suspended）+ 关闭对应服务客户（幂等）
  probe                   从 stdin 读「email 密码」，走 Logto experience API 真实登录验证
  guard [--hours N]       svc-fleet 成员近期登录审计（browser 类=告警）

红线：凭据只打印一次、只进 docs/TEST-ACCOUNTS.md「服务账号」节；不进代码/日志/报告。
"""
from __future__ import annotations

import base64


import json
import re
import secrets
import string
import subprocess
import sys
from datetime import datetime, timezone

XINRU = __import__("os").environ.get("FD_SVC_XINRU", "ubuntu@100.64.0.3")
CHEAP3 = __import__("os").environ.get("FD_SVC_CHEAP3", "root@100.64.0.12")
ORG_NAME = "svc-fleet"
EMAIL_DOMAIN = "finddatatech.cloud"
PLAN_FREE_ID = 4  # billing_plans.code='free'
KEY_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")  # fleet-core-1
ID_ALPHABET = string.ascii_lowercase + string.digits


def logto_psql(sql: str) -> str:
    cmd = ["ssh", "-o", "BatchMode=yes", XINRU,
           "sudo docker exec fd-postgres psql -U logto -d logto -Atc " + _shq(sql)]
    return _run(cmd)


def wire_psql(sql: str) -> str:
    cmd = ["ssh", "-o", "BatchMode=yes", CHEAP3,
           "sudo docker exec wire-postgres psql -U wire -d wire_platform -Atc " + _shq(sql)]
    return _run(cmd)


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        raise SystemExit(f"remote command failed: {' '.join(cmd[:2])}…")
    return r.stdout.strip()


def nanoid12(existing: set[str]) -> str:
    while True:
        nid = "".join(secrets.choice(ID_ALPHABET) for _ in range(12))
        if nid not in existing:
            return nid


def argon2i_hash(password: str) -> str:
    # 与 Logto 在册格式对齐：$argon2i$v=19$m=8192,t=8,p=1$<salt22>$<hash43>
    salt = secrets.token_bytes(16)
    import argon2.low_level as ll

    raw = ll.hash_secret_raw(
        secret=password.encode(), salt=salt, time_cost=8, memory_cost=8192,
        parallelism=1, hash_len=32, type=ll.Type.I,
    )
    b64 = lambda b: base64.b64encode(b).decode().rstrip("=")
    return f"$argon2i$v=19$m=8192,t=8,p=1${b64(salt)}${b64(raw)}"


def org_id() -> str | None:
    out = logto_psql(f"select id from organizations where name='{ORG_NAME}' and tenant_id='default'")
    return out or None


def cmd_ensure_org() -> None:
    oid = org_id()
    if oid:
        print(f"org-exists {ORG_NAME} id={oid}")
        return
    existing = {r for r in logto_psql("select id from organizations").splitlines() if r}
    nid = nanoid12(existing)
    logto_psql(
        "insert into organizations (tenant_id, id, name, description, is_mfa_required) "
        f"values ('default', '{nid}', '{ORG_NAME}', 'fleet service accounts (machine-only)', false)"
    )
    print(f"org-created {ORG_NAME} id={nid}")


def cmd_provision(key: str) -> None:
    if not KEY_RE.match(key):
        raise SystemExit(f"bad key {key!r}: 期望小写字母数字与连字符，如 fleet-core-1")
    email = f"svc-{key}@{EMAIL_DOMAIN}"
    username = f"svc-{key}"
    oid = org_id() or _die("先跑 ensure-org")

    existing = logto_psql(
        f"select id, is_suspended from users where primary_email='{email}'"
    )
    if existing:
        uid, susp = (existing.split("|") + [""])[:2]
        _reconcile(uid, email, oid, key)
        print(f"exists email={email} sub={uid} suspended={susp or 'f'}（幂等：不改凭据）")
        return

    all_ids = {r for r in logto_psql("select id from users where tenant_id='default'").splitlines() if r}
    uid = nanoid12(all_ids)
    password = secrets.token_urlsafe(18)
    h = argon2i_hash(password)
    logto_psql(
        "insert into users (tenant_id, id, username, primary_email, password_encrypted, "
        "password_encryption_method, is_suspended, password_updated_at, created_at, updated_at) "
        f"values ('default', '{uid}', '{username}', '{email}', '{h}', 'Argon2i', false, now(), now(), now())"
    )
    logto_psql(
        "insert into organization_user_relations (tenant_id, organization_id, user_id) "
        f"values ('default', '{oid}', '{uid}')"
    )
    _reconcile(uid, email, oid, key)
    roles = logto_psql(f"select count(*) from users_roles where user_id='{uid}'")
    assert roles == "0", f"角色数异常: {roles}"
    print(json.dumps({"email": email, "password": password, "sub": uid,
                      "org": f"{ORG_NAME}({oid})", "customer": f"svc/{key}"},
                     ensure_ascii=False))
    print("⚠ 凭据只此一次：立即抄入 docs/TEST-ACCOUNTS.md 服务账号节，勿入其他任何面")


def _reconcile(uid: str, email: str, oid: str, key: str) -> None:
    """补齐缺失的 org 成员 / 服务客户行（幂等修复位）。"""
    in_org = logto_psql(
        f"select count(*) from organization_user_relations where organization_id='{oid}' and user_id='{uid}'"
    )
    if in_org == "0":
        logto_psql(
            "insert into organization_user_relations (tenant_id, organization_id, user_id) "
            f"values ('default', '{oid}', '{uid}')"
        )
    cust = wire_psql(f"select id, status from billing_customers where logto_sub='{uid}'")
    if not cust:
        name = f"svc/{key}"
        dup = wire_psql(f"select id from billing_customers where name='{name}'")
        if dup:
            wire_psql(f"update billing_customers set logto_sub='{uid}' where id={dup}")
            print(f"customer-linked name={name} id={dup}（重名行补挂 sub）")
        else:
            wire_psql(
                "insert into billing_customers (name, sub2api_user_id, logto_sub, plan_id, status, created_at) "
                f"values ('{name}', null, '{uid}', {PLAN_FREE_ID}, 'active', now())"
            )
            print(f"customer-created name=svc/{key} plan=free")


def cmd_retire(email: str) -> None:
    row = logto_psql(f"select id from users where primary_email='{email}'")
    if not row:
        raise SystemExit(f"no such user {email}")
    logto_psql(f"update users set is_suspended=true, updated_at=now() where id='{row}'")
    wire_psql(f"update billing_customers set status='closed' where logto_sub='{row}' and status<>'closed'")
    print(f"retired email={email} sub={row}（is_suspended=true；服务客户已 closed）")


def probe_login(email: str, password: str, ua: str = "fd-svc-probe/1") -> str:
    script = (_PROBE_BASH
              .replace("__EMAIL__", email)
              .replace("__PW__", _shq(password).strip("'"))
              .replace("__UA__", ua))
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", XINRU, "bash -s"],
                       input=script, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stdout + r.stderr).strip())
    return r.stdout.strip()


def cmd_probe() -> None:
    line = sys.stdin.readline().split()
    if len(line) != 2:
        raise SystemExit("stdin 期望：〈email〉〈密码〉")
    email, password = line
    print(probe_login(email, password))


def guard_rows(hours: int) -> list[str]:
    since = f"now() - interval '{hours} hours'"
    rows = logto_psql(
        "select to_char(l.created_at at time zone 'Asia/Shanghai','MM-DD HH24:MI:SS') || ' | ' || "
        "u.primary_email || ' | ua=' || coalesce(l.payload->>'userAgent','?') || ' | ip=' || coalesce(l.payload->>'ip','?') "
        "from logs l join organization_user_relations r on r.user_id = l.payload->>'userId' "
        "join organizations o on o.id = r.organization_id and o.name='" + ORG_NAME + "' "
        "join users u on u.id = r.user_id "
        f"where l.key='Interaction.SignIn.Submit' and l.created_at >= {since} "
        "order by l.created_at"
    )
    return [r for r in rows.splitlines() if r]


def classify(line: str) -> str:
    ua = line.split("ua=")[1].split(" | ip=")[0] if "ua=" in line else ""
    return "ALARM(browser)" if ua.lower().startswith("mozilla") else "ok(tool)"


def cmd_guard(hours: int) -> None:
    rows = guard_rows(hours)
    if not rows:
        print(f"guard: 近 {hours}h 无服务账号登录事件 ✓")
        return
    for line in rows:
        print(f"guard: {line} -> {classify(line)}")


def cmd_selftest() -> None:
    """护栏构造测试（替代一周观察，2026-10-29 用户裁决）。

    临时号发号 → 工具 UA 探针（应 ok(tool)）→ 浏览器 UA 探针（应 ALARM(browser)）
    → guard 分类断言 → 即退役。密码不打印、不落任何面；账号用毕即 suspend。
    """
    key = f"selftest-{secrets.token_hex(2)}"
    email = f"svc-{key}@{EMAIL_DOMAIN}"
    checks: list[tuple[str, bool]] = []

    oid = org_id() or _die("先跑 ensure-org")
    existing = {r for r in logto_psql("select id from users where tenant_id='default'").splitlines() if r}
    uid = nanoid12(existing)
    password = secrets.token_urlsafe(18)
    logto_psql(
        "insert into users (tenant_id, id, username, primary_email, password_encrypted, "
        "password_encryption_method, is_suspended, password_updated_at, created_at, updated_at) "
        f"values ('default', '{uid}', 'svc-{key}', '{email}', '{argon2i_hash(password)}', "
        "'Argon2i', false, now(), now(), now())"
    )
    logto_psql(
        "insert into organization_user_relations (tenant_id, organization_id, user_id) "
        f"values ('default', '{oid}', '{uid}')"
    )
    checks.append(("provision(临时号入 org)", True))

    try:
        probe_login(email, password, ua="fd-svc-probe/1")
        checks.append(("tool-UA 探针登录成功", True))
    except RuntimeError as e:
        checks.append((f"tool-UA 探针失败: {e}", False))

    try:
        probe_login(email, password, ua="Mozilla/5.0 (Macintosh) fd-guard-alarm-test")
        checks.append(("browser-UA 探针登录成功", True))
    except RuntimeError as e:
        checks.append((f"browser-UA 探针失败: {e}", False))

    rows = [r for r in guard_rows(1) if email in r]
    checks.append(("审计可见两条登录", len(rows) >= 2))
    checks.append(("工具 UA 分类 ok(tool)", any(classify(r) == "ok(tool)" for r in rows)))
    checks.append(("浏览器 UA 分类 ALARM(browser)", any(classify(r) == "ALARM(browser)" for r in rows)))

    cmd_retire(email)
    after = logto_psql(f"select is_suspended from users where primary_email='{email}'")
    checks.append(("退役即停用", after == "t"))

    ok = all(passed for _, passed in checks)
    for name, passed in checks:
        print(f"selftest [{'PASS' if passed else 'FAIL'}] {name}")
    print(f"selftest: {'ALL PASS' if ok else 'FAILED'} ({email} 已退役)")
    raise SystemExit(0 if ok else 1)


def _die(msg: str):
    raise SystemExit(msg)


_PROBE_BASH = r"""
set -e
B=http://localhost:3001
U='__UA__'
J=$(mktemp)
CV=$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=')
CC=$(printf %s "$CV" | openssl dgst -sha256 -binary | base64 | tr '+/' '-_' | tr -d '=')
LOC=$(curl -s -o /dev/null -D - -A "$U" -c "$J" \
  "$B/oidc/auth?client_id=i0oxdswqxv4x7te99iukv&redirect_uri=https%3A%2F%2Fwire.finddatatech.cloud%2Fconsole%2Fcallback&response_type=code&scope=openid&code_challenge=$CC&code_challenge_method=S256&prompt=login" \
  | tr -d '\r' | awk 'tolower($1)=="location:"{print $2}')
[ -n "$LOC" ] || { echo 'probe-fail: 无 interaction 重定向'; exit 1; }
curl -s -o /dev/null -A "$U" -b "$J" -c "$J" "$B$LOC"
PUT=$(curl -s -A "$U" -b "$J" -X PUT "$B/api/experience" -H 'content-type: application/json' -d '{"interactionEvent":"SignIn"}')
V=$(curl -s -A "$U" -b "$J" -X POST "$B/api/experience/verification/password" -H 'content-type: application/json' -d "{\"identifier\":{\"type\":\"email\",\"value\":\"__EMAIL__\"},\"password\":\"__PW__\"}" | sed -n 's/.*"verificationId":"\([^"]*\)".*/\1/p')
[ -n "$V" ] || { echo "probe-fail: 密码校验未过或接口形态变化 PUT=$PUT"; exit 1; }
ID=$(curl -s -A "$U" -b "$J" -X POST "$B/api/experience/identification" -H 'content-type: application/json' -d "{\"verificationId\":\"$V\"}")
SUB=$(curl -s -A "$U" -b "$J" -X POST "$B/api/experience/submit")
echo "probe-ok submit=$SUB"
"""


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        raise SystemExit(__doc__)
    cmd, *rest = argv
    if cmd == "ensure-org":
        cmd_ensure_org()
    elif cmd == "provision" and rest:
        cmd_provision(rest[0])
    elif cmd == "retire" and rest:
        cmd_retire(rest[0])
    elif cmd == "probe":
        cmd_probe()
    elif cmd == "guard":
        cmd_guard(int(rest[1]) if len(rest) > 1 and rest[0] == "--hours" else 24)
    elif cmd == "selftest":
        cmd_selftest()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
