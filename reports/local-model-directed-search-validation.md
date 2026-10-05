# Local validation — 17 September 2026

The changes are on `feature/model-directed-search`. Review the running chatbot at
**http://localhost:8001**. No remote Git push, merge to `main`, or AWS change was made.

The original staged changes are preserved byte for byte. The branch was created
from the existing dirty checkout at `98b075f0342cb32663a2a826d8dfda358ec0e6b0`;
existing fixes were retained. Nothing was committed during this work.

## Behavior implemented

- The model selects answer, search, or clarify from the current question and recent
  conversation. Search receives the original question, corrections, and constraints.
- Text search supports veterinary colleges, hospitals, clinics, public services,
  rescue groups, and other relevant sources. It bypasses the old NGO answer cache,
  provider selection, source-domain exclusions, and regional-NGO fallback.
- Prior replies are supplied to research as reference data. Source descriptions
  are retained without rewriting their city or adding a “Verified” label.
- Contact replies receive a separate check against fetched source text. The model
  matches the requested institution; code checks that the institution and phone
  occur in a quoted source passage. Missing evidence produces uncertainty.
- Humane motorbike guidance and conversation-aware fallbacks handle feared bites
  and recurring community-dog encounters. India scope, animal-welfare relevance,
  photo jurisdiction, and the HTTP interface remain in place.
- Conversation metadata records the action, routing source, whether web search
  completed, result type, and source links. Citations render as clickable links.

## Automated checks

**437 Python tests passed** in an isolated test database/storage environment.
The selected suites cover model routing, source rendering/evidence, provider
corrections, normal pronoun follow-ups, absent contact evidence, reload/ownership,
router/search outages, urgent guidance, city/location precedence, and existing
photo/intake and legacy service behavior. The city tests include 120 city/phrasing
subcases across 20 Indian cities. **8 browser-rendering Node tests passed.**

```bash
.venv/bin/python -m unittest test_unit test_text_turns test_text_turns_review \
  test_model_directed_search test_search_evidence test_behavior_context \
  test_veterinary_safety test_local_validation_runner test_followup_integration \
  test_india_city_rescue_routing test_conversation test_ngo_followup \
  test_web_search_reliability test_twilio_whatsapp -q
node test_static_resource_links.js
```

An earlier broader run also included `test_scrape_dar_site`. Its existing
`test_project_crawl_stops_at_depth_and_writes_manifest` fails because the scraper
calls `relative_to(repository)` on a temporary directory outside the repository.
The scraper and its test match the original checkout; neither was changed here.
This unrelated failure is not counted among the 437 passing tests.

## Real local conversations

The browser replay used the configured model and web-search services, followed
by a page reload. After the final context-precedence correction, the daily-encounter
turn was repeated in that conversation to confirm that a shorter model paraphrase
could not lose the known motorbike detail. The complete transcript is in
[live-deb-browser-validation-final.json](local-model-directed-search/live-deb-browser-validation-final.json).

| User turn | Final local result |
| --- | --- |
| Animal help in Palampur | Web search ran; returned a source-backed administrative animal-husbandry contact, without claiming rescue pickup or a Palampur branch of a Dharamshala NGO. |
| Only the Veterinary College in Palampur number | Fresh search ran. The source check could not establish the college phone, so the reply explicitly withheld a number. Previous providers were not substituted. |
| Dog chasing a motorbike | Care answer with traffic safety and humane handling; no web search. |
| “He may bite me if I slow down.” | Retained motorbike context and acknowledged bite uncertainty; no location request or web search. |
| Daily encounter at the same intersection | Retained recurring community-dog context, with humane longer-term guidance; no location request or web search. |

The final sequence recorded search flags **true, true, false, false, false**.
Reload restored the conversation and source links.

The independent [Chennai transcript](local-model-directed-search/live-chennai-validation-final.json)
passed all four checks: a named veterinary hospital contact, “only its number,”
a fictional college after that contact, and the fictional college in a new
conversation. The real hospital number matched the
[TANUVAS contacts page](https://www.tanuvas.ac.in/contacts.php). Both fictional
requests withheld a number. All four performed web searches; response times were
11.57, 8.55, 14.55, and 10.29 seconds. Telephone reachability was not tested.

The [injury transcript](local-model-directed-search/live-injury-validation.json)
retained context through heavy bleeding after vehicle trauma and an unconscious-dog
follow-up. Both replies supplied immediate care guidance without a location gate;
the second explicitly advised against giving water. No notification was sent.

## Remaining limitations

- **The Palampur college phone remains unresolved in the final build.** Earlier
  search drafts produced a number, but the final source check could not support
  it from readable evidence. This is an honest missing-contact result, not a
  successful college-phone lookup. It should be reviewed before any deployment.
- Source availability still limits contact answers. The bounded evidence reader
  currently reads public HTTPS HTML/text; inaccessible, PDF-only, scanned, or
  JavaScript-only evidence can lead to a withheld contact. It reads up to four
  sources, with a 12-second fetch budget, plus a reviewing model call.
- Institution matching still involves model judgment. Literal source/phone
  checks reduce the demonstrated substitution problem but do not guarantee that
  all future contacts, coverage claims, or availability statements are correct.
- Contact checking adds latency and an API call. During development, real tests
  exposed wrong-institution replies even with citations; those failures led to
  the independent source check. The final Chennai correction tests passed.
- Live advice can vary in wording. The injury follow-up mentioned feeling for
  breath near the nose; observing chest movement alone would better preserve
  the earlier precaution about keeping hands and face away.

## Local state and restart

The runner uses a fresh operational SQLite database, separate uploads/storage,
and a copy of the existing knowledge data. The isolated SQLite and Chroma stores
both contain 156 knowledge records. Only the revised behavior document was
re-ingested into that copy; the original knowledge stores were retained.
See [the ingestion check](local-model-directed-search/behavior-ingestion-report.json).

Local conversation cookies work over HTTP. Slack, rescue webhooks, and outbound
WhatsApp are disabled. Credentials were not added to Git or copied into reports.

```bash
.venv/bin/python scripts/run_local_validation.py --state-dir \
  /private/var/folders/38/5zhj7jdx6q11jzsf6xz6sxj40000gn/T/gaia-local-validation-06slr7vg
```

The running server log is `server.log` inside that state directory. Relevant log
fields are `text_turn ... action=... search_ran=... result_kind=...` and
`Contact evidence review status=... fetched_sources=...`.

The initial staged/unstaged patches, status, and working-tree backup are in:

```text
/var/folders/38/5zhj7jdx6q11jzsf6xz6sxj40000gn/T/gaia-model-directed-search-baseline-t7_juh8y
```

The temporary state and backup can disappear when temporary files are cleaned.
The transcripts in this report directory remain with the source checkout.
Deployment awaits a subsequent user instruction.
