#!/usr/bin/env bash
# Shared settings and helpers for up.sh, status.sh and down.sh. Sourced, not run.
set -euo pipefail

export AWS_PAGER=""
REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$REGION"

PROJECT=opt-compass
INSTANCE_TYPE="${INSTANCE_TYPE:-t4g.small}"   # 2 GB RAM, ARM; the service measured about 1.3 GB with both models loaded
DISK_GB="${DISK_GB:-20}"
GIT_REF="${GIT_REF:-main}"                     # the branch the server clones; it must be pushed to GitHub
REPO_URL=https://github.com/ankush1699/opt-compass.git

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
STATE_DIR="$HERE/.state"                       # gitignored: resource ids and the generated keys
STATE_FILE="$STATE_DIR/resources.env"
SECRETS_FILE="$STATE_DIR/secrets.env"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# save KEY VALUE: write a created resource's id down at once, so down.sh can remove it even if up.sh stops half-way.
save() {
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  touch "$STATE_FILE"
  { grep -v "^$1=" "$STATE_FILE" || true; printf '%s=%s\n' "$1" "$2"; } > "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
}

load() {
  if [ -f "$STATE_FILE" ]; then
    # shellcheck disable=SC1090
    . "$STATE_FILE"
  fi
}

confirm() {
  local answer
  read -r -p "$1 Type yes to continue: " answer
  [ "$answer" = yes ] || die "stopped, nothing was changed"
}

require_login() {
  ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" ||
    die "no working AWS login; run: aws configure"
}
