#!/usr/bin/env bash
# ===========================================================================
# deploy.sh — Build & deploy the Claude Code Usage Monitor to Cloud Run
# ===========================================================================
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - Docker installed (for local build) OR Cloud Build enabled
#   - Required env vars in .env or exported in shell:
#       GCP_PROJECT, GCP_REGION, DISCORD_WEBHOOK_URL,
#       ANTHROPIC_API_KEY, MONTHLY_BUDGET_USD (optional)
# ===========================================================================
set -euo pipefail

# ----------- Config (override via env or edit here) -------------------------
GCP_PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
GCP_REGION="${GCP_REGION:-asia-northeast1}"
SERVICE_NAME="${SERVICE_NAME:-claude-usage-monitor}"
IMAGE="gcr.io/${GCP_PROJECT}/${SERVICE_NAME}"
MONTHLY_BUDGET_USD="${MONTHLY_BUDGET_USD:-100.0}"
BILLING_CYCLE_DAY="${BILLING_CYCLE_DAY:-1}"
# ----------------------------------------------------------------------------

if [[ -z "$GCP_PROJECT" ]]; then
  echo "❌  GCP_PROJECT is not set. Run: export GCP_PROJECT=your-project-id"
  exit 1
fi
if [[ -z "${DISCORD_WEBHOOK_URL:-}" ]]; then
  echo "❌  DISCORD_WEBHOOK_URL is not set."
  exit 1
fi
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "❌  ANTHROPIC_API_KEY is not set."
  exit 1
fi

echo "==> Project : ${GCP_PROJECT}"
echo "==> Region  : ${GCP_REGION}"
echo "==> Service : ${SERVICE_NAME}"
echo "==> Image   : ${IMAGE}"
echo ""

# 1. Build & push image via Cloud Build (no local Docker required)
echo "==> Building image with Cloud Build..."
gcloud builds submit \
  --project="${GCP_PROJECT}" \
  --tag="${IMAGE}" \
  .

# 2. Deploy to Cloud Run
echo "==> Deploying to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
  --project="${GCP_PROJECT}" \
  --image="${IMAGE}" \
  --region="${GCP_REGION}" \
  --platform=managed \
  --min-instances=1 \
  --max-instances=1 \
  --memory=256Mi \
  --cpu=1 \
  --timeout=60 \
  --set-env-vars="ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY},DISCORD_WEBHOOK_URL=${DISCORD_WEBHOOK_URL},MONTHLY_BUDGET_USD=${MONTHLY_BUDGET_USD},BILLING_CYCLE_DAY=${BILLING_CYCLE_DAY}" \
  --no-allow-unauthenticated

echo ""
echo "✅  Deployment complete!"
echo ""
echo "Service URL:"
gcloud run services describe "${SERVICE_NAME}" \
  --project="${GCP_PROJECT}" \
  --region="${GCP_REGION}" \
  --format="value(status.url)"
echo ""
echo "Trigger a manual check:"
echo "  curl -X POST \$(gcloud run services describe ${SERVICE_NAME} --region=${GCP_REGION} --format='value(status.url)')/check \\"
echo "       -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\""
