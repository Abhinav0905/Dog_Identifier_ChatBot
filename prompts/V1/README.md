# Ask Dorjee V1

The five `.txt` files and manifest are byte-for-byte the evaluated
`single-agent-v1.0.0-2026-10-28` package. The manifest's acceptance section records
the tests at packaging time. The later 50-question run scored **43 pass, 6 partial,
1 fail (86%)**; see `reports/modular-final50-2026-10-30`.

Enable the web adapter with `CHAT_ENGINE=single_agent`. Its default prompt source
is this folder. EC2 can set `SIMPLE_AGENT_PROMPT_DIR` to an atomically activated
local release. GitHub is the publishing source; requests read local verified
files, not GitHub or S3 over the network.

```sh
python scripts/manage_single_agent_prompts.py validate prompts/V1
python scripts/manage_single_agent_prompts.py activate prompts/V1 --current /path/to/single-agent-prompts/current
```

To change or roll back V1 prompts, activate another complete five-file package
with the same command. Each request pins a validated snapshot. A corrupt package
is rejected before the model is called. Do not edit an active release in place.

To roll back the entire engine to V0, set `CHAT_ENGINE=legacy`, retain the legacy
`PROMPT_PACKAGE_DIR`, and restart the service. V0 and V1 have different schemas;
they cannot be interchanged by pointing the same loader at another folder.

Text and ordinary photo answers use the evaluated agent, native web search and
the photo-analysis function. Local DAR photo reporting still uses the existing
structured severity classifier, location gates, incident persistence and alert
delivery. Admin queries also retain the legacy prompt bundle. Thus the five-file
comparison measures the assistant policy, not every operational prompt on disk.

Model calls and tool results use the existing bounded private trace store.
Content capture requires a specific conversation allowlist; hidden reasoning,
images and credentials are omitted. The public health endpoint and response
headers expose release provenance, not private trace content.
