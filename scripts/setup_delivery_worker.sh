#!/usr/bin/env bash
#
# Bootstrap/refresh the Cloud Run Job + Cloud Scheduler entry that drains the
# HealthOS communication delivery outbox (email/realtime/push) in production.
#
# The API service only enqueues outbox rows (it never calls ZeptoMail/Ably/FCM
# from a request). This job must run continuously (currently every minute via
# Cloud Scheduler) or password-reset codes and other mail expire unsent.
#
# The job inherits a copy of the API service's environment (plain vars and
# Secret Manager references), so re-run this script whenever the service env
# changes.
#
# Usage:
#   scripts/setup_delivery_worker.sh
#   PROJECT_ID=... REGION=... IMAGE=... scripts/setup_delivery_worker.sh
#
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-storied-shell-510407-e0}"
REGION="${REGION:-asia-south1}"
SERVICE="${SERVICE:-healthos-api}"
JOB="${JOB:-healthos-delivery-worker}"
SCHEDULER_JOB="${SCHEDULER_JOB:-healthos-delivery-worker-every-minute}"
SCHEDULE="${SCHEDULE:-* * * * *}"
RUNTIME_SA="${RUNTIME_SA:-healthos-cloud-run@${PROJECT_ID}.iam.gserviceaccount.com}"
IMAGE="${IMAGE:-}"

command -v gcloud >/dev/null || { echo "error: gcloud is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "error: python3 is required" >&2; exit 1; }

if [[ -z "${IMAGE}" ]]; then
  IMAGE="$(gcloud run services describe "${SERVICE}" \
    --project="${PROJECT_ID}" --region="${REGION}" \
    --format='value(spec.template.spec.containers[0].image)')"
fi
if [[ -z "${IMAGE}" ]]; then
  echo "error: could not resolve an image; pass IMAGE=..." >&2
  exit 1
fi
echo "Deploying job '${JOB}' with image: ${IMAGE}"

SERVICE_JSON_FILE="$(mktemp -t healthos-service.XXXXXX.json)"
ENV_FILE="$(mktemp -t healthos-worker-env.XXXXXX.yaml)"
trap 'rm -f "${SERVICE_JSON_FILE}" "${ENV_FILE}"' EXIT

gcloud run services describe "${SERVICE}" \
  --project="${PROJECT_ID}" --region="${REGION}" --format=json > "${SERVICE_JSON_FILE}"

SECRET_ENV="$(python3 - "${SERVICE_JSON_FILE}" "${ENV_FILE}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    container = json.load(handle)["spec"]["template"]["spec"]["containers"][0]

plain: dict[str, str] = {}
secrets: list[str] = []
for entry in container.get("env", []):
    ref = (entry.get("valueFrom") or {}).get("secretKeyRef")
    if ref:
        secrets.append(f'{entry["name"]}={ref["name"]}:{ref.get("key", "latest")}')
    elif "value" in entry:
        plain[entry["name"]] = entry["value"]

with open(sys.argv[2], "w", encoding="utf-8") as handle:
    for name, value in plain.items():
        handle.write(f"{name}: {json.dumps(value)}\n")

print(",".join(secrets))
PY
)"

DEPLOY_ARGS=(
  run jobs deploy "${JOB}"
  --project="${PROJECT_ID}"
  --region="${REGION}"
  --image="${IMAGE}"
  --service-account="${RUNTIME_SA}"
  --command=python
  --args=-m,app.domains.communication.delivery.worker,--once
  --env-vars-file="${ENV_FILE}"
  --max-retries=1
  --task-timeout=10m
)
if [[ -n "${SECRET_ENV}" ]]; then
  DEPLOY_ARGS+=(--set-secrets="${SECRET_ENV}")
fi
gcloud "${DEPLOY_ARGS[@]}"

# The scheduler's OAuth identity must be allowed to execute the job.
gcloud run jobs add-iam-policy-binding "${JOB}" \
  --project="${PROJECT_ID}" --region="${REGION}" \
  --member="serviceAccount:${RUNTIME_SA}" --role=roles/run.invoker >/dev/null

RUN_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB}:run"

if gcloud scheduler jobs describe "${SCHEDULER_JOB}" \
    --project="${PROJECT_ID}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${SCHEDULER_JOB}" \
    --project="${PROJECT_ID}" --location="${REGION}" \
    --schedule="${SCHEDULE}" --uri="${RUN_URI}" --http-method=POST \
    --oauth-service-account-email="${RUNTIME_SA}"
else
  gcloud scheduler jobs create http "${SCHEDULER_JOB}" \
    --project="${PROJECT_ID}" --location="${REGION}" \
    --schedule="${SCHEDULE}" --uri="${RUN_URI}" --http-method=POST \
    --oauth-service-account-email="${RUNTIME_SA}"
fi

cat <<EOF
Done.

Trigger a run now:
  gcloud run jobs execute ${JOB} --project=${PROJECT_ID} --region=${REGION} --wait

Backlog/queue status (Cloud SQL):
  select status, count(*) from communication.delivery_job group by status;
EOF
