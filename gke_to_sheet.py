#!/usr/bin/env python3

import subprocess
import json
import sys
from typing import List, Dict, Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

# =========================================================
# CHANGE THESE VALUES
# =========================================================

# List the GCP projects you want to scan
PROJECT_IDS = [
    "CHANGE_ME_PROJECT_ID_1",
    # "CHANGE_ME_PROJECT_ID_2",
]

# Google Sheet details
SPREADSHEET_ID = "CHANGE_ME_SPREADSHEET_ID"
SHEET_NAME = "CHANGE_ME_TAB_NAME"   # Example: Sheet1

# Optional filters
# Leave empty list [] to scan all namespaces except ignored ones
INCLUDED_NAMESPACES = []

# Namespaces to skip
EXCLUDED_NAMESPACES = [
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "istio-system",
    "gke-gmp-system",
    "config-management-system",
]

# Workload types to include
# supported: deployments, statefulsets, daemonsets
WORKLOAD_TYPES = ["deployments", "statefulsets", "daemonsets"]

# Path to service account json that has access to Google Sheets API
SERVICE_ACCOUNT_FILE = "CHANGE_ME_SERVICE_ACCOUNT_JSON_PATH"

# If True, clear old data in A2:F before writing fresh data
CLEAR_EXISTING_DATA = True

# =========================================================


def run_cmd(cmd: List[str], check: bool = True) -> str:
    """Run shell command and return stdout."""
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n"
            f"Exit code: {result.returncode}\n"
            f"STDERR:\n{result.stderr}"
        )
    return result.stdout.strip()


def get_gke_clusters(project_id: str) -> List[Dict[str, Any]]:
    """Return list of GKE clusters in the given project."""
    cmd = [
        "gcloud", "container", "clusters", "list",
        "--project", project_id,
        "--format=json"
    ]
    output = run_cmd(cmd)
    clusters = json.loads(output) if output else []
    return clusters


def get_cluster_credentials(project_id: str, cluster_name: str, location: str) -> None:
    """Fetch kubeconfig credentials for a cluster."""
    cmd = [
        "gcloud", "container", "clusters", "get-credentials",
        cluster_name,
        "--project", project_id,
        "--location", location,
    ]
    run_cmd(cmd)


def get_namespaces() -> List[str]:
    """Return namespaces from current kubectl context."""
    cmd = ["kubectl", "get", "ns", "-o", "json"]
    output = run_cmd(cmd)
    data = json.loads(output)
    namespaces = [item["metadata"]["name"] for item in data.get("items", [])]

    filtered = []
    for ns in namespaces:
        if INCLUDED_NAMESPACES and ns not in INCLUDED_NAMESPACES:
            continue
        if ns in EXCLUDED_NAMESPACES:
            continue
        filtered.append(ns)
    return filtered


def get_workloads(namespace: str, workload_type: str) -> List[Dict[str, Any]]:
    """
    Return workloads for a namespace.
    workload_type examples: deployments, statefulsets, daemonsets
    """
    cmd = ["kubectl", "get", workload_type, "-n", namespace, "-o", "json"]
    output = run_cmd(cmd, check=False)

    if not output:
        return []

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []

    items = data.get("items", [])
    results = []

    for item in items:
        name = item["metadata"]["name"]
        selector = item.get("spec", {}).get("selector", {}).get("matchLabels", {})

        results.append({
            "name": name,
            "selector": selector,
        })

    return results


def selector_to_label_string(selector: Dict[str, str]) -> str:
    """Convert dict selector to kubectl -l format."""
    if not selector:
        return ""
    return ",".join([f"{k}={v}" for k, v in selector.items()])


def get_running_pods(namespace: str, selector: Dict[str, str]) -> int:
    """Count running pods in a namespace matching selector labels."""
    label_selector = selector_to_label_string(selector)

    cmd = ["kubectl", "get", "pods", "-n", namespace, "-o", "json"]
    if label_selector:
        cmd.extend(["-l", label_selector])

    output = run_cmd(cmd, check=False)
    if not output:
        return 0

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return 0

    count = 0
    for pod in data.get("items", []):
        phase = pod.get("status", {}).get("phase", "")
        if phase == "Running":
            count += 1

    return count


def collect_rows() -> List[List[Any]]:
    """Collect rows for sheet."""
    rows = []

    for project_id in PROJECT_IDS:
        print(f"\nScanning project: {project_id}", file=sys.stderr)
        clusters = get_gke_clusters(project_id)

        if not clusters:
            print(f"No clusters found in project {project_id}", file=sys.stderr)
            continue

        for cluster in clusters:
            cluster_name = cluster["name"]
            cluster_endpoint = cluster.get("endpoint", "")

            # GKE API may return location or zone depending on cluster type
            location = cluster.get("location") or cluster.get("zone")
            if not location:
                print(f"Skipping cluster {cluster_name}: location not found", file=sys.stderr)
                continue

            print(
                f"Connecting to cluster: {cluster_name} ({location}) "
                f"endpoint={cluster_endpoint}",
                file=sys.stderr
            )

            try:
                get_cluster_credentials(project_id, cluster_name, location)
            except Exception as e:
                print(f"Failed to get credentials for {cluster_name}: {e}", file=sys.stderr)
                continue

            try:
                namespaces = get_namespaces()
            except Exception as e:
                print(f"Failed to get namespaces for {cluster_name}: {e}", file=sys.stderr)
                continue

            for namespace in namespaces:
                for workload_type in WORKLOAD_TYPES:
                    try:
                        workloads = get_workloads(namespace, workload_type)
                    except Exception as e:
                        print(
                            f"Failed to get {workload_type} in {cluster_name}/{namespace}: {e}",
                            file=sys.stderr
                        )
                        continue

                    for workload in workloads:
                        workload_name = workload["name"]
                        selector = workload["selector"]

                        try:
                            running_pods = get_running_pods(namespace, selector)
                        except Exception as e:
                            print(
                                f"Failed pod count for "
                                f"{cluster_name}/{namespace}/{workload_name}: {e}",
                                file=sys.stderr
                            )
                            running_pods = 0

                        # Columns A-G
                        row = [
                            project_id,         # A Project ID
                            cluster_name,       # B Cluster Name
                            namespace,          # C Namespace
                            workload_type[:-1], # D Type -> deployment/statefulset/daemonset
                            workload_name,      # E K8s Object
                            running_pods,       # F Pods Running
                            cluster_endpoint,   # G Cluster Endpoint
                        ]
                        rows.append(row)

    return rows

def get_sheets_service():
    """Build Google Sheets API service using service account."""
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=scopes
    )
    return build("sheets", "v4", credentials=creds)


def clear_range(service, spreadsheet_id: str, range_name: str):
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        body={}
    ).execute()


def write_rows_to_sheet(rows: List[List[Any]]) -> None:
    """Write rows to Google Sheet."""
    service = get_sheets_service()

    if CLEAR_EXISTING_DATA:
        clear_range(service, SPREADSHEET_ID, f"{SHEET_NAME}!A2:G")

    if not rows:
        print("No rows collected. Nothing to write.", file=sys.stderr)
        return

    body = {
        "values": rows
    }

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A2",
        valueInputOption="RAW",
        body=body
    ).execute()


def main():
    try:
        rows = collect_rows()
        print(f"\nTotal rows collected: {len(rows)}", file=sys.stderr)
        write_rows_to_sheet(rows)
        print("Sheet update completed successfully.")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()