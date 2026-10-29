#!/usr/bin/env bash
# Scale every ECS service in the test cluster to 0 (save Fargate cost) and back.
# Internal-only. Both directions are CloudFormation drift; the next services
# deploy re-applies DesiredCount but NOT autoscaling MinCapacity, so always
# `up` before (or after) an upgrade test.
#
#   REGION=us-east-1 CLUSTER=<cluster name or arn> dev-scripts/scale-cardinal.sh down
#   REGION=us-east-1 CLUSTER=<cluster name or arn> dev-scripts/scale-cardinal.sh up
#
# `down` records each service's desired count and autoscaling floor in
# STATE_FILE (default ~/.cardinal-scale-<cluster>.tsv); `up` restores from it.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib.sh"
LOG_TAG="scale-cardinal"

REGION="${REGION:-us-east-1}"
CLUSTER="${CLUSTER:?CLUSTER is required (ECS cluster name or ARN)}"
CLUSTER_NAME="${CLUSTER##*/}"
STATE_FILE="${STATE_FILE:-$HOME/.cardinal-scale-$CLUSTER_NAME.tsv}"
MODE="${1:-}"

need_cmd aws
need_cmd jq

scalable_min() {  # <service name> -> MinCapacity or empty
  aws application-autoscaling describe-scalable-targets --region "$REGION" \
    --service-namespace ecs --resource-ids "service/$CLUSTER_NAME/$1" \
    --query 'ScalableTargets[0].MinCapacity' --output text 2>/dev/null | sed 's/^None$//'
}

set_min() {  # <service name> <min>
  aws application-autoscaling register-scalable-target --region "$REGION" \
    --service-namespace ecs --scalable-dimension ecs:service:DesiredCount \
    --resource-id "service/$CLUSTER_NAME/$1" --min-capacity "$2" >/dev/null
}

set_desired() {  # <service name> <count>
  aws ecs update-service --region "$REGION" --cluster "$CLUSTER" \
    --service "$1" --desired-count "$2" --query 'service.serviceName' --output text >/dev/null
}

case "$MODE" in
  down)
    [ -e "$STATE_FILE" ] && die "$STATE_FILE exists; already scaled down? run 'up' first or remove it"
    for svc in $(aws ecs list-services --region "$REGION" --cluster "$CLUSTER" \
        --query 'serviceArns[]' --output text | tr '\t' '\n' | sed 's#.*/##'); do
      desired="$(aws ecs describe-services --region "$REGION" --cluster "$CLUSTER" \
        --services "$svc" --query 'services[0].desiredCount' --output text)"
      min="$(scalable_min "$svc")"
      printf '%s\t%s\t%s\n' "$svc" "$desired" "$min" >> "$STATE_FILE"
      [ -n "$min" ] && set_min "$svc" 0
      set_desired "$svc" 0
      log "down: $svc desired=$desired min=${min:-n/a} -> 0"
    done
    log "state saved to $STATE_FILE"
    ;;
  up)
    [ -r "$STATE_FILE" ] || die "no state file $STATE_FILE; nothing to restore"
    while IFS=$'\t' read -r svc desired min; do
      [ -n "$min" ] && set_min "$svc" "$min"
      set_desired "$svc" "$desired"
      log "up: $svc -> desired=$desired min=${min:-n/a}"
    done < "$STATE_FILE"
    rm -f "$STATE_FILE"
    ;;
  *)
    die "usage: $0 up|down"
    ;;
esac
