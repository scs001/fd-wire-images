#!/usr/bin/env bash
# 服务账号护栏巡检（change: add-fleet-service-accounts）
# 在 xinru-master 上运行（cron 每日）；扫 Logto 审计里 svc-fleet 成员的登录事件。
# 分类：浏览器 UA（Mozilla*）= ALARM；其余（fd-svc-probe/mint-line/curl 等）= 记录不告警。
set -euo pipefail
HOURS="${1:-26}"
SQL=$(cat <<EOF
select to_char(l.created_at at time zone 'Asia/Shanghai','MM-DD HH24:MI:SS') || ' | ' ||
u.primary_email || ' | ua=' || coalesce(l.payload->>'userAgent','?') || ' | ip=' || coalesce(l.payload->>'ip','?')
from logs l
join organization_user_relations r on r.user_id = l.payload->>'userId'
join organizations o on o.id = r.organization_id and o.name='svc-fleet'
join users u on u.id = r.user_id
where l.key='Interaction.SignIn.Submit' and l.created_at >= now() - interval '${HOURS} hours'
order by l.created_at
EOF
)
ROWS=$(sudo docker exec fd-postgres psql -U logto -d logto -Atc "$SQL" || true)
STAMP=$(date '+%F %T %z')
if [ -z "$ROWS" ]; then
  echo "[$STAMP] guard: 近 ${HOURS}h 无服务账号登录事件 OK"
  exit 0
fi
ALARM=0
while IFS= read -r line; do
  [ -z "$line" ] && continue
  UA=${line#*| ua=}; UA=${UA%% | ip=*}
  case "$UA" in
    Mozilla*) echo "[$STAMP] guard ALARM(browser): $line"; ALARM=1 ;;
    *)        echo "[$STAMP] guard ok(tool): $line" ;;
  esac
done <<< "$ROWS"
exit $ALARM
