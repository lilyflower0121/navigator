# Navigator export validation

Measured locally on 2026-10-15. These are software-contract and synthetic processing checks, not AI-quality approval.

| Check | Observed result | Scope |
|---|---|---|
| Portable renamed regression | 450 tests run, failures 0, errors 0, one optional-runtime class skipped | Python core, parsers, API, jobs, ACL, deletion, replay, calculations and candidate boundaries |
| Optional native XLSX integration | 11 tests passed | Existing separately licensed runtime, synthetic workbook bytes; SDK not included |
| Independent export checks | 4 added checks + 22 related checks passed | Fresh imports, no heavy model import, actual synthetic text ingestion/job/search, worker opt-in, packaged worker path, live OpenAPI |
| HTTP E2E | Passed | Synthetic CSV/text → queued parse → search → evidence answer → numeric rule diagnosis/recognition hold → template draft → restart → deletion/replay revocation |
| CLI/startup | Passed within portable regression | Exclusive private principal creation, live localhost startup, OpenAPI generation and SIGTERM shutdown |
| Package import | All implementation modules imported | Torch/Transformers absent from loaded modules |
| OpenAPI | 69 operations, saved JSON equals regenerated schema | Renamed identifiers and schema consistency |
| Offline wheel build and isolated smoke | Passed | Built with existing setuptools using `--no-index --no-deps --no-build-isolation`; no dependency download. Wheel contains worker and third-party notice, and starts its own authenticated localhost ingestion/job/search flow. |

The 450-test suite explicitly skips the native XLSX execution class when the separately licensed worker is not enabled. With an authorized existing runtime configured, all 11 tests in that class passed separately. The independent subset overlaps the main suite; do not add its count to claim additional independent test coverage. Results are saved as counts in `test-summary.json`, `optional-xlsx-summary.json`, `smoke-summary.json` and `wheel-summary.json`. Raw logs, source originals and original evaluation files are excluded.

An old spreadsheet-source-title assertion initially expected a semantic hold. The current implementation requests source confirmation instead. That expectation was updated to require `needs_confirmation` and the `source_reference` prompt; the newer condition tests remain. The passing result is the post-correction export run, not the initial failure.

Historical original-document evaluations and snapshot-preservation tests were intentionally not copied because they require excluded original files or old source history. Removing those test dependencies does not improve model quality or establish their scenarios on a new host. Native-tool absence on another host produces explicit skips/capability failures. No model weights, original documents, credential state or local database was bundled to make tests pass.

The generic source audit checks caller-provided forbidden text/path terms using casefold/NFKC and ignores Unicode format characters, checks private home/volume paths and credential signatures, and flags prohibited binary/runtime artifacts. The final scan also checks the staged git tree and confirms that the local repository has no imported commits or remote. The staged whitespace check passed after removing a trailing blank line from a test file. Review reports give review-time results; the final local audit records the finalized file sizes separately. No publication occurred during validation.

Known limits remain those in the README: failed historical small-model semantic/evidence evaluation, unexecuted external reader trial, no real-time visual/audio recognition, local prototype identity, optional licensed native output, and no old-store compatibility after namespace renaming.
