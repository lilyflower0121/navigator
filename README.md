# Navigator

Navigator is a local modular API prototype: one Python process, one SQLite store, and explicit library, evidence, diagnosis and report modules. It has 69 OpenAPI operations. There is no frontend or cloud deployment in this export. It is not production-ready.

## What works

- Small TXT/Markdown/CSV/JSON files are ingested as original bytes, queued, parsed into versioned segments, and searched with filters, lexical matching or BM25. Original SHA, analysis version and source positions accompany evidence. XLSX source-row parsing uses bounded ZIP/XML inspection and retains cells, headers, formula-cache observations and row positions.
- Native PDF extraction and bounded local image OCR/pixel similarity are implemented behind optional existing tools. Images do not receive general semantic interpretation. Video/audio ingestion is a contract with an explicit unsupported/hold result; live perception, VLM and ASR are not implemented.
- Evidence tasks support bounded references, explicit table lookup/arithmetic, original row bundles, condition clarification, and optional experimental reader candidates. Quotes and citation identity do not prove semantic entailment. Review flags remain attached.
- Defined numeric rules distinguish event time, missing input, lateness, sequence conflicts and hold. This is structured-rule diagnosis; it does not recognize real-time camera or microphone streams.
- JSON-layout reports retain original templates, field/cell mappings, source references and unresolved fields. A validated-calculation bridge and native XLSX integration exist, but native output needs the separately licensed optional runtime. Arbitrary spreadsheet editing and general report formats are not implemented.
- Tenant separation, registered-principal scopes, project membership/source ACL, current-rights rechecks, source versions, job states, idempotency, cancellation/retry, draft review/publication and deletion propagation have local software tests. The identity store is an administrator-owned local prototype, not production SSO.

## Start without downloads

Run from this directory with Python 3.11+:

```sh
python3 -m navigator init-local --auth .local/access.json
python3 -m navigator serve --db .local/module.sqlite --auth .local/access.json --port 4340
```

`init-local` creates a private local test-principal file and refuses to overwrite an existing file. The server binds `127.0.0.1` by default. Open `http://127.0.0.1:4340/healthz` or `http://127.0.0.1:4340/openapi.json`. Use the generated bearer credential only for local authenticated API requests; never commit it. Stop with Ctrl-C. No model, container runtime or external service starts by default.

The core has no pip dependencies. See `DEPENDENCIES.md` for optional tools and licensed-runtime limits. A separate administrator may install this package, but source-checkout startup above requires no installation or network. Python CLI commands are `init-local`, `serve`, and `openapi`:

```sh
python3 -m navigator openapi --output openapi.json
```

Container source is included as a text/API prototype; no image build or deployment is part of this export. An external bind requires explicit allowed Host authorities and disabled in-process worker. See `Dockerfile` and the `NAVIGATOR_ALLOWED_HOSTS` / `NAVIGATOR_ALLOWED_ORIGINS` settings. Optional native tools are not installed in the minimal image.

## Verify locally

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m tests.run_e2e --output evidence/new-http-e2e.json
```

Tests use small synthetic in-memory/file fixtures and local HTTP. They never invoke paid inference or load real models. Native PDF/image tests explicitly skip if their existing tools are missing; XLSX execution tests skip unless the licensed runtime is explicitly enabled. Existing runtime integration can be checked separately without adding its binaries to the repository. The E2E processes synthetic bytes through ingestion, jobs, search, evidence answer, rule diagnosis, draft report, restart, idempotent replay and deletion. Generated evidence and runtime files are ignored by git.

Historical tests requiring excluded original evaluation documents or implementation snapshots are not presented as portable regression tests. The renamed export has its own reported test counts; these are not the old workspace's full count. See `docs/VALIDATION.md` for measured results and scope.

## Quality limits and external trial status

Prior actual small-model evaluation failed: the pinned Qwen3 0.6B diagnostic had semantic/evidence success 0/8 on eight known real-document questions and necessary abstention 0/2. After table-context inclusion repair, both measured ranking modes still had semantic/evidence success 0/4 and necessary abstention 0/1 on the same four known table questions. Better evidence inclusion did not establish answer quality. A prior limited real-photo OCR evaluation was also unsuccessful (0/3 correct). These aggregate results are historical context, not a fresh evaluation of renamed source; original documents, full prompts, raw model outputs and gold files are excluded.

The Gemini reader adapter is prepared but its trial remains **stopped**. Actual generation and token-count calls are both zero. The owner's conditional trial approval is not embedded in this export. Exact key project, ordinary paid plan, remaining budget and tax-inclusive cost conditions remain unresolved in the original environment. This export supplies no execution grant and does not activate Gemini through the HTTP CLI. Provider metadata availability and mock tests are not answer-quality evidence.

All generated answers remain candidates with `semantic_support_verified=false`, `review_required=true`, and no automatic action or promotion into certified calculations or final reports. Production authentication, hostile-file sandbox qualification, backup/retention operations, general multimodal semantic retrieval and reliable grounded QA remain outside demonstrated capability.

## Namespace compatibility

The exported package is `navigator`, configuration variables use `NAVIGATOR_`, and template/MIME/image-signature namespaces have been renamed consistently. No legacy aliases or hidden old identifiers are kept. The default requires a fresh local store: old stores, saved receipts, template IDs or pixel hashes from another namespace are not wire-compatible. Their migration would need a separately reviewed explicit conversion. Original working files and evidence remain untouched outside this export.

## Export scope

This directory contains implementation, portable tests, synthetic fixture text, the integration worker, dependency declarations, OpenAPI, startup/container instructions and review summaries. It excludes credentials/environment files, local databases, model assets, SDK binaries, original/private evaluation documents, raw logs, caches, prior archives, unrelated projects and original git history. Publication destination and audience must be selected before push; no repository visibility or credentials are changed by local preparation.
