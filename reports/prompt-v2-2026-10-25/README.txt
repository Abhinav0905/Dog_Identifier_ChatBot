Ask Dorjee — Srini v2 external prompt verification (25 October 2026)

Implementation
- Prompts now come from versioned local text files: prompts/runtime-v2/.
- Srini's 14 supplied files are copied byte-for-byte. Files 01–09 are runtime
  prompts; 00, 10, README, CHANGES and the diff are reference material.
- Existing ancillary builders use 18 support text files rather than live policy
  strings embedded in Python. Historical source remains commented and archived.
- GitHub is the review/versioning source. EC2 reads its own local copy; no remote
  GitHub/S3 fetch occurs during a chat request. An activation command validates
  and atomically switches a release, with old releases retained for rollback.
- A request pins one version/hash for router, care, search, evidence and worker
  threads. /health and response headers expose the selected version/hash.

Technical tests
163 focused tests passed. Full details: technical-validation.json.
The broader historical suite still has existing failures reproduced at commit
41b7afd. See baseline-comparison.json. Do not describe the full suite as green.

Live backend acceptance
15 selected cases, real localhost HTTP API with configured live models/search.
All 15 returned HTTP 200 and reported the expected prompt version/hash.
Manual transcript verdicts: 11 PASS, 4 PARTIAL, 0 FAIL.

Partial cases
A1: Palampur research/administrative leads do not establish the requested local
    clinical treatment access; a general IVRI clinical page is insufficient.
A5: Route planning and community assessment work, but vaccination/sterilisation
    are not separately explained as community-health measures in that answer.
B1: Correct wound safety is retained, but prayer/ritual is introduced without
    being mentioned by the user; the grandmother's protective intent is missed.
B4: Panchayat/public-health tone works, but cost-effectiveness is not explained.

Other observations
The Kochi answer contains a routine optional closing offer, contrary to the final
response contract, despite meeting that particular geographic discovery rubric.
The photo case uses a deliberately black synthetic image; no real injured-dog
photo, clinical diagnosis or rescue dispatch is certified by this run.

Files
backend-results.json: exact questions, answers, response payloads, verdicts,
                     rationale, sources, version/hash and timings.
backend-results.csv:  same questions/answers and review columns for sharing.
baseline-comparison.json: failures compared with the unchanged Git commit.
technical-validation.json: focused checks and their result.

Deployment state
Local implementation verified. Public EC2 activation still requires a known SSH
hostname/alias and username; this report does not claim production was changed.
Instructions: docs/EXTERNAL_PROMPTS.md.
