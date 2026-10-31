# GPT-5.6 application profile — 25 October 2026

Every generative OpenAI role now uses the GPT-5.6 family. Model IDs were checked
against both the local and production API account's model lists before rollout.

| Workload | Model | Reasoning setting |
| --- | --- | --- |
| Care conversation and follow-ups | `gpt-5.6-sol` | medium |
| Image recommendation enrichment | `gpt-5.6-sol` | none, within its 512-token JSON budget |
| Photo assessment | `gpt-5.6-terra` | none, within its 900-token JSON budget |
| Text routing and compatibility query analysis | `gpt-5.6-terra` | none |
| Place extraction | `gpt-5.6-terra` | none |
| Web search | `gpt-5.6-terra` | low |
| Contact evidence review | `gpt-5.6-terra` | none, with its own 20-second cap and the shared turn deadline |
| Admin read-only SQL generation | `gpt-5.6-terra` | none |

The retained Chat Completions text/vision adapter also uses the configured roles
and `max_completion_tokens` for GPT-5.6. Existing non-GPT-5.6 adapter token options
remain compatible. Retrieval embeddings use their dedicated embedding models;
GPT text/vision models cannot replace embedding vectors. The production instance
currently uses SQLite retrieval.

Set all seven model variables from `.env.example` when upgrading an existing
environment: its explicit values override defaults in `config.py`. Restart model
clients after changing model configuration. Prompt-only release activation still
does not require a service restart.

This migration changes model selection and explicit reasoning options. Srini's
prompt text, JSON schemas, deterministic safety checks, request deadlines and
evidence validation remain the same. Earlier acceptance transcripts identify the
models used at the time; they are not rewritten as GPT-5.6 test results.

Official references checked for this migration:

- [GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol)
- [GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra)
- [Reasoning effort](https://developers.openai.com/api/docs/guides/reasoning#reasoning-effort)

Sol and Terra support text/image inputs, Responses and Chat Completions,
structured outputs and web search. Both default to medium reasoning; short JSON
tasks explicitly request none so reasoning cannot consume their small completion
budget before the JSON answer is emitted.
