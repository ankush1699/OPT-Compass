#!/usr/bin/env bash
# Deletes everything up.sh created, then lists what is left. Safe to run again if it stops part-way.
# It uses the ids in .state/resources.env, and also looks for anything tagged Project=opt-compass.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_login
load

# Anything tagged for this project that the state file does not know about (for example after a half-finished up.sh).
if [ -z "${INSTANCE_ID:-}" ]; then
  INSTANCE_ID="$(aws ec2 describe-instances --filters "Name=tag:Project,Values=$PROJECT" \
    'Name=instance-state-name,Values=pending,running,stopping,stopped' --query 'Reservations[0].Instances[0].InstanceId' --output text)"
  [ "$INSTANCE_ID" != None ] || INSTANCE_ID=""
fi
if [ -z "${EIP_ALLOC_ID:-}" ]; then
  EIP_ALLOC_ID="$(aws ec2 describe-addresses --filters "Name=tag:Project,Values=$PROJECT" --query 'Addresses[0].AllocationId' --output text)"
  [ "$EIP_ALLOC_ID" != None ] || EIP_ALLOC_ID=""
fi
if [ -z "${SG_ID:-}" ]; then
  SG_ID="$(aws ec2 describe-security-groups --filters "Name=tag:Project,Values=$PROJECT" --query 'SecurityGroups[0].GroupId' --output text)"
  [ "$SG_ID" != None ] || SG_ID=""
fi
if [ -z "${CLOUDFRONT_ID:-}" ]; then
  CLOUDFRONT_ID="$(aws cloudfront list-distributions --query "DistributionList.Items[?Comment=='$PROJECT'].Id | [0]" --output text 2>/dev/null || true)"
  { [ "$CLOUDFRONT_ID" != None ] && [ "$CLOUDFRONT_ID" != null ]; } || CLOUDFRONT_ID=""
fi

say "account $ACCOUNT, region $REGION. About to delete:"
say "  server: ${INSTANCE_ID:-none}   elastic ip: ${EIP_ALLOC_ID:-none}   security group: ${SG_ID:-none}   cloudfront: ${CLOUDFRONT_ID:-none}"
if [ -z "${INSTANCE_ID}${EIP_ALLOC_ID}${SG_ID}${CLOUDFRONT_ID}" ]; then
  say "Nothing to delete."
  rm -rf "$STATE_DIR"
  exit 0
fi
say "  (the database and the analytics data live on the server's disk and are lost with it)"
confirm ""

CF_CONFIG="$(mktemp)"
trap 'rm -f "$CF_CONFIG"' EXIT

if [ -n "$CLOUDFRONT_ID" ]; then
  say "disabling CloudFront $CLOUDFRONT_ID (it can only be deleted once the change has rolled out)..."
  if cfg="$(aws cloudfront get-distribution-config --id "$CLOUDFRONT_ID" 2>/dev/null)"; then
    if [ "$(jq -r .DistributionConfig.Enabled <<<"$cfg")" = true ]; then
      jq '.DistributionConfig | .Enabled = false' <<<"$cfg" > "$CF_CONFIG"
      aws cloudfront update-distribution --id "$CLOUDFRONT_ID" --if-match "$(jq -r .ETag <<<"$cfg")" \
        --distribution-config "file://$CF_CONFIG" >/dev/null
    fi
  else
    say "  CloudFront $CLOUDFRONT_ID is already gone"
    CLOUDFRONT_ID=""
  fi
fi

if [ -n "$INSTANCE_ID" ]; then
  say "terminating the server $INSTANCE_ID..."
  aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" >/dev/null || true
  aws ec2 wait instance-terminated --instance-ids "$INSTANCE_ID" || true
fi

if [ -n "$EIP_ALLOC_ID" ]; then
  say "releasing the Elastic IP..."
  aws ec2 release-address --allocation-id "$EIP_ALLOC_ID" || true
fi

if [ -n "$SG_ID" ]; then
  say "deleting the security group..."
  for _ in $(seq 1 18); do   # it is in use until the server's network interface is gone
    aws ec2 delete-security-group --group-id "$SG_ID" 2>/dev/null && break
    sleep 10
  done
fi

if [ -n "$CLOUDFRONT_ID" ]; then
  say "waiting for CloudFront to finish rolling out the disable (several minutes)..."
  aws cloudfront wait distribution-deployed --id "$CLOUDFRONT_ID"
  say "deleting CloudFront $CLOUDFRONT_ID..."
  aws cloudfront delete-distribution --id "$CLOUDFRONT_ID" --if-match \
    "$(aws cloudfront get-distribution-config --id "$CLOUDFRONT_ID" --query ETag --output text)"
fi

say ""
say "What is left in $REGION tagged Project=$PROJECT:"
left=0
n="$(aws ec2 describe-instances --filters "Name=tag:Project,Values=$PROJECT" 'Name=instance-state-name,Values=pending,running,stopping,stopped' \
  --query 'length(Reservations[].Instances[])' --output text)"; say "  servers: $n"; left=$((left + n))
n="$(aws ec2 describe-addresses --filters "Name=tag:Project,Values=$PROJECT" --query 'length(Addresses)' --output text)"; say "  elastic ips: $n"; left=$((left + n))
n="$(aws ec2 describe-security-groups --filters "Name=tag:Project,Values=$PROJECT" --query 'length(SecurityGroups)' --output text)"; say "  security groups: $n"; left=$((left + n))
n="$(aws cloudfront list-distributions --query "length(DistributionList.Items[?Comment=='$PROJECT'] || \`[]\`)" --output text 2>/dev/null || echo 0)"
[ "$n" != None ] || n=0
say "  cloudfront distributions: $n"; left=$((left + n))
if [ "$left" = 0 ]; then
  rm -rf "$STATE_DIR"
  say "Everything is removed. Billing for these resources has stopped."
else
  die "something is still there (see above); run ./down.sh again, or look in the AWS console"
fi
