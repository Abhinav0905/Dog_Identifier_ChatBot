ASK DORJEE — STANDALONE SINGLE AGENT

Run from the repository root with Python 3.10+ and the openai package installed.
Supply OPENAI_API_KEY through your normal environment/secret-management method.
Do not put credentials in prompt files or traces.

One question:
python -m simple_agent --question 'Where can an injured community dog get treatment in Kochi?' --output /tmp/dorjee-question-new

One photo:
python -m simple_agent --question 'What can you see in this photo?' --image /absolute/path/dog.png --output /tmp/dorjee-photo-new

The fixed 50-question acceptance set:
python -m simple_agent --suite core50 --output /tmp/dorjee-core50-new

The same 50 plus eight guardrail/photo checks:
python -m simple_agent --suite all58 --output /tmp/dorjee-all58-new

Always use a new output directory. For suite photos, --image-root can point to
the repository containing example_images/dog_1.png, example_images/dog_2.png and
static/assets/dar-logo.png. The package can be used separately for one question
or an explicitly supplied photo; it does not import the application.

FILES
__main__.py: CLI, fixed suite, runtime provenance and result persistence.
agent.py: SingleAgent and AgentConfig; one main model chooses tools and answers.
catalog.py: immutable PromptCatalog loads the five inspectable prompt files.
tools.py: SearchTool registers native web_search; ImageAnalysisTool implements
          analyze_photo using native vision on only the supplied image ID.
tracing.py: incremental JSONL trace recorder and JSON result writer.
prompts/system.txt: three-line main instruction.
prompts/guardrails.txt: existing shared policy, India/privacy and off-topic rules.
prompts/care.txt: brief immediate care, conversation handling and needs-based advice.
prompts/search.txt: provider search, contact-role checks and same-facility evidence.
prompts/photo.txt: cautious image observations and reporter-symptom priority.
evaluation/cases.json: unchanged questions, order and expectations from the prior
                      experiment. Expectations are never sent to the model.

This package does not call the old router, query-analysis, contact-review or
enrichment agents. Their applicable behaviour is expressed as instructions to
the one main agent. Admin SQL belongs to the separate admin application and is
not a chatbot tool. There are no production database reads, S3 reads, rescue
notifications or deployment actions in this package.

RESULTS AND TRACES
runtime.json: configuration, code/prompt hashes and test-set sizes.
prompts.json: exact prompts used, including all guardrails.
test-plan.json: fixed ordered cases and their expectations.
<case>.json and results.json: answers, citations, status, timing and trace events.
traces/<case>.jsonl: events persisted during execution, including errors.

Trace events include model outputs, search actions/source URLs, function calls
and results, timing, usage, and prompt hashes. They do not include API secrets,
image base64 or hidden reasoning. Native search does not expose every retrieved
page excerpt; a source URL in the trace is not proof of every claimed field.

A completed API response is not an accuracy pass. The runner leaves verdicts
UNREVIEWED. A separate semantic review must judge the saved acceptance criteria.
Target: at least 45 fully passing answers out of the core 50; partials do not
count as passes. The extra eight checks are reported separately. Repeated runs
must remain separate: do not combine the best answer per question into a score.

Validation of the tool loop:
python -m unittest test_simple_agent_modular -v

The model defaults remain gpt-5.6-terra for both calls and can be selected with
--model / --vision-model. Model access depends on the API account. Documentation:
https://developers.openai.com/api/docs/guides/function-calling
https://developers.openai.com/api/docs/guides/tools-web-search
https://developers.openai.com/api/docs/guides/images-vision
