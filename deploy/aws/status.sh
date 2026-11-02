#!/usr/bin/env bash
# Shows what exists, whether the site answers, and (with --log) the server's first-boot log.
#
#   ./status.sh          resources, health check, how to read the stats
#   ./status.sh --log    also print the server's console output (the first-boot install log), if EC2 has any
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_login
load
[ -n "${INSTANCE_ID:-}${CLOUDFRONT_ID:-}${SG_ID:-}" ] || { say "Nothing recorded in $STATE_FILE: no deployment from this folder."; exit 0; }

say "account $ACCOUNT, region $REGION"
if [ -n "${INSTANCE_ID:-}" ]; then
  aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].[InstanceId,InstanceType,State.Name,PublicIpAddress]' --output text |
    awk '{printf "server   %s  %s  %s  %s\n", $1, $2, $3, $4}'
fi
if [ -n "${CLOUDFRONT_ID:-}" ]; then
  aws cloudfront get-distribution --id "$CLOUDFRONT_ID" --query 'Distribution.[Status,DomainName]' --output text |
    awk '{printf "cloudfront  %s (%s)\n", $2, $1}'
fi
say "security group: ${SG_ID:-none}   elastic ip allocation: ${EIP_ALLOC_ID:-none}"

if [ -n "${CLOUDFRONT_DOMAIN:-}" ]; then
  say ""
  say "health: $(curl -sS --max-time 10 "https://$CLOUDFRONT_DOMAIN/health" 2>&1 || true)"
  say "page:   https://$CLOUDFRONT_DOMAIN/"
  say "stats:  curl -s -H \"X-Stats-Key: \$(sed -n 's/^STATS_KEY=//p' $SECRETS_FILE)\" https://$CLOUDFRONT_DOMAIN/stats"
fi

if [ "${1:-}" = "--log" ] && [ -n "${INSTANCE_ID:-}" ]; then
  say ""
  say "--- console output (may lag by a few minutes; the install log itself is /var/log/opt-compass-init.log on the server) ---"
  aws ec2 get-console-output --instance-id "$INSTANCE_ID" --latest --output text --query Output | tail -60
fi
