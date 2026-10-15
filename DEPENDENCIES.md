# Dependencies

Core API, SQLite storage, text/CSV/JSON ingestion, BM25, XLSX source-row parsing, rule checks and JSON-layout draft reports use Python 3.11+ standard library. Linux and macOS are the supported prototype targets. There is no mandatory Python package install.

Optional capabilities are explicit:

| Capability | Existing dependency needed | Missing dependency behavior |
|---|---|---|
| Native PDF text/layout | Poppler `pdfinfo`, `pdftotext` on PATH | Typed capability failure; no fabricated extraction |
| Local image OCR/pixel signature | macOS `sips`, Tesseract with `eng` data on PATH | Capability unavailable; no visual-semantic claim |
| Native XLSX export | Node.js 22+, separately licensed `@oai/artifact-tool` 2.8.94 | Disabled by default; typed `xlsx_capability_unavailable` |
| E5 / EmbeddingGemma 2 retrieval | Existing pinned local snapshot; Torch, Transformers, safetensors, sentencepiece | Semantic mode unavailable; no download fallback |
| Qwen3 0.6B diagnostic | Existing exact hash-pinned weights/tokenizer plus optional local-model packages | Unavailable unless administrator supplies artifacts |
| Gemini reader experiment | Standard-library HTTPS and an existing key, plus exact execution grant | Disabled without grant; no API-server activation flag |

Local-model package declarations are in `pyproject.toml`. The EmbeddingGemma 2 adapter explicitly checks Transformers 5.19.0. No model weights, tokenizer assets, third-party runtime code, npm package, or dependency cache is included. Installing optional dependencies is separate from the core startup procedure; do not install or download them automatically.

The XLSX worker requires an authorized existing runtime. Set `NAVIGATOR_ENABLE_XLSX_WORKER=1`, optionally `NAVIGATOR_NODE_PATH`, and `NAVIGATOR_ARTIFACT_TOOL_MODULE` to an administrator-selected existing module file, or make the package resolvable normally. The worker script is Navigator integration code; the imported SDK is not distributed here. Opt-in indicates configuration only and does not grant a license. See `THIRD_PARTY_NOTICES.md`.

Pinned local Qwen artifacts must be explicitly configured with `NAVIGATOR_QWEN_WEIGHTS` and `NAVIGATOR_QWEN_TOKENIZER`. The SHA checks remain mandatory. Generic downloaded weights are not assumed compatible with the experimental adapter. Startup does not import Torch or load any model unless the corresponding profile is selected.
