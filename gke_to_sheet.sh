#!/usr/bin/env bash

set -euo pipefail

# =========================================================
# CHANGE THESE VALUES
# =========================================================

# GCP projects to scan
PROJECT_IDS=(
  "CHANGE_ME_PROJECT_ID_1"
  # "CHANGE_ME_PROJECT_ID_2"
)

# Spreadsheet details
SPREADSHEET_ID="CHANGE_ME_SPREADSHEET_ID"
SHEET_NAME="CHANGE_ME_TAB_NAME"   # Example: Monitoring Sheet

# Google service account json for Sheets API access
SERVICE_ACCOUNT_FILE="CHANGE_ME_SERVICE_ACCOUNT_JSON_PATH"

# Included namespaces.
# Leave empty ("") to scan all namespaces except excluded ones.
INCLUDED_NAMESPACES=(
  # "default"
  # "prod"
  # "backend"
)

# Excluded namespaces
EXCLUDED_NAMESPACES=(
  "kube-system"
  "kube-public"
  "kube-node-lease"
  "istio-system"
  "gke-gmp-system"
  "config-management-system"
)

# Workload types to scan
WORKLOAD_TYPES=(
  "deployments"
  "statefulsets"
  "daemonsets"
)

# If true, clear A2:F before writing
CLEAR_EXISTING_DATA="true"

# Temporary CSV output
OUTPUT_CSV="/tmp/gke_to_sheet_output.csv"

# =========================================================


log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: Required command not found: $1" >&2
    exit 1
  }
}

contains_in_array() {
  local seeking="$1"
  shift
  local item
  for item in "$@"; do
    [[ "$item" == "$seeking" ]] && return 0
  done
  return 1
}

namespace_allowed() {
  local ns="$1"

  if contains_in_array "$ns" "${EXCLUDED_NAMESPACES[@]}"; then
    return 1
  fi

  if [[ ${#INCLUDED_NAMESPACES[@]} -eq 0 ]]; then
    return 0
  fi

  if contains_in_array "$ns" "${INCLUDED_NAMESPACES[@]}"; then
    return 0
  fi

  return 1
}

get_access_token() {
  gcloud auth application-default print-access-token
}

clear_sheet_range() {
  local access_token="$1"
  local range="${SHEET_NAME}!A2:F"

  curl -sS -X POST \
    -H "Authorization: Bearer ${access_token}" \
    -H "Content-Type: application/json" \
    "https://sheets.googleapis.com/v4/spreadsheets/${SPREADSHEET_ID}/values/${range}:clear" \
    -d '{}'
}

csv_to_json_values() {
  python3 - <<'PY' "${OUTPUT_CSV}"
import csv
import json
import sys

path = sys.argv[1]
rows = []

with open(path, newline="", encoding="utf-8") as f:
    reader = csv.reader(f)
    for row in reader:
        rows.append(row)

print(json.dumps({"values": rows}))
PY
}

write_sheet_data() {
  local access_token="$1"
  local json_body="$2"
  local range="${SHEET_NAME}!A2"

  curl -sS -X PUT \
    -H "Authorization: Bearer ${access_token}" \
    -H "Content-Type: application/json" \
    "https://sheets.googleapis.com/v4/spreadsheets/${SPREADSHEET_ID}/values/${range}?valueInputOption=RAW" \
    -d "${json_body}"
}

collect_cluster_rows() {
  : > "${OUTPUT_CSV}"

  local project_id
  for project_id in "${PROJECT_IDS[@]}"; do
    log "Scanning project: ${project_id}"

    local clusters_json
    clusters_json="$(gcloud container clusters list \
      --project "${project_id}" \
      --format=json)"

    local cluster_count
    cluster_count="$(echo "${clusters_json}" | jq 'length')"

    if [[ "${cluster_count}" -eq 0 ]]; then
      log "No clusters found in ${project_id}"
      continue
    fi

    echo "${clusters_json}" | jq -c '.[]' | while IFS= read -r cluster; do
      local cluster_name location cluster_endpoint
      cluster_name="$(echo "${cluster}" | jq -r '.name')"
      location="$(echo "${cluster}" | jq -r '.location // .zone // empty')"
      cluster_endpoint="$(echo "${cluster}" | jq -r '.endpoint // empty')"

      if [[ -z "${location}" ]]; then
        log "Skipping cluster ${cluster_name}: location not found"
        continue
      fi

      log "Connecting to cluster: ${cluster_name} (${location}) endpoint=${cluster_endpoint}"

      if ! gcloud container clusters get-credentials \
        "${cluster_name}" \
        --project "${project_id}" \
        --location "${location}" >/dev/null 2>&1; then
        log "Failed to get credentials for ${cluster_name}"
        continue
      fi

      local namespaces_json
      if ! namespaces_json="$(kubectl get ns -o json 2>/dev/null)"; then
        log "Failed to get namespaces for cluster ${cluster_name}"
        continue
      fi

      echo "${namespaces_json}" | jq -r '.items[].metadata.name' | while IFS= read -r namespace; do
        if ! namespace_allowed "${namespace}"; then
          continue
        fi

        local workload_type
        for workload_type in "${WORKLOAD_TYPES[@]}"; do
          local workloads_json
          if ! workloads_json="$(kubectl get "${workload_type}" -n "${namespace}" -o json 2>/dev/null)"; then
            continue
          fi

          echo "${workloads_json}" | jq -c '.items[]?' | while IFS= read -r workload; do
            local workload_name type_singular
            workload_name="$(echo "${workload}" | jq -r '.metadata.name')"
            type_singular="${workload_type%s}"

            local selector
            selector="$(echo "${workload}" | jq -r '
              .spec.selector.matchLabels // {} |
              to_entries |
              map("\(.key)=\(.value)") |
              join(",")
            ')"

            local pods_json running_pods
            if [[ -n "${selector}" ]]; then
              pods_json="$(kubectl get pods -n "${namespace}" -l "${selector}" -o json 2>/dev/null || echo '{"items":[]}')"
            else
              pods_json="$(kubectl get pods -n "${namespace}" -o json 2>/dev/null || echo '{"items":[]}')"
            fi

            running_pods="$(echo "${pods_json}" | jq '[.items[] | select(.status.phase=="Running")] | length')"

            python3 - <<'PY' "${OUTPUT_CSV}" "${project_id}" "${cluster_name}" "${namespace}" "${type_singular}" "${workload_name}" "${running_pods}" "${cluster_endpoint}"
import csv
import sys

path, a, b, c, d, e, f, g = sys.argv[1:]
with open(path, "a", newline="", encoding="utf-8") as fp:
    writer = csv.writer(fp)
    writer.writerow([a, b, c, d, e, f, g])
PY
          done
        done
      done
    done
  done
}

main() {
  require_cmd gcloud
  require_cmd kubectl
  require_cmd jq
  require_cmd curl
  require_cmd python3

  export GOOGLE_APPLICATION_CREDENTIALS="${SERVICE_ACCOUNT_FILE}"

  log "Checking application default credentials"
  gcloud auth application-default print-access-token >/dev/null

  log "Collecting rows from GKE"
  collect_cluster_rows

  local row_count
  row_count="$(python3 - <<'PY' "${OUTPUT_CSV}"
import csv, sys
count = 0
with open(sys.argv[1], newline="", encoding="utf-8") as f:
    for _ in csv.reader(f):
        count += 1
print(count)
PY
)"

  log "Collected ${row_count} rows"

  local access_token
  access_token="$(get_access_token)"

  if [[ "${CLEAR_EXISTING_DATA}" == "true" ]]; then
    log "Clearing existing sheet data A2:F"
    clear_sheet_range "${access_token}" >/dev/null
  fi

  if [[ "${row_count}" -eq 0 ]]; then
    log "No rows collected. Nothing to write."
    exit 0
  fi

  local json_body
  json_body="$(csv_to_json_values)"

  log "Writing data to Google Sheet"
  write_sheet_data "${access_token}" "${json_body}" >/dev/null

  log "Sheet update completed successfully"
}

main "$@"