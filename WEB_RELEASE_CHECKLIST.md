# Web release and rollback checks

This checklist concerns the India-wide web chatbot. Messaging channels are outside this release. Local acceptance is evidence about the tested checkout; it does not certify the currently deployed service.

## Validated local profile — 4 October 2026

The acceptance candidate uses `gpt-5.4` for care (medium reasoning, low verbosity, 1,600 output-token cap), `gpt-5.4-mini` for routing/search/evidence, and `gpt-4o` for images. The active local knowledge backend is Chroma with 156 chunks and a warmed local embedding model. The complete turn deadline is 90 seconds; search has a 60-second total budget. The preserved production environment can override these defaults, so compare its effective profile before using this validation as release evidence.

Current scenario results and retained attempts are in `reports/india-web-release-2026-10-04/`. There are 15 distinct scenarios; targeted reruns are recorded separately. These results do not verify the public proxy, production credentials, a real notification recipient, or stakeholder acceptance. The local Markdown refresh and verified backup do not update the production knowledge store.

Reproduce the local 15-scenario run with `.venv/bin/python scripts/run_india_web_acceptance.py`. It makes paid model/search calls, uses isolated data, disables actual notifications and retains prior attempts. `--case C03` reruns only that case with its preceding recorded conversation restored. Do not interpret the runner's automated checks as the final transcript review.

Focused offline checks are `test_search_evidence`, `test_veterinary_safety`, `test_web_region_release`, `test_web_operations`, `test_deploy_release` and `test_web_knowledge_refresh`, run with `.venv/bin/python -m unittest MODULE_NAME`. Web source-card checks use `node --test test_static_resource_links.js`. These code regressions are separate from the 15 conversational scenarios.

## Before replacing the running application

- Record the candidate source revision/image ID, previous image ID, effective model names, active RAG backend/collection, and runtime environment-file location. Do not publish credentials.
- Back up the runtime SQLite database, uploaded images, and persistent Chroma directory while application writes are stopped. Preserve the runtime environment separately under its existing access controls. Pinecone data requires its own versioned namespace or provider backup; this local backup does not include hosted vectors.
- Use `scripts/web_release_backup.py --database PATH --storage PATH --chroma PATH --output NEW_PRIVATE_DIRECTORY --quiesced`. Omit `--chroma` only when Chroma is not used. This command refuses an existing destination. Run `scripts/web_release_backup.py --verify BACKUP_DIRECTORY` before proceeding. A failed check is a release blocker.
- If `rag_docs` changed, re-ingest the changed documents into **both the SQLite knowledge store and the active vector store**. Preserve existing PDF knowledge when refreshing Markdown; do not clear the whole store merely to refresh documents. Confirm the retrieved text reflects the edited documents, not only matching chunk counts.
- Use the incremental command below for current Markdown. It also removes stale content-hashed vectors belonging to those documents; ordinary upsert-only ingestion does not. Documents absent from `rag_docs`, PDFs and unrelated knowledge remain untouched.
- Run schema migration twice against a backup copy, then verify integrity and the existing records. New nullable/defaulted columns must not require destructive migration.

## Incremental Markdown refresh

Use the same environment, database path, Chroma path and cached model directory as the serving profile. On a host where those runtime paths are already configured:

```bash
.venv/bin/python scripts/refresh_web_knowledge.py
.venv/bin/python scripts/refresh_web_knowledge.py --apply --quiesced
```

The first command previews changed documents without changing knowledge records. Run the second only with a verified backup and all application/ingestion writers stopped. For Docker, use the complete runtime-mount command in [EC2_DEPLOYMENT.md](EC2_DEPLOYMENT.md). New chunks and local embeddings are prepared first; new vectors must verify before old IDs are removed. SQLite replacement is atomic per document, but the two stores have no shared transaction. If any step fails, keep traffic stopped and rerun or restore the verified backup; do not declare a partial refresh ready. Restart serving processes after success to clear retrieval caches. This command supports SQLite/Chroma; hosted Pinecone requires its own versioned refresh workflow.

## Candidate checks

- Verify the active knowledge collection is populated and the local embedding model is warm. `GET /health` reports readiness without making paid model calls. Cold embedding or an unobserved model is not a successful model smoke test.
- Run the agreed acceptance transcript against the exact candidate, including the real configured retrieval backend. Keep model/search authentication failures, controlled failures, previous attempts, and partial outcomes visible. Do not label a provider's published information as guaranteed availability or pickup.
- Inspect the rendered browser: phone-only answers and their sources, Hindi/child responses, uncertain images, case-location changes, reload/ownership behavior, and honest report/notification status. Verify TLS, secure cookies, request limits and admin authentication through the real proxy.
- If web photo reporting is enabled, use an explicitly authorized controlled report to verify the configured receiver. A successful webhook is channel acceptance; an authenticated operator acknowledgement is separate, and neither establishes dispatch. Confirm failed alerts retry at most three times and exhausted/unacknowledged alerts appear in the admin operations view.
- Verify a process restart recovers a due web-alert retry without concurrently sending the same alert. Provider acceptance immediately before a process failure is an uncertain-delivery window, not an exactly-once guarantee.
- Monitor `GET /v1/admin/operations` with the admin credential for model/search failures, actual RAG outcomes, latency, failed/exhausted alerts and unacknowledged notifications. Operational events contain statuses and exception types, not prompts, phone numbers or locations.

## Retention

`services.web_operations.retention(closed_incident_days=N)` is a dry run. An explicit `apply=True` is required to scrub old closed/resolved **web** reports and their unshared original images. Active reports and other channels are excluded. Choose and document the retention period before applying it; no retention deletion runs automatically. Keep a verified backup until the change is reviewed. Separately expired guest conversation cleanup follows the existing guest-conversation policy.

## Rollback

Stop candidate writes before rollback. Keep the failed candidate's data for diagnosis in a private directory; do not overwrite either the backup or the failing dataset. Restore the verified database, storage directory and local vector directory into a new data directory, then launch the recorded previous image with the preserved environment and restored mounts. Match the database/image paths to the original runtime mount paths; the backup manifest records the original storage root. Recheck SQLite integrity, photo availability, active RAG retrieval, browser ownership and health before switching traffic. Do not restore only the database while keeping unrelated newer image/vector files.

The Docker deployment script now builds and creates a stopped candidate before stopping the old container, takes a private quiesced data copy, preserves the previous container/image, and restores that copy plus the previous container if local startup readiness fails. This involves downtime and requires all other writers to be stopped. It does not roll back a release already accepted by the script, undo external notifications, or replace the verified backup, live model, browser and receiver checks above. See [EC2_DEPLOYMENT.md](EC2_DEPLOYMENT.md) for persistent vectors, cached embeddings and the distinct SQLite deployment profile.
