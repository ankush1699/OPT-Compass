#!/usr/bin/env bash
# Creates the OPT Compass stack on AWS: one EC2 server (runs docker compose), a security group, an Elastic IP and a
# CloudFront distribution that gives it an https address. Everything is tagged Project=opt-compass.
#
#   ./up.sh --check   look things up and ask EC2 whether the launch would be allowed (a dry run; creates nothing)
#   ./up.sh           create it (asks for a "yes" first)
#
# Needs the repo-root .env to hold LLM_PROVIDER=gemini, LLM_MODEL and GEMINI_API_KEY. The key is sent to the server in the
# instance's user data (see README.md in this folder for what that means).
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

require_login
load
[ -z "${INSTANCE_ID:-}" ] || die "already deployed (instance $INSTANCE_ID); run ./down.sh first"

env_value() { # value of KEY in the repo-root .env, without surrounding quotes
  { grep -E "^$1=" "$REPO_ROOT/.env" 2>/dev/null || true; } | tail -1 | cut -d= -f2- | sed -E "s/^[\"']//; s/[\"']$//"
}
LLM_PROVIDER_VALUE="$(env_value LLM_PROVIDER)"; LLM_PROVIDER_VALUE="${LLM_PROVIDER_VALUE:-gemini}"
LLM_MODEL_VALUE="$(env_value LLM_MODEL)"
GEMINI_KEY_VALUE="$(env_value GEMINI_API_KEY)"
[ "$LLM_PROVIDER_VALUE" = gemini ] || die "this deployment is set up for the Gemini free tier; LLM_PROVIDER in .env is '$LLM_PROVIDER_VALUE'"
[ -n "$LLM_MODEL_VALUE" ] || die "LLM_MODEL is empty in the repo-root .env"
[ -n "$GEMINI_KEY_VALUE" ] || die "GEMINI_API_KEY is empty in the repo-root .env"
git ls-remote --exit-code "$REPO_URL" "refs/heads/$GIT_REF" >/dev/null ||
  die "branch '$GIT_REF' is not on GitHub; push it (or set GIT_REF=<branch>) because the server clones it"

curl -fsI "https://raw.githubusercontent.com/ankush1699/opt-compass/$GIT_REF/docker-compose.yml" >/dev/null 2>&1 ||
  die "branch '$GIT_REF' on GitHub has no docker-compose.yml (merge the Docker work into it, or set GIT_REF=<branch>)"

AMI="$(aws ec2 describe-images --owners 099720109477 \
  --filters 'Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*' 'Name=state,Values=available' \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)"
VPC="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)"
[ "$VPC" != None ] || die "no default VPC in $REGION"
SUBNET="$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" Name=default-for-az,Values=true --query 'Subnets[0].SubnetId' --output text)"
PREFIX_LIST="$(aws ec2 describe-managed-prefix-lists --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
  --query 'PrefixLists[0].PrefixListId' --output text)"
CACHE_POLICY="$(aws cloudfront list-cache-policies --type managed \
  --query "CachePolicyList.Items[?CachePolicy.CachePolicyConfig.Name=='Managed-CachingDisabled'].CachePolicy.Id | [0]" --output text)"
ORIGIN_POLICY="$(aws cloudfront list-origin-request-policies --type managed \
  --query "OriginRequestPolicyList.Items[?OriginRequestPolicy.OriginRequestPolicyConfig.Name=='Managed-AllViewerExceptHostHeader'].OriginRequestPolicy.Id | [0]" --output text)"

say "account $ACCOUNT, region $REGION"
say "image $AMI (Ubuntu 24.04 arm64), vpc $VPC, subnet $SUBNET"
say "CloudFront origin-facing prefix list $PREFIX_LIST; cache policy $CACHE_POLICY; origin request policy $ORIGIN_POLICY"

if [ "$CHECK_ONLY" = 1 ]; then
  DEFAULT_SG="$(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$VPC" Name=group-name,Values=default \
    --query 'SecurityGroups[0].GroupId' --output text)"
  result="$(aws ec2 run-instances --dry-run --image-id "$AMI" --instance-type "$INSTANCE_TYPE" --subnet-id "$SUBNET" \
    --security-group-ids "$DEFAULT_SG" 2>&1 || true)"
  case "$result" in
    *DryRunOperation*) say "dry run: EC2 would allow launching a $INSTANCE_TYPE. Nothing was created." ;;
    *) die "dry run: EC2 refused the launch: $result" ;;
  esac
  exit 0
fi

say ""
say "This will create in $REGION, in account $ACCOUNT:"
say "  - 1 EC2 server $INSTANCE_TYPE with a ${DISK_GB} GB gp3 disk, 1 Elastic IP, 1 security group, 1 CloudFront distribution"
say "  - it clones $REPO_URL ($GIT_REF) and runs docker compose"
say "  - roughly \$17.50 a month while it runs; ./down.sh removes everything"
confirm ""

TAGS_EC2="{Key=Project,Value=$PROJECT}"
trap 'say ""; say "Stopped part-way. What was created is recorded; run ./down.sh to remove it." >&2' ERR

# Keys: generated here, kept in .state/secrets.env (mode 600) and on the server in /opt/opt-compass/.env.
API_KEY="$(openssl rand -hex 24)"; STATS_KEY="$(openssl rand -hex 24)"
SALT="$(openssl rand -hex 16)"; DB_PASSWORD="$(openssl rand -hex 16)"
mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
( umask 077; printf 'API_KEY=%s\nSTATS_KEY=%s\n' "$API_KEY" "$STATS_KEY" > "$SECRETS_FILE" )

ENV_B64="$(printf '%s\n' \
  "LLM_PROVIDER=gemini" "LLM_MODEL=$LLM_MODEL_VALUE" "GEMINI_API_KEY=$GEMINI_KEY_VALUE" "GEMINI_FREE_TIER=1" \
  "DEMO_ENABLED=1" "TRUST_PROXY_HEADERS=1" "API_BIND=0.0.0.0" "API_PORT=80" \
  "API_KEYS=owner:$API_KEY" "STATS_KEY=$STATS_KEY" "ANALYTICS_SALT=$SALT" "POSTGRES_PASSWORD=$DB_PASSWORD" | base64 | tr -d '\n')"

USER_DATA="$(mktemp)"; CF_CONFIG="$(mktemp)"
trap 'rm -f "$USER_DATA" "$CF_CONFIG"' EXIT
# No "set -x" here: the console output would then show the commands, and the .env is written from a variable.
cat > "$USER_DATA" <<SCRIPT
#!/bin/bash
set -eu
exec >> /var/log/opt-compass-init.log 2>&1
export DEBIAN_FRONTEND=noninteractive
echo "start \$(date -u)"
apt-get update -y
apt-get install -y docker.io docker-compose-v2 git
systemctl enable --now docker
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
git clone --depth 1 --branch "$GIT_REF" "$REPO_URL" /opt/opt-compass
cd /opt/opt-compass
echo '$ENV_B64' | base64 -d > .env
chmod 600 .env
docker compose up -d --build
echo "done \$(date -u)"
SCRIPT

say "creating the security group (port 80, reachable only from CloudFront)..."
SG="$(aws ec2 create-security-group --group-name "$PROJECT-sg" --description "OPT Compass: HTTP from CloudFront only" \
  --vpc-id "$VPC" --tag-specifications "ResourceType=security-group,Tags=[$TAGS_EC2]" --query GroupId --output text)"
save SG_ID "$SG"
aws ec2 authorize-security-group-ingress --group-id "$SG" \
  --ip-permissions "IpProtocol=tcp,FromPort=80,ToPort=80,PrefixListIds=[{PrefixListId=$PREFIX_LIST,Description=CloudFront}]" >/dev/null

say "launching the server..."
INSTANCE="$(aws ec2 run-instances --image-id "$AMI" --instance-type "$INSTANCE_TYPE" --subnet-id "$SUBNET" --security-group-ids "$SG" \
  --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$DISK_GB,VolumeType=gp3,DeleteOnTermination=true}" \
  --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$PROJECT},$TAGS_EC2]" "ResourceType=volume,Tags=[$TAGS_EC2]" \
  --user-data "file://$USER_DATA" --query 'Instances[0].InstanceId' --output text)"
save INSTANCE_ID "$INSTANCE"
aws ec2 wait instance-running --instance-ids "$INSTANCE"

say "attaching a fixed IP address..."
read -r ALLOC EIP < <(aws ec2 allocate-address --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[$TAGS_EC2]" \
  --query '[AllocationId,PublicIp]' --output text)
save EIP_ALLOC_ID "$ALLOC"
aws ec2 associate-address --instance-id "$INSTANCE" --allocation-id "$ALLOC" >/dev/null
DASHED="${EIP//./-}"
ORIGIN=""
for _ in $(seq 1 30); do
  ORIGIN="$(aws ec2 describe-instances --instance-ids "$INSTANCE" --query 'Reservations[0].Instances[0].PublicDnsName' --output text)"
  case "$ORIGIN" in *"$DASHED"*) break ;; esac
  ORIGIN=""; sleep 5
done
[ -n "$ORIGIN" ] || die "the server did not get a public DNS name for $EIP"
say "server $INSTANCE at $EIP ($ORIGIN)"

say "creating the CloudFront distribution (this part takes several minutes to roll out)..."
cat > "$CF_CONFIG" <<JSON
{
  "DistributionConfig": {
    "CallerReference": "$PROJECT-$(date +%s)",
    "Comment": "$PROJECT",
    "Enabled": true,
    "PriceClass": "PriceClass_100",
    "Origins": {"Quantity": 1, "Items": [{
      "Id": "server", "DomainName": "$ORIGIN",
      "CustomOriginConfig": {"HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "http-only",
                             "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 5}
    }]},
    "DefaultCacheBehavior": {
      "TargetOriginId": "server",
      "ViewerProtocolPolicy": "redirect-to-https",
      "AllowedMethods": {"Quantity": 7, "Items": ["GET","HEAD","OPTIONS","PUT","POST","PATCH","DELETE"],
                         "CachedMethods": {"Quantity": 2, "Items": ["GET","HEAD"]}},
      "CachePolicyId": "$CACHE_POLICY",
      "OriginRequestPolicyId": "$ORIGIN_POLICY",
      "Compress": false
    }
  },
  "Tags": {"Items": [{"Key": "Project", "Value": "$PROJECT"}]}
}
JSON
read -r CF_ID CF_DOMAIN < <(aws cloudfront create-distribution-with-tags --distribution-config-with-tags "file://$CF_CONFIG" \
  --query 'Distribution.[Id,DomainName]' --output text)
save CLOUDFRONT_ID "$CF_ID"
save CLOUDFRONT_DOMAIN "$CF_DOMAIN"
save EIP "$EIP"

say "waiting for https://$CF_DOMAIN/health (the server builds the image on first boot, about 10 minutes)..."
for i in $(seq 1 90); do
  if curl -fsS --max-time 10 "https://$CF_DOMAIN/health" 2>/dev/null | grep -q '"status":"ok"'; then
    say ""
    say "Live: https://$CF_DOMAIN/"
    say "Health: https://$CF_DOMAIN/health"
    say "Your API key and stats key are in $SECRETS_FILE (not printed). Run ./status.sh for the details."
    exit 0
  fi
  [ $((i % 6)) = 0 ] && say "  still waiting ($((i / 2)) min)..."
  sleep 10
done
die "the site did not answer within 15 minutes; see ./status.sh --log, and remember ./down.sh removes everything"
