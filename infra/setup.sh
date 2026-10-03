#!/usr/bin/env bash
# Sandbox setup for the PoC (Azure CLI). Creates: resource group, Azure AI Search, Log Analytics + Application Insights,
# and grants YOUR signed-in user the data-plane roles needed for AUTH_MODE=entra.
#
# Create the Microsoft Foundry resource, project and model deployments first (see README, Day 1),
# then supply the Foundry resource ID below. Writes Search and monitoring settings to the repo's .env.
#
# Usage:  az login && bash infra/setup.sh
set +x                              # never trace connection strings, even when invoked with bash -x
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
POC_PYTHON="${POC_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$POC_PYTHON" ]]; then
  echo "Phase 1 .venv Python is required before setup." >&2
  exit 1
fi
"$POC_PYTHON" -c 'import dotenv'       # fail before creating resources if local prep is incomplete

PREFIX="${PREFIX:-eapoc$RANDOM}"          # must be globally unique for Search
LOCATION="${LOCATION:-uaenorth}"          # sandbox: any region where your models are available
RG="${RG:-rg-$PREFIX}"
SEARCH="${SEARCH:-srch-$PREFIX}"
LAW="${LAW:-law-$PREFIX}"
APPI="${APPI:-appi-$PREFIX}"
FOUNDRY_RESOURCE_ID="${FOUNDRY_RESOURCE_ID:-}"   # /subscriptions/.../providers/Microsoft.CognitiveServices/accounts/<name>
SUBSCRIPTION="$(az account show --query id -o tsv)"
if [[ -n "$FOUNDRY_RESOURCE_ID" && "$FOUNDRY_RESOURCE_ID" != /subscriptions/"$SUBSCRIPTION"/* ]]; then
  echo "Active subscription does not match FOUNDRY_RESOURCE_ID; stopping." >&2
  exit 1
fi

echo "▶ Resource group $RG in $LOCATION"
az group create --subscription "$SUBSCRIPTION" -n "$RG" -l "$LOCATION" -o none

echo "▶ Azure AI Search $SEARCH (Basic, API key + Entra RBAC enabled)"
az search service create --subscription "$SUBSCRIPTION" -n "$SEARCH" -g "$RG" -l "$LOCATION" --sku basic \
  --auth-options aadOrApiKey --aad-auth-failure-mode http401WithBearerChallenge -o none
echo "  (optional) enable semantic ranker:  az search service update -n $SEARCH -g $RG --semantic-search free"

echo "▶ Log Analytics + Application Insights"
az monitor log-analytics workspace create --subscription "$SUBSCRIPTION" -g "$RG" -n "$LAW" -l "$LOCATION" -o none
LAW_ID=$(az monitor log-analytics workspace show --subscription "$SUBSCRIPTION" -g "$RG" -n "$LAW" --query id -o tsv)
az extension add -n application-insights --upgrade -o none
az monitor app-insights component create --subscription "$SUBSCRIPTION" -a "$APPI" -g "$RG" -l "$LOCATION" --workspace "$LAW_ID" -o none

echo "▶ Role assignments for the signed-in user (AUTH_MODE=entra)"
ME=$(az ad signed-in-user show --query id -o tsv)
SEARCH_ID=$(az search service show --subscription "$SUBSCRIPTION" -n "$SEARCH" -g "$RG" --query id -o tsv)
for ROLE in "Search Service Contributor" "Search Index Data Contributor" "Search Index Data Reader"; do
  az role assignment create --subscription "$SUBSCRIPTION" --assignee-object-id "$ME" --assignee-principal-type User --role "$ROLE" --scope "$SEARCH_ID" -o none
  echo "  Assigned: $ROLE"
done
if [[ -n "$FOUNDRY_RESOURCE_ID" ]]; then
  for ROLE in "Foundry User" "Cognitive Services OpenAI User" "Cognitive Services User"; do
    az role assignment create --subscription "$SUBSCRIPTION" --assignee-object-id "$ME" --assignee-principal-type User --role "$ROLE" --scope "$FOUNDRY_RESOURCE_ID" -o none
    echo "  Assigned: $ROLE"
  done
else
  echo "  ⚠ FOUNDRY_RESOURCE_ID not set — re-run with it after creating the Foundry resource to grant model access."
fi

"$POC_PYTHON" - "$REPO_ROOT" "$SUBSCRIPTION" "$RG" "$SEARCH" "$APPI" <<'PY'
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import set_key

repo_root, subscription, resource_group, search, app_insights = sys.argv[1:]
result = subprocess.run(
    [
        "az", "monitor", "app-insights", "component", "show",
        "--subscription", subscription, "-a", app_insights, "-g", resource_group,
        "--query", "connectionString", "-o", "tsv", "--only-show-errors",
    ],
    capture_output=True,
    text=True,
)
if result.returncode or not result.stdout.strip():
    sys.exit("Could not retrieve the Application Insights connection string; .env was not updated.")

env_path = Path(repo_root) / ".env"
os.umask(0o077)
if not env_path.exists():
    shutil.copyfile(Path(repo_root) / ".env.example", env_path)
env_path.chmod(0o600)
set_key(env_path, "SEARCH_ENDPOINT", f"https://{search}.search.windows.net", quote_mode="always")
set_key(env_path, "APPLICATIONINSIGHTS_CONNECTION_STRING", result.stdout.strip(), quote_mode="always")
print("Updated .env keys: SEARCH_ENDPOINT, APPLICATIONINSIGHTS_CONNECTION_STRING")
PY

echo "✅ Setup complete. Role assignments can take 5–10 minutes to take effect."
