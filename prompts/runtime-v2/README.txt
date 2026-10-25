RELEASE v2.1 - 25 October 2026
File 04 now clarifies full source names, branch-local addresses and citation placement.
Its original Srini v2 wording is retained at prompts/archive/04_contact_evidence_review_system_srini_v2_original.txt.
Other numbered prompts, including Deb's full verbatim reference, are unchanged.

ASK DORJEE — RUNTIME PROMPT PACKAGE (v2, Oct 2026)

Contents
- 01–09_*.txt   Assembled runtime prompts, one per application path. Named placeholders
                ({{CURRENT_MESSAGE}}, {{RECENT_CONTEXT}}, {{RUNTIME_CONTEXT_JSON}}) are filled
                per request; JSON output schemas are unchanged from v1.
- 00_Deb_DAR_Humane_Education_GPT_Full.txt
                Deb Jarrett's complete DAR Humane Education GPT prompt, verbatim from her
                Sep 21, 2026 email. Reference only — not loaded at runtime. Replaces v1's
                partial "01_Dev_India_Cultural_Guardrails_Verbatim.txt".
- 10_regression_test_cases.txt
                QA cases from DAR's reported failures plus cultural-sensitivity probes.
- CHANGES.md    What changed in each prompt and why.

Shared policy block
02, 03, 05 and 06 each embed the same "SHARED ASK DORJEE POLICY" block. It MUST stay
byte-identical across all four. Check: run
  awk '/^SHARED ASK DORJEE POLICY$/,/^Use the user.s requested language/' FILE | md5sum
on each file; all four hashes must match. In the application source this block is
likely a single constant — edit it there once rather than editing these files.

Where Deb's guidance lives at runtime
| Deb section                         | Runtime location                                  |
|-------------------------------------|---------------------------------------------------|
| Identity & purpose                  | Shared block: VOICE AND VALUES; 02 opening line   |
|   (DAR grounding = approach only,   |   never a default referral)                       |
| Cultural positioning / core values  | Shared block: VOICE AND VALUES + cultural bullet  |
| Myths & beliefs                     | Shared block (with her example framing); 01, 07   |
| Role in behaviour change / success  | Shared block: VOICE AND VALUES                    |
| Dog behaviour education (why first) | Shared block; 02 final contract                   |
| Rabies & public health              | Shared block; 02 closing conditional sentence     |
| Humane solutions (ABC etc.)         | Shared block: VOICE AND VALUES                    |
| Never-do list                       | Shared block: VOICE AND VALUES                    |
| Adaptive modes (all 7) + "most      | Shared block: audience bullet; 01 router          |
|   vulnerable audience" default      |                                                   |
| (No stigmatising source language)   | 03 web search, 04 evidence review                 |
| (No neglect inference from photos)  | 05 vision                                         |

Not in the prompts by design
- DAR helpline / contact numbers: supplied by the application's geofence logic for the
  Dharamsala area, never from model recall. The shared block explicitly permits recommending
  DAR when that location context places a case in DAR's service area.
- Dharamsala-specific community assumptions: the service covers all of India, and the
  prompts forbid inferring religion, caste or community from location.
