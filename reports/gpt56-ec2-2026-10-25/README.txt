GPT-5.6 and external prompt deployment - 25 October 2026

Deployed application source: 85f389a

GPT-5.6 is active locally and on EC2. Srini's text-file package is versioned in GitHub and activated as a validated local release on EC2; chat requests do not fetch GitHub or S3. Changes are reviewed/pushed, copied to a release, validated, and activated atomically. The package has not been moved to S3.

Technical validation: 165 focused automated tests, 7 direct live role probes, 3 candidate HTTP checks and 3 public HTTPS checks passed. Browser response rendering also passed. These technical results do not mean every answer met Dev's expectations.

Manual review of the six live HTTP transcripts: 4 PASS, 2 FAIL. Indore and Kochi both failed useful local treatment discovery. Indore also omitted first aid. No numbers were invented, but uncertainty-only fallbacks remain incomplete answers.

Read validation-summary.json for the complete scope, limitations, rollback/data preservation and next work. Read candidate-http-results-56.json and public-http-results.json for exact questions, generated answers, requirement verdicts and reasons. The earlier 15-question results remain separately labelled under reports/prompt-v2-2026-10-25; those have not been rerun under GPT-5.6.

No credentials, environment file contents, real incident images or operational database snapshots are included here.
