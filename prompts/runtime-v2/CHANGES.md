## v2.2 - 25 October 2026

File 03 prioritizes one to three usable local treatment options and explicitly searches pet or small-animal clinics without restricting results to street/community/NGO/free-care language. Admission and fees still require confirmation. Runtime review keeps branch contact association even when a publisher does not use the heading "branches". Original file 03 is archived; Deb's verbatim reference and shared policy remain unchanged.

## v2.1 - 25 October 2026

File 04 adds a clarification to use the full literal publisher identity, one literal branch address per provider claim, branch-bound contacts, and structured citation fields. Its original v2 text is archived. No change to Deb's full reference, shared cultural policy, JSON schemas or placeholders. Runtime code separately fixes false competing-provider detection, bounded evidence-review timing and first-aid preservation.

# Ask Dorjee prompts — v1 → v2 changes

**Why:** DAR's testing (Deb Jarrett, Sep 2026) found answers "less compassionate" than her own
Custom GPT, plus wrong providers and an answer that suggested stones/sticks/horns. v1 had
patched each failure with a specific "do not" rule, but carried only one line of DAR's tone and
cultural guidance. v2 leads with Deb's values and voice and keeps every existing safety rule.

**Unchanged across all files:** JSON output schemas, placeholders, routing logic, evidence and
citation rules, medical/poisoning/traffic safety rules.

## Shared policy block (embedded in 02, 03, 05, 06; byte-identical)
- **New "VOICE AND VALUES" section** at the top, condensed from Deb's prompt: kind, calm,
  non-judgmental; listen → validate → explain → gently guide; assume care (fear is earned,
  beliefs come from elders and faith, harm comes from fear and misinformation); users are
  partners; never frame India as "behind" or compare it with the West; dogs are part of the
  community; never put dogs above people; why before what; myth handling with her exact
  example framing; rabies essentials; DAR's humane solutions (ABC, vaccination, feeding with
  sterilisation, reporting injured dogs, teaching children); never-do list; success = behaviour change.
- **"Street/community dogs", not "strays"**, unless the user uses that word.
- **Cultural-humility bullet extended:** India's religious/linguistic/regional diversity; don't
  assume faith, caste, language or community, or attribute beliefs to a group.
- **Audience bullet → all 7 of Deb's modes** (child, elder, village, school, NGO,
  government/panchayat, donor) plus her "write for the most vulnerable audience" default.
- **Two deliberate deviations from Deb's text:**
  1. Her line "running, shouting, throwing stones escalates fear" omits *stones*: per her Sep 21
     request that such items never be brought up, and v1's existing rule on unsolicited tactics.
  2. "Validate before information" yields to safety in an emergency or actual bite exposure:
     the action goes first, and the acknowledgement can share its sentence.

- **Guards added after self-review** (so the new voice can't break existing behaviour):
  - Precedence: voice rules never override a requested format (phone-only), JSON output,
    length limits, or the safety/evidence rules.
  - Validation only when a feeling is expressed; why-first only for behaviour/safety advice.
  - Rabies content scoped to *human* exposure (not dog-on-dog injuries), "at least 15 minutes"
    to match the existing medical rule, child audience gets no mention of death.
  - Humane-solutions content only when relevant (no appended lessons).
  - Elder mode "avoid direct contradiction" still states safety facts (e.g. no turmeric).
  - DAR grounding "shapes the approach only; never the default contact for a case" — except
    when the application's geofence/location context places the case in DAR's service area,
    where recommending DAR is appropriate (keeps the geofence path working).

## 01 text router
- Questions about beliefs, myths, traditions, remedies or dog behaviour route to `answer`
  (humane education), not `search`.
- Audience list expanded to all 7 modes so a "panchayat" or "village" request isn't misrouted.

## 02 care
- Opening identity: "kind and thoughtful". (Deliberately *not* "created with DAR": that raises
  the model's prior to refer cases to DAR/Kangra — the exact Palampur failure.)
- Final contract: for behaviour questions, briefly explain **why** the dog reacts before the steps.
- Final contract: in bite-risk behaviour answers, allow **one calm conditional rabies sentence**
  once per conversation (Deb wants rabies "repeatedly reinforced"; v1 suppressed it as an
  unrequested checklist).

## 03 web search
- Much of India has no rescue NGO: a vet hospital/college/clinic in the requested town is often
  the right first answer; an out-of-town organisation isn't local help unless shown to serve it.
  (Palampur failure.)
- Don't repeat stigmatising source labels ("stray menace", "dog terror") or present
  culling/poisoning/relocation advocacy as guidance.

## 04 contact evidence review
- "Correct facts, not voice": keep the draft's kind tone, acknowledgement and culturally humble
  framing when rewriting; don't import stigmatising source labels.

## 05 vision (photo triage)
- "Stray dogs" → "street and community dogs".
- Describe only what's visible; never infer neglect, abandonment, mistreatment or ownership.
- Moderate severity reworded: "has not been cared for" → "shows visible signs that it needs care".
- Gentle triage summary, no graphic injury/death; never suggest untrained handling.

## 06 enrichment
- Actions phrased kindly and positively, no stigmatising labels (plus the shared block).

## 07 query analysis
- `general_dog_question` explicitly includes beliefs and myths.

## 08 place extraction, 09 admin NL→SQL
- No change (no user-facing language).

## Package files
- `01_Dev_India_Cultural_Guardrails_Verbatim.txt` (partial, 6 sections) →
  `00_Deb_DAR_Humane_Education_GPT_Full.txt` (complete, verbatim, sourced).
- README rewritten; `10_regression_test_cases.txt` added.

## Watch in QA
- v2 runtime prompts are ~40% longer in total (shared block grew). Check latency/cost on the vision and
  enrichment paths, where the long shared block matters least.
