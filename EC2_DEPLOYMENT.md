# EC2 web deployment

This guide covers the India-wide web chatbot. Complete [WEB_RELEASE_CHECKLIST.md](WEB_RELEASE_CHECKLIST.md) for every release. The deployment scripts have local checks; they do not certify external services or the deployed answers.

## Public access requires HTTPS

Use a domain with a valid TLS certificate through Nginx or an HTTPS load balancer. Share `https://<your-domain>/`. A plain HTTP public IP is not a production release: browser geolocation requires a secure origin and session/admin credentials must be protected in transit.

- Permit `443/tcp` to the HTTPS endpoint. Use public `80/tcp` only for HTTPS redirects or certificate validation, never for serving the chatbot.
- Restrict SSH to the administrator's IP. Do not expose backend port `8000` publicly.
- The Docker script defaults to `HOST_PORT=127.0.0.1:8000` for an on-instance reverse proxy. A load balancer deployment must deliberately set its private binding and security-group rules.
- Preserve the existing TLS configuration. The Docker script does not replace Nginx configuration or provision certificates. Confirm HTTP redirects to HTTPS before opening user traffic.
- Set the existing secure-cookie and trusted-proxy configuration for the actual proxy, then verify the browser cookie and ownership behavior through the public domain.

## Choose and verify one retrieval profile

**Full Docker / Chroma:** install Docker on the target instance and allocate enough measured memory/disk for the active embedding model, Chroma collection, uploads and backup copies. The Docker image installs the full requirements. Keep `RAG_VECTOR_BACKEND=chroma` when Chroma is the intended production backend. The runtime mounts are:

| Data | Persistent host path | Container path |
| --- | --- | --- |
| SQLite | `.deploy-data/dharmasala.db` | `/app/data/dharmasala.db` |
| Original images | `.deploy-data/storage` | `/app/data/storage` |
| Chroma | `.deploy-data/chroma_db` | `/app/data/chroma_db` |
| Cached embedding weights | `.deploy-data/model-cache/hub` | `/app/data/model-cache/hub` |

`DATA_DIR` overrides the host root. The script injects the container paths without editing `.env`. Existing persistent directories are never replaced during seeding. Only when the Chroma directory is missing, it copies `CHROMA_SEED_DIR`, or the previous stopped container's `/app/chroma_db`, or the repository's `chroma_db`, in that order. Only when the embedding cache is missing, it copies `EMBEDDING_CACHE_SEED_DIR` (the Hugging Face **hub directory**) or the previous stopped container's `/root/.cache/huggingface/hub`. Explicit source paths must contain complete, trusted, matching data. Custom previous paths must be supplied explicitly. Keep symlinked cache snapshots and their referenced blobs together.

Provision the configured embedding model's weights in that cache before deployment. Startup warming is local-only and does not download models. A populated collection with cold embeddings fails the script's local readiness gate. An existing but empty directory is deliberately not overwritten; populate it through the documented knowledge-ingestion workflow while writers are stopped. If `rag_docs` changed, re-ingest the changed Markdown into SQLite **and** Chroma, preserve PDF chunks, and inspect retrieved text before release.

**Lightweight systemd / SQLite:** `deploy/ec2/deploy_micro.sh` installs `requirements.micro.txt`, forces the SQLite retrieval backend and runs one worker. This is a separate serving profile; it does not provide Chroma retrieval or need embedding weights. Its historical port-80 Nginx setup is only a bootstrap configuration behind restricted access. Configure TLS/redirects before exposing the application. This helper currently rewrites runtime-path/backend entries in its `.env` and does not supply automatic rollback; preserve that file and use the backup/release checklist before invoking it. Prefer the full Docker procedure for the persistent Chroma deployment.

Acceptance evidence must identify the profile that actually serves requests. Passing SQLite-only tests cannot certify the Chroma deployment, and vice versa.

## Prepare the release

Keep the existing environment file in place. On a new instance only, copy `.env.example` to `.env` and set the API credentials, strong admin password, intended model/backend, and public deployment settings. Do not commit or paste secrets. `.env` is excluded from Docker builds and backup artifacts; preserve it separately using its existing access controls.

Record the source revision and use a unique image tag. Stop other ingestion/maintenance writers, pause public traffic with the existing proxy's maintenance control, and make a verified quiesced backup as described in the release checklist. Confirm there is disk space for the current data, rollback copy, failed data if needed, and both images. The script stops only the named application container, not independently running writers.

```bash
IMAGE_NAME=gaia-chatbot:<release-id> ./deploy/ec2/deploy.sh
```

The script:

1. Acquires a deployment lock, builds the image and creates a stopped candidate while the previous container is still running. Candidate creation validates Docker configuration; application startup is checked later.
2. Stops the previous container, copies the complete data directory to private `.deploy-backups/<release-id>/data`, and records both image IDs. `.env` is preserved without modification.
3. Seeds only missing vector/cache directories, preserves the previous container under a release-specific name, and starts the candidate on the configured backend binding.
4. Waits up to `STARTUP_TIMEOUT_SECONDS` (default 180) for local database, India-boundary and selected knowledge readiness. The Chroma profile requires a populated, warmed collection. No paid model request occurs in this probe. Hosted Pinecone requires a separate live retrieval gate.
5. On failure, stops/removes the failed candidate, keeps failed data in the backup directory, restores the quiesced data copy, and restarts the previously running container. On success, retains the old stopped container and private backup for reviewed rollback.

There is a maintenance window. A failure to restore data is reported and prevents an automatic restart with uncertain data. Review permissions and startup logs before retrying. Do not remove a stale deployment lock until confirming no deployment is running. The script's plain data copy is additional recovery protection; use `scripts/web_release_backup.py` for integrity/hash verification. Keep the backend behind maintenance access until all release gates pass.

## Verified Docker backup with original image paths

Build the candidate image containing the backup script first. Pause traffic and stop every application/ingestion writer before this sequence. Run from the repository root; replace the image tag, application name or host data root when overridden. The read-only source mount deliberately retains `/app/data/storage`, because those are the image paths stored in the production database. No environment file or credentials are needed for backup verification.

```bash
docker stop --time 45 gaia-chatbot
backup_id="verified-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p .deploy-backups
chmod 700 .deploy-backups
docker run --rm --network none \
  -v "$(pwd)/.deploy-data:/app/data:ro" \
  -v "$(pwd)/.deploy-backups:/backups" \
  gaia-chatbot:REPLACE_WITH_RELEASE_ID python scripts/web_release_backup.py \
  --database /app/data/dharmasala.db --storage /app/data/storage \
  --chroma /app/data/chroma_db --output "/backups/${backup_id}" --quiesced
docker run --rm --network none \
  -v "$(pwd)/.deploy-backups:/backups:ro" \
  gaia-chatbot:REPLACE_WITH_RELEASE_ID python scripts/web_release_backup.py \
  --verify "/backups/${backup_id}"
```

Stop on any failed command. For a SQLite-only deployment omit `--chroma`; keep it for Chroma. The verified artifact contains the database, original images and vectors; separately preserve the runtime environment and matching cached embedding weights. Do not resume traffic until refresh/candidate checks pass. These commands do not create a certificate, send notifications, or test model credentials.

## Refresh edited Markdown in the Docker runtime stores

Build the candidate image containing the edited documents first. With traffic paused, a verified backup taken and all application/ingestion writers stopped, run this Bash sequence from the repository root. Replace the image tag and host data path if your deployment overrides them. The image must already contain the refresh script. The first invocation previews; the second explicitly applies the reviewed changes.

```bash
refresh_cmd=(docker run --rm --env-file .env
  -e DB_PATH=/app/data/dharmasala.db -e STORAGE_DIR=/app/data/storage
  -e CHROMA_PERSIST_DIR=/app/data/chroma_db -e HF_HUB_CACHE=/app/data/model-cache/hub
  -v "$(pwd)/.deploy-data:/app/data"
  gaia-chatbot:REPLACE_WITH_RELEASE_ID python scripts/refresh_web_knowledge.py)
"${refresh_cmd[@]}"
"${refresh_cmd[@]}" --apply --quiesced
```

The command refreshes current Markdown in SQLite and the configured Chroma collection, removing obsolete IDs only for changed documents after replacement vectors verify. Existing PDF and unrelated knowledge remains. Cached embedding weights are required; no model API or automatic download is used. A failed refresh is a release blocker: keep writers stopped and rerun or restore the verified backup. Restart serving processes after success to invalidate retrieval caches. If the Docker deploy starts with the old container already stopped, a failed candidate restores that prior stopped state; reopening traffic remains an explicit release step.

## Reverse proxy and verification

Add this location to the **existing TLS server** (retain its certificate and redirect configuration). Match the body limit to the configured application image limit; 110M accommodates the current 100 MB default plus multipart overhead.

```nginx
location / {
    client_max_body_size 110M;
    client_body_buffer_size 1M;
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 5s;
    proxy_send_timeout 120s;
    proxy_read_timeout 120s;
}
```

Check `https://<your-domain>/health`, the rendered browser, authentication and ownership through that proxy. `models: not_observed` after startup does not prove credentials/model service works; an authorized live acceptance request must establish that separately. Also verify actual active RAG retrieval, human/animal safety cases, phone-source binding, and any configured web-report receiver according to the release checklist. Channel acceptance and an operator acknowledgement do not prove rescue dispatch.

After successful verification, reopen traffic. Retain the previous image/container and verified backup for the agreed rollback period. A rollback after users resume writing requires a deliberate data-reconciliation decision: do not blindly restore an old snapshot and discard new reports. User data retention is explicit and dry-run by default; neither deploy success nor a later cleanup authorizes deleting reports or backup copies.
