# TinyCTX skygazer: remediation and validation handover

**Prepared:** 26 September 2026  
**Repository:** `itzpingcat/TinyCTX`  
**Reviewed branch:** `skygazer`  
**Reviewed commit:** `902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0`  
**Purpose:** An implementation backlog and verification plan for a coding LLM or maintainer.  
**Scope:** Known defects and source-backed risks from the inspected areas, plus explicit checks for areas not fully audited. This is not a claim that every defect in the repository has been discovered.

## 1. Read this before modifying code

The immediate goal is to stabilise the existing architecture, especially authority boundaries, truthful action/result recording, context integrity and asynchronous execution. Do not begin with a rewrite, new integrations or a wholesale framework replacement.

The reviewed branch still resolved to the commit above when checked through GitHub. Source references below are pinned to that commit. If the working branch has moved, compare the relevant functions first; mark findings already fixed and retain regression tests rather than reintroducing obsolete changes. Do not reset or overwrite unrelated working-tree changes.

### Evidence and limitations

| Label | Meaning |
|---|---|
| **C** | Confirmed in the inspected source: the described code path or contract mismatch is present. This does not mean a deployed exploit or full integration failure was executed. |
| **R** | The relevant source pattern was additionally reproduced with an isolated local check. These are small stand-ins/excerpts, not the project's actual test suite. |
| **V** | Verification-first item. Reproduce it against the actual code and resolved dependencies before implementing a purported bug fix. |
| **H** | Hardening, design clarification or an evidence/quality requirement. Do not describe it as an already demonstrated exploit. |

The repository was inspected through the GitHub connector. A direct clone into the execution environment failed because the host could not be resolved. Accordingly, **the full TinyCTX test suite was not run, no production patch was applied, no Docker stack was started, no wheel was built, no live model benchmark was performed and no deployed security exploit was attempted**. Eleven isolated checks were run using Python 3.13.5, aiohttp 3.13.3 and PyYAML 6.0.3. The project declares Python >=3.14; all final regression/integration validation must be rerun on its supported runtime. See section 7 for exactly what the isolated checks establish.

**Priority interpretation:** P1 should be addressed before unattended or broadly accessible use; P2 is needed for dependable maintenance, releases and supported behaviour. No P0 deployed-compromise claim is made. Priority is not a formal vulnerability severity score.

### Instructions for the coding LLM

1. Read repository-specific development instructions and the relevant implementation/tests. Treat this document as a backlog, not as proof that every hypothesis is true in the current checkout.
2. Establish a baseline and write a failing regression against the **actual production function/path** before each confirmed bug fix. An isolated copy of an algorithm is useful diagnosis but not adequate regression coverage.
3. Fix related invariants coherently. In particular, do not patch one context-trimming index while leaving other hooks able to generate invalid tool histories.
4. Preserve persisted conversation history, permission semantics and documented public interfaces unless an explicit migration/design decision authorises a change. Never suppress real results, grant extra authority, disable a security test or catch every exception merely to obtain a green build.
5. Use small reviewable changes and deterministic local fixtures. Do not use real credentials, user data, public test targets or paid model calls without authorisation. Do not deploy, push changes or change repository access/rules without authorisation.
6. For each V/H item, record the reproduction or design decision. A disproved hypothesis should be closed with evidence, not implemented anyway. A material residual risk needs a maintainer decision, not an invented acceptance by the coding LLM.
7. Finish with a change log, migration notes, exact verification commands/results and an explicit list of unresolved items. Never report tests as passing when they were not run.

### Preserve these existing design choices unless evidence requires a change

The branch has useful architectural boundaries: persistent conversation branches, central execution-time capability checks, explicit permission declarations, a separate workspace/internal-data layout and a separate shell sandbox. Preserve these rather than flattening everything into a new monolith.

`ROOT` deliberately does not imply every named permission in the grant set. `BACKEND_EXEC` is a location capability. `UNTRUSTED_EXEC` authorises effects that cannot be statically classified and should not be presented as a safe narrow-effect grant. `NETWORK_READ` is explicitly not an exfiltration barrier. Tool visibility is not tool authorisation. An internal admin key can intentionally confer full gateway authority; SEC-04 is about making that contract explicit or replacing it deliberately. See [permission semantics][src-permissions], [shell classification][src-shell-perms] and [gateway authentication][src-gateway].

## 2. Backlog index

The detailed entries are actionable units, not a request for one enormous patch. Evidence qualifiers apply to each entry; combined labels mean that the entry includes both an observed defect and related hardening/verification work.

| ID | Priority | Evidence | Work item |
|---|---|---|---|
| [SEC-01](#sec-01) | P1 | C + R | Reject ambiguous booleans at every authority boundary |
| [SEC-02](#sec-02) | P1 | C + R | Close child-path escapes in grep and glob |
| [SEC-03](#sec-03) | P1 | C | Authorise the actual shell execution location |
| [SEC-04](#sec-04) | P1 | H: documented design decision | Make the gateway master-key trust model explicit |
| [SEC-05](#sec-05) | P1 | V + H | Harden filesystem operations against check/open races |
| [CTX-01](#ctx-01) | P1 | C + R | Preserve complete tool-call/result exchanges across every transform |
| [CTX-02](#ctx-02) | P1 | C + R | Done — reject system prompts exceeding the configured context fraction |
| [CTX-03](#ctx-03) | P1 | C + H | Make final payload budgets model-aware and truthful |
| [CTX-04](#ctx-04) | P1 | C + R | Done — preserve author and parent metadata when copying history entries |
| [CTX-05](#ctx-05) | P2 | H; static cost concern | Measure and bound context assembly cost |
| [AGT-01](#agt-01) | P1 | C | Preserve real tool results at the cycle limit and terminate clearly |
| [AGT-02](#agt-02) | P1 | C | Actually process corrective post-completion follow-ups |
| [AGT-03](#agt-03) | P1 | C for ordering; V for full persistence path | Restore deferred tools after registration and verify durable selection |
| [AGT-04](#agt-04) | P1 | C | Keep retries, fallbacks and stream hooks from corrupting output |
| [AGT-05](#agt-05) | P1 | C | Use one effective model identity for context, vision and execution |
| [TOL-01](#tol-01) | P1 | C | Generate correct schemas and validate tool arguments once |
| [TOL-02](#tol-02) | P1 | C | Use structured tool outcomes instead of scanning output text |
| [TOL-03](#tol-03) | P2 | C | Respect discovery toggles and independent embedding models |
| [TOL-04](#tol-04) | P2 | C | Namespace embedding caches by the complete embedding identity |
| [TOL-05](#tol-05) | P2 | C + H | Align discovery visibility, execution requirements and deterministic ordering |
| [ASY-01](#asy-01) | P1 | V: high-confidence failure hypothesis | Verify and repair streaming retry semantics |
| [ASY-02](#asy-02) | P1 | C + H | Propagate cancellation through queues, streams and tools |
| [ASY-03](#asy-03) | P1 | C + H | Bound admission, queues, output and total request duration |
| [ASY-04](#asy-04) | P1 | C + H | Make startup, module lifecycle and shutdown exception-safe |
| [SHL-01](#shl-01) | P1 | C | Remove blocking subprocess execution from the sandbox HTTP loop |
| [SHL-02](#shl-02) | P1 | C | Carry timeout budgets through the sandbox protocol |
| [SHL-03](#shl-03) | P1 | H with source-backed gaps | Bound subprocess output and supervise whole process trees |
| [FIL-01](#fil-01) | P1 | C + H | Make edits atomic and conflict detection reliable |
| [FIL-02](#fil-02) | P2 | C + R for line numbering | Fix view line numbers, exact editing semantics and file-handle ownership |
| [FIL-03](#fil-03) | P2 | C + H | Bound file/search work and make grep backends agree |
| [API-01](#api-01) | P1 | C + R | Read complete bounded request bodies and validate their structure |
| [API-02](#api-02) | P2 | V + H | Specify lane identity, disconnect and resume semantics |
| [RUN-01](#run-01) | P1 | C + H | Compute state from the actual attach point and isolate reset generations |
| [DB-01](#db-01) | P1 | C + H | Make state, flags and checkpoints safe under concurrent writers |
| [DB-02](#db-02) | P2 | C + H | Make migrations and corrupt-state handling explicit |
| [CFG-01](#cfg-01) | P1 | C + R | Done — resolve shared API-key variables before scrubbing them |
| [CFG-02](#cfg-02) | P1 | C | Done — make supported configuration fields reach their consumers |
| [REL-01](#rel-01) | P2 | C + H | Run enforceable checks on the development branch |
| [REL-02](#rel-02) | P2 | V + H | Verify distributable package assets and make releases reproducible |
| [REL-03](#rel-03) | P2 | C | Reconcile operator documentation with executable behaviour |
| [REL-04](#rel-04) | P2 | H; no performance claim established | Demonstrate context efficiency with reproducible evaluations |

## 3. Detailed remediation tasks

<a id="sec-01"></a>

### SEC-01: Reject ambiguous booleans at every authority boundary

**Priority:** P1  
**Evidence:** C + R  
**Source locations:** [Configuration parsing][src-config]; [tool argument coercion][src-handler]; [gateway elevation][src-gateway].

**Problem.** `_parse_permission_set()` grants a capability when `bool(value)` is true. YAML `file_write: "false"` therefore grants `file_write`. Several other configuration parsers use the same coercion. `_coerce_args()` accepts arbitrary non-empty strings as true, and the elevation endpoint interprets `reset` through `bool(...)`. These are distinct authority/configuration boundaries, not merely display preferences. The quoted-YAML failure was reproduced in isolation.

**Required work.** Require actual booleans for security-sensitive YAML/JSON fields, or explicitly parse a small documented set of string values and reject everything else. Prefer strict JSON booleans for HTTP and tool execution. Apply validation before permission classification and execute the exact same validated argument object afterwards. Do not turn an invalid value into a permissive default. Preserve explicitly false overrides. Inspect bridge enablement and tool overrides for the same pattern.

**Acceptance tests.** Native true/false behave correctly; quoted "false", "0", "no", null, lists, dictionaries and arbitrary strings are rejected or explicitly parsed according to the published contract. Test configuration loading, HTTP elevation and a permission-sensitive tool argument. Assert that invalid input produces no permission change and no tool invocation.

<a id="sec-02"></a>

### SEC-02: Close child-path escapes in grep and glob

**Status:** Done — grep and glob now reject out-of-root symlink and parent-traversal results.

**Priority:** P1  
**Evidence:** C + R  
**Source locations:** [Filesystem `_run_py_grep`, `resolve`, `grep`, `glob_search`][src-filesystem].

**Problem.** The Python grep fallback validates the starting directory but then calls `read_text()` on each child found by `os.walk()`. File symlinks are followed even though directory traversal does not follow symlinked directories. A symlink inside the workspace can therefore expose file contents outside the configured roots when the fallback is used. `glob_search()` also passes the caller's pattern directly to `Path.glob()` without validating each result; `../*.txt` can enumerate outside the starting root. Both filesystem patterns were reproduced against temporary, non-sensitive files.

**Required work.** Use a shared containment policy for every candidate, not only the starting directory. Reject parent-traversal and absolute glob patterns, or securely validate every resulting path before inspecting or returning it. Resolve symlink targets against workspace plus explicitly authorised read-only roots. Do not return disallowed paths, metadata or content. Make the Python and ripgrep paths enforce the same policy. Treat concurrent symlink swaps separately under SEC-05.

**Acceptance tests.** Force both `has_rg=False` and the ripgrep path. Test an out-of-root file symlink, directory symlink, dangling symlink, symlink into an explicitly allowed read-only root, `../*`, nested parent traversal and normal in-root files. A marker placed outside all allowed roots must never appear in output. Run tests entirely in temporary directories.

<a id="sec-03"></a>

### SEC-03: Authorise the actual shell execution location

**Status:** Done — local shell execution now requires explicit backend access when the sandbox is unavailable.

**Priority:** P1  
**Evidence:** C  
**Source locations:** [Shell `_dispatch` and settings][src-shell]; [shell capability classifier][src-shell-perms].

**Problem.** When `sandbox_url` is disabled, `_dispatch()` runs locally even with `backend_access=False`. The classifier adds `BACKEND_EXEC` only when the argument is true; it does not know the effective execution location. A configuration change can therefore move a previously sandboxed call into the main process/container without adding the location capability described by the permission model. This is a conditional deployment-path defect, not evidence of a network exploit.

**Required work.** Resolve the effective backend before authorisation and bind that decision to execution. Any main-container/host execution must require `BACKEND_EXEC`, regardless of whether the caller explicitly requested it or the sandbox is disabled. Fail closed when a required sandbox is unavailable. If a deliberately trusted local-only mode is retained, name it explicitly, validate its grants at startup and document the changed boundary; do not silently infer it from a missing URL.

**Acceptance tests.** With no `BACKEND_EXEC`, a caller cannot run locally when the URL is empty, the sandbox is unavailable, or `backend_access=True`. Authorised sandbox calls still work. Check malformed policy/command paths, and ensure the classifier and dispatcher cannot choose different backends for one call.

<a id="sec-04"></a>

### SEC-04: Make the gateway master-key trust model explicit

**Priority:** P1  
**Evidence:** H: documented design decision  
**Source locations:** [Gateway authentication, CLI identity and administration endpoints][src-gateway]; [permission model][src-permissions]; [README][src-readme].

**Problem.** The gateway bearer key is effectively an instance-administration credential: callers can supply `cli_username`, elevate users, write workspace files and request shutdown. Some routes deliberately trust the key rather than a user capability. This is stated in parts of the code and is not an unauthenticated bypass. It becomes dangerous if operators mistake this key for a limited-user chat credential. The blank-key middleware already rejects protected requests; preserve that behaviour.

**Required work.** Choose and document one model before changing authorisation. For an admin-only gateway, label the key as a master credential everywhere, keep binding conservative, prohibit distributing it to limited users, and document impersonation and workspace access. For a multi-user API, introduce separately scoped credentials, server-bound identities, per-route grants and conversation ownership checks; never trust a client-supplied username to establish authority. Do not silently change the CLI contract. Review whether the API's `admin` field means full overrides, effective capabilities, or possession of `ROOT`, and name it accurately.

**Acceptance tests.** Blank/wrong keys fail. An admin-only deployment demonstrably has no promise of isolation between holders of the same key. If scoped tokens are introduced, low-privilege tokens cannot impersonate another user, elevate, shut down, read another lane or bypass filesystem permissions. CLI bootstrap remains usable without weakening runtime checks.

<a id="sec-05"></a>

### SEC-05: Harden filesystem operations against check/open races

**Priority:** P1  
**Evidence:** V + H  
**Source locations:** [Filesystem path resolution and opens][src-filesystem]; [gateway workspace endpoints][src-gateway]; [shared workspace mount][src-compose].

**Problem.** Resolving a path and checking containment before opening it is not a complete race-resistant boundary when another process can mutate the shared workspace. Some text operations use `O_NOFOLLOW` on the final component, but parent components, image/document reads and gateway `Path` operations use different paths. No live symlink-race exploit was executed in this review; verify the actual threat model and supported operating systems.

**Required work.** Centralise safe filesystem access. For the Linux container boundary, consider descriptor-relative traversal with verified directory components and no-follow opens, or an equivalent beneath-root primitive. Keep read-only-root policy distinct from writable-root policy. Ensure atomic replacement cannot escape via a swapped parent. Do not advertise race resistance based only on `Path.resolve()` or final-component `O_NOFOLLOW`. Include document/image reads, attachments and file delivery in the audit.

**Acceptance tests.** Use barriers to swap a parent or final component between validation and access. All operations must either remain within the authorised root or fail without exposing/changing an outside marker. Cover read, write, prepend, edit, image/document view, upload and outbound delivery. Record unsupported platform guarantees explicitly.

<a id="ctx-01"></a>

### CTX-01: Preserve complete tool-call/result exchanges across every transform

**Priority:** P1  
**Evidence:** C + R  
**Source locations:** [Context budget trimming][src-context]; [context optimisation hooks][src-ctx-tools]; [agent follow-ups][src-agent]; [image expansion][src-ai].

**Problem.** There are multiple related defects. Budget trimming clears calls from a text-bearing assistant entry, then scans for results starting at that same assistant index, so it removes none. Deduplication returns `None` when all calls are suppressed and the assistant text is empty; the pipeline interprets that as no change, leaving the calls. Age trimming can remove calls while retaining tool-role stubs. Tokenade handling can clear calls without coordinated result handling. Follow-up and image transformations can insert user-role messages between a multi-call assistant batch and its results. The first budget and dedup patterns were reproduced in isolation.

**Required work.** Define an exchange-aware transformation model and one final protocol validator. Remove or compact a call together with its result, or preserve both with an explicit compacted result. Never leave a tool result without its call, an unresolved call in a completed payload, or a user/system message splitting a required result batch. Make hook return semantics explicit: unchanged, replace, or remove. Run validation after all hooks and provider-specific image conversion. Do not repair payloads by executing tools again.

**Acceptance tests.** Test text-bearing and empty assistants, one/many calls, repeated identical calls, deduplication, ageing, tokenade, byte/token trimming, vision results and corrective follow-ups. Validate after each combination and on the final wire payload. Include an error/abort after only some calls in a batch. The database must retain the truthful original outcomes even when the outbound context is compacted.

<a id="ctx-02"></a>

### CTX-02: Protect the current task and fail explicitly on irreducible overflow

**Status:** Done. System prompts exceeding the configurable context fraction are rejected and surfaced through the normal agent error event. Ordinary history trimming may still evict the originating user message by design; preserving it would require a separate history policy.

**Priority:** P1  
**Evidence:** C + R  
**Source locations:** [Context `assemble()`][src-context]; [tokenade transform][src-ctx-tools].

**Problem.** The budget loop deletes the oldest non-system entry until the estimate fits, with no explicit protection for the current request. If that request is oversized, it can disappear completely. If only system content remains above the limit, the loop breaks and returns an over-budget payload. Both control-flow outcomes were reproduced with a synthetic token counter. Merging consecutive user messages before eviction can also enlarge the eviction unit beyond the intended current task.

**Required work.** Mark the active request/current tool exchange structurally, not by fragile position guesses after merging. Evict eligible historical exchanges first. If protected content cannot fit, return a typed, user-visible overflow error or use a documented explicit partial-input/reference policy. Do not pretend to have processed omitted material. Set budgets for core prompts, retrieved memory and tools, and handle system/tool-schema-only overflow before calling a provider. Ensure tokenade rejection is visible as rejection, not disguised as normal task completion.

**Acceptance tests.** A single oversized user message, oversized attachment, all-system overflow, huge tool schemas, consecutive group-chat users and a long active tool exchange produce explicit, bounded behaviour. No successful request silently loses the latest user instruction. Property tests assert protected content survives every successful assembly.

<a id="ctx-03"></a>

### CTX-03: Make final payload budgets model-aware and truthful

**Priority:** P1  
**Evidence:** C + H  
**Source locations:** [Context token counting and post-assembly hooks][src-context]; [agent model construction/attempt loop][src-agent]; [provider payload adaptation][src-ai]; [model context contract][src-config].

**Problem.** Counting uses `o200k_base` or a characters/4 approximation, multiplied by `token_fuzz`. The budget and image estimate are selected from the initial primary/session model, while fallbacks reuse already assembled messages. Post-assembly hooks and provider image expansion can change the payload after reported counts are finalised. The configuration calls `context` a history budget in code but broader documentation describes the model window; output-reserve semantics therefore need an explicit decision, not a blind subtraction that could double-reserve tokens.

**Required work.** Define physical model window, input budget, output allowance and safety margin separately. Count the final provider-shaped messages and tool schemas for the model being attempted, including reasoning replay, image representation and protocol overhead. Use model/provider tokenisation where available and label estimates honestly elsewhere. Reassemble for smaller/different fallbacks. Recalculate metadata after final transformations and reject impossible payloads. Coordinate with AGT-05 rather than maintaining independent model-selection logic.

**Acceptance tests.** Test exact boundary, one-token overflow, system-only overflow, large tool schemas, reasoning replay, images, a hook adding content and a smaller-context fallback. Assert the sent payload matches reported usage. Compare estimates with reported provider usage for each supported model family and document the tolerance.

<a id="ctx-04"></a>

### CTX-04: Preserve author and parent metadata when copying history entries

**Status:** Done. Context transforms now copy history entries with complete dataclass metadata, including attribution and ancestry.

**Priority:** P1  
**Evidence:** C + R  
**Source locations:** [`ctx_tools._copy()`][src-ctx-tools]; [HistoryEntry defaults and attribution][src-context].

**Problem.** `_copy()` reconstructs `HistoryEntry` with role/content/id/index/calls/result ID/tags but omits `author_id` and `parent_id`. Their defaults replace the original values. A transform applied before attribution can therefore remove a real user's label. The same pattern was reproduced with a minimal dataclass. This affects context attribution; do not claim it changes the separate `cycle.caller` permission identity.

**Required work.** Use `dataclasses.replace(entry, **overrides)` or an equally complete copy operation. Preserve all metadata by default, including future fields. Make tag invalidation explicit only for destructive content transforms. Document which transforms may intentionally change identity metadata; ordinary trimming and normalisation must not.

**Acceptance tests.** Create a fully populated HistoryEntry and apply every relevant transform. Assert author, parent, ID, result ID and unrelated metadata survive, while explicitly replaced fields and destructive tags change as intended. Confirm a transformed user entry retains its attribution in the final prompt.

<a id="ctx-05"></a>

### CTX-05: Measure and bound context assembly cost

**Priority:** P2  
**Evidence:** H; static cost concern  
**Source locations:** [Context ancestor loading/recount loop][src-context]; [database ancestor and state traversal][src-db].

**Problem.** Each assembly loads the ancestor chain, applies hooks and repeatedly renders/re-tokenises the remaining entries as individual entries are removed. State loading also walks the chain. This creates a potentially expensive repeated full-history/recount path. No repository-scale latency or complexity benchmark was executed, so do not report an observed performance regression or speedup.

**Required work.** Add measurements first. Preserve archival history while bounding the working set and using checkpoints where valid. Cache immutable entry/token costs by model and content version; update totals incrementally where transformations allow it. Use exchange-aware eviction so performance work does not reintroduce CTX-01. Invalidate caches on content, model, tools or hook output changes. Keep expensive processing off the event-loop hot path where appropriate.

**Acceptance tests.** Benchmark histories of increasing length and size, including many tool exchanges and branching state. Record p50/p95 assembly latency, memory and event-loop lag. Verify cached and uncached payloads are identical. Check that scaling is not dominated by repeated full tokenisation and that old history remains retrievable.

<a id="agt-01"></a>

### AGT-01: Preserve real tool results at the cycle limit and terminate clearly

**Status:** Done — final-cycle tool results are preserved and execution ends with one explicit limit message.

**Priority:** P1  
**Evidence:** C  
**Source locations:** [AgentCycle generation loop][src-agent].

**Problem.** On the final allowed cycle, the code executes requested tools and then replaces each result with a limit-reached instruction. It increments the counter and leaves the loop, so there is no guaranteed model call to produce the requested summary. This can perform side effects, replace their recorded outputs with a misleading stub and end without a useful final response.

**Required work.** Never overwrite an actual result with a scheduling/control message. Define the limit precisely: tool rounds, inference rounds and finalisation budget must be distinct. Either reserve one final tool-disabled model call or emit a truthful deterministic terminal message containing action status and the limit reason. Keep actions/results in durable history. Ensure repeated terminal handling cannot execute a tool twice. Validate zero/negative limits under CFG-02.

**Acceptance tests.** Use a stub model and a counted side-effect tool at `max_tool_cycles=1` and at the normal boundary. Assert exactly one execution, the real result in history, one terminal outcome, and no model instruction that can never be acted on. Test success, expected failure, cancellation and multiple calls in the last batch.

<a id="agt-02"></a>

### AGT-02: Actually process corrective post-completion follow-ups

**Priority:** P1  
**Evidence:** C  
**Source locations:** [AgentCycle pending_followups and no-tool branch][src-agent]; [PostCompletionAction contract][src-context].

**Problem.** Post-completion hooks may enqueue a corrective user message. The agent writes it, but when the original completion has no native tool calls the no-tool branch can still break immediately. The promised correction is therefore recorded without a subsequent inference. When native calls are present, follow-ups are written before their results, which overlaps the exchange-integrity defect in CTX-01.

**Required work.** Make a pending corrective follow-up an explicit reason to continue the inference loop. Append it only after any outstanding tool-result batch is complete. Give corrective rounds their own bounded retry policy, define whether provisional output is replaced or labelled, and report exhaustion clearly. Do not let corrective hooks create an unbounded autonomous conversation.

**Acceptance tests.** A completion containing only malformed text-encoded calls triggers the hook, then another inference. Mixed native/text calls retain a valid result batch. A model that never corrects itself stops with an explicit diagnostic within the configured bound. A completion without follow-ups remains unchanged.

<a id="agt-03"></a>

### AGT-03: Restore deferred tools after registration and verify durable selection

**Priority:** P1  
**Evidence:** C for ordering; V for full persistence path  
**Source locations:** [AgentCycle setup][src-agent]; [module tool wiring][src-module-registry]; [tool enable/search][src-handler].

**Problem.** Saved `enabled_tools` are replayed against a new handler before module tools have been registered. Most saved module names therefore fail the membership check and are skipped. Passive search and always-on tools can mask the defect. The inspected restoration code is not proof that every explicit selection is durably written; trace that write path as part of the fix.

**Required work.** Register module/MCP tools first, then restore valid saved selections and apply a documented precedence policy for explicit configuration overrides. Persist explicit enablement at the right session boundary; keep passive one-turn enablement transient. Prune removed names without discarding valid selections. Reapply current permission checks independently of saved visibility. Do not invent a disabled-tool policy from `always_on=False`, which currently means deferred rather than forbidden.

**Acceptance tests.** Disable passive discovery, explicitly enable a deferred tool, complete the turn, start another turn and restart the process. The selection survives where documented. Removed tools are ignored cleanly; revoked permissions still deny execution; passive-only tools do not become permanent. Test override precedence and dynamically discovered MCP tools.

<a id="agt-04"></a>

### AGT-04: Keep retries, fallbacks and stream hooks from corrupting output

**Priority:** P1  
**Evidence:** C  
**Source locations:** [`_stream_inference()` and outer thinking accumulation][src-agent].

**Problem.** Text from a failing model can already have reached the client before fallback begins; there is no explicit replacement/reset event. Outer `thinking_chunks` accumulate events across model attempts, so a failed attempt's thinking can be stored with the successful attempt. Hook `flush()` output is appended directly instead of being passed through subsequent hooks, breaking pipeline composition when multiple stream hooks exist.

**Required work.** Track attempts explicitly. Separate provisional client output, committed response text and reasoning for the successful attempt. Decide whether to stop after partial delivery or support labelled/retractable attempts; never concatenate incompatible attempts as one answer. Reset all attempt-local state on fallback. Pipe flushed output through downstream hooks in order and define abort flushing. Preserve the intended NO_REPLY suppression semantics without leaking suppressed provisional content.

**Acceptance tests.** Model A emits text/thinking then fails; model B succeeds. Persist only the committed attempt and render a coherent client result. Test failure before first token, during a tool call, hook buffering across token splits, two composable hooks, cancellation and NO_REPLY split over multiple chunks.

<a id="agt-05"></a>

### AGT-05: Use one effective model identity for context, vision and execution

**Status:** Done — model overrides, attachments, context budgeting, fallback attempts, and vision handling now share the effective model identity.

**Priority:** P1  
**Evidence:** C  
**Source locations:** [Agent model selection and image unwrapping][src-agent]; [runtime attachment construction][src-runtime]; [model configuration][src-config].

**Problem.** The agent supports a session model override, but `_execute_tool()` checks vision using `config.llm.primary`, and runtime attachment construction also uses the global primary. Fallback attempts can have different capabilities from that primary. This can discard usable image input or supply unsupported input based on the wrong model. Budget consequences are covered by CTX-03.

**Required work.** Introduce an explicit resolved model/attempt context reused by attachment preparation, context assembly, image handling and the provider request. Preserve original attachment bytes/references so choosing another model does not irreversibly lose input. Validate stale session overrides instead of allowing an inconsistent model-chain lookup. Define a clear error or adaptation policy when the next model lacks vision or another required capability.

**Acceptance tests.** Test text-only primary plus vision session override, vision primary plus text-only override, vision/non-vision fallback combinations and unknown saved model names. Assert that capability checks and the actual request always refer to the same effective model, with no silent attachment loss.

<a id="tol-01"></a>

### TOL-01: Generate correct schemas and validate tool arguments once

**Priority:** P1  
**Evidence:** C  
**Source locations:** [Tool registration, type-to-schema conversion and coercion][src-handler]; [filesystem typed arguments][src-filesystem].

**Problem.** Stringified annotations are only resolved for a small primitive-name map. With postponed annotations, types such as `list[str]`, `list | None` and `Literal[...]` fall back to a string schema. Tuple schemas can omit `items`. Required fields, unknown fields and the top-level JSON-object shape are not validated through a complete schema before calling the function. Permissive coercion can also disagree with the semantic contract of a parameter.

**Required work.** Resolve annotations for trusted registered Python functions using supported introspection, with an explicit unresolved-type failure path. Handle nullable values, collections, literal enums, nested objects and defaults. Respect the schema dialect supported by each provider rather than sending unsupported constructs. Validate/coerce once, apply defaults, reject unknown or invalid arguments and use that identical object for classification and execution. Do not evaluate untrusted remote schema text as Python. Preserve native MCP schemas rather than lossy annotation reconstruction.

**Acceptance tests.** Register actual functions with postponed annotations, optional lists, booleans, enums, dictionaries, tuples and nested types. Compare generated schemas with expected shapes. Send JSON arrays/null/scalars, missing/extra fields and malformed values; no invalid call executes. Test that classifiers receive exactly the same defaults and values as implementations.

<a id="tol-02"></a>

### TOL-02: Use structured tool outcomes instead of scanning output text

**Priority:** P1  
**Evidence:** C  
**Source locations:** [ToolError handling][src-handler]; [agent error heuristic][src-agent]; [shell output handling][src-shell]; [sandbox exit_code][src-sandbox].

**Problem.** `ToolError` is converted into `success=True`. The agent separately guesses failure by looking for substrings such as `error: ` in output. This can mark a successful file read containing error examples as failed, while an expected failure without those words looks successful. Shell helpers mostly return strings; `_run_sandbox()` discards the structured exit code supplied by the service. Non-zero exit status and domain-specific no-match semantics are therefore not reliably represented end to end.

**Required work.** Introduce or extend a typed outcome carrying success/error classification, output, exit status, timeout/cancellation and optional attachments. Treat expected tool failures as failures that the model can recover from, not successes. Keep stdout/stderr as data. Define search/test-command exit semantics explicitly, and transport the sandbox result instead of throwing its metadata away. Preserve call IDs even on malformed dictionary-shaped calls. Do not rely on changing error-message wording to fix status reporting.

**Acceptance tests.** A successful read containing a traceback or the word error remains successful. ToolError remains failed without a magic prefix. Test `false`, missing executables, timeout, grep no-match, grep invalid regex, negative/signal exits and normal output. Stored and streamed statuses agree and preserve the original call ID.

<a id="tol-03"></a>

### TOL-03: Respect discovery toggles and independent embedding models

**Priority:** P2  
**Evidence:** C  
**Source locations:** [Passive/ranked discovery][src-handler]; [agent embedder selection][src-agent]; [independent discovery settings][src-config]; [runtime embedder factory][src-runtime].

**Problem.** `passive_search()` takes the plain-BM25 branch when `not bm25_on`; enabling vectors while disabling BM25 therefore still runs keyword search. Separately, the agent chooses one embedder using `search.embedding_model or passive.embedding_model`, although the configuration promises the two paths can select different models. Search/cache calls can be labelled with one configured model name while using an embedder selected for the other path.

**Required work.** Implement and test all four BM25/vector toggle combinations. Resolve each path's embedder from its own effective configuration, or explicitly reject distinct models if that capability is unsupported. When a selected backend is unavailable, use a documented fallback policy and a diagnostic; do not silently run an explicitly disabled backend. Ensure cache/model identifiers describe the actual vectors generated. Keep explicit enablement separate from passive enablement.

**Acceptance tests.** Test neither, keyword-only, vector-only and hybrid modes with spies on the actual ranking/embedding calls. Configure different embedding models and dimensions for passive and explicit search. Assert each uses its own model, avoids mixed vectors and degrades only according to the documented policy.

<a id="tol-04"></a>

### TOL-04: Namespace embedding caches by the complete embedding identity

**Priority:** P2  
**Evidence:** C  
**Source locations:** [Embedder process-wide cache][src-ai]; [embedding templates][src-config].

**Problem.** The in-memory embedding cache is keyed by `(model, kind, raw_text)`. It does not include endpoint identity or the query/document template that determines the actual submitted text. Two configurations using the same model string and raw text but different endpoints or templates can reuse the wrong vector. Cached list objects are also returned directly; callers' mutation behaviour needs verification.

**Required work.** Use a non-secret embedding fingerprint including normalised endpoint, actual model/version or configuration identifier, relevant dimensions/options, kind and template or final templated text. Invalidate persistent caches when their fingerprint/schema changes. Never include raw credentials in cache keys or logs. Consider immutable or defensive-copy return values. Audit ToolVectorStore and RAG/knowledge-graph caches for equivalent identity and dimension assumptions before changing their formats.

**Acceptance tests.** Two endpoints with the same model label do not collide. Changing a template/dimension invalidates the relevant entries. Repeated identical effective requests still hit cache. Failed/partial vectors are not cached as success; mixed-dimension results are rejected cleanly. Mutating one returned vector cannot corrupt unrelated future results.

<a id="tol-05"></a>

### TOL-05: Align discovery visibility, execution requirements and deterministic ordering

**Priority:** P2  
**Evidence:** C + H  
**Source locations:** [Tool visibility, discovery and definition ordering][src-handler]; [permission expansion][src-permissions].

**Problem.** Execution expands required permissions, but `_tool_visible()` checks the unexpanded static/listing set. A tool can therefore be shown to a caller who cannot execute it under the expanded requirement. `tools_search()` searches the registry without the same caller-aware filtering, and tool definitions iterate over a set union, making ordering unstable across processes. None of these observations by itself bypasses the central execution guard.

**Required work.** Reuse the same requirement-expansion semantics for visibility where statically decidable. Define whether discovery may reveal unavailable tool names/descriptions and make documentation match that decision. If minimal visibility promises hiding, apply it to exact and fuzzy discovery as well. Sort emitted definitions deterministically without changing ranking semantics. Keep dynamic classifiers authoritative at invocation time and do not hide every dynamic tool merely because some invocations are denied.

**Acceptance tests.** A network-write tool requiring expanded read permission is hidden or accurately labelled when read is denied. Exact/fuzzy search obey the chosen disclosure policy. Definition order is stable across interpreter/hash-seed changes. Permission-denied hallucinated calls still cannot execute.

<a id="asy-01"></a>

### ASY-01: Verify and repair streaming retry semantics

**Priority:** P1  
**Evidence:** V: high-confidence failure hypothesis  
**Source locations:** [`LLM._stream_with_retry()`][src-ai]; [Tenacity documentation][ref-tenacity].

**Problem.** The retry decorator wraps an async generator. Exceptions arising during iteration may not be retried by a decorator that only retries the call returning the generator. The implementation also turns many provider errors into yielded LLMError events, which are not retry exceptions. Tenacity was not installed in the review runtime, so this item must first be reproduced against the project's resolved dependency version; it is not reported as a completed repository test.

**Required work.** Build a deterministic fake streaming endpoint/generator and count attempts. If confirmed, implement retries around the awaited request/iteration boundary, with bounded backoff and explicit retryable connection/status categories. Define how Retry-After, read timeout and partial output are handled. Coordinate with AGT-04: retry before first output is different from replacing an answer after partial delivery. Do not stack independent retry loops into an unexpectedly multiplied request budget.

**Acceptance tests.** Fail connection establishment and first iteration, then succeed. Check the precise attempt count, delay policy, terminal error type and queue-slot cleanup. Test failures after text or tool fragments, non-retryable 4xx, selected 429/5xx and cancellation during backoff. Unsupported/truncated streams never execute partial calls.

<a id="asy-02"></a>

### ASY-02: Propagate cancellation through queues, streams and tools

**Priority:** P1  
**Evidence:** C + H  
**Source locations:** [AI queue producer/consumer][src-ai]; [agent abort checks][src-agent]; [runtime task lifecycle][src-runtime]; [gateway SSE][src-gateway].

**Problem.** Aborting is largely cooperative at event boundaries. `_enqueue_stream()` does not clean up its queued/running work when its consumer exits; the worker can continue draining a provider stream into an abandoned queue. Runtime `_process()` enters its cleanup block only after acquiring the semaphore, so cancelling a task while it waits can skip run/abort-event/sentinel cleanup. SSE disconnect handling does not explicitly choose or implement cancellation versus supervised detachment. Tool batches also lack a clear cancellation check between all side effects.

**Required work.** Give each admitted run/request an owned cancellation handle and terminal state. Remove cancelled queued work, close active generators/transports and stop producing into orphaned queues. Put cleanup around semaphore acquisition as well as execution. Define whether a client disconnect cancels or detaches; detached work needs an owner, durable result and bounded sink. Check cancellation before each tool. A cancelled thread-pool future is not proof that its synchronous side effect stopped; represent uncertain completion honestly.

**Acceptance tests.** Abort while waiting for a runtime slot, waiting for an AI slot, reading a silent stream, running a tool and between two tools. Disconnect an SSE consumer. Assert eventual terminal status, no stale `_runs`/abort entries, no abandoned producer, released permits and the documented sentinel behaviour. After cancellation, a new request can make progress.

<a id="asy-03"></a>

### ASY-03: Bound admission, queues, output and total request duration

**Priority:** P1  
**Evidence:** C + H  
**Source locations:** [Runtime task admission][src-runtime]; [AI queue and client timeout][src-ai]; [SSE queues][src-gateway].

**Problem.** The runtime deliberately accepts work without an admission cap and relies on a semaphore only for concurrent execution. AI and SSE queues are unbounded. The chat timeout uses no total deadline and only a socket-read timeout, so a stream that continually sends small amounts can occupy a slot indefinitely. Concurrency limits are not queue-length, memory, cost or wall-clock limits.

**Required work.** Add configurable global/per-user admission limits, queue bounds, maximum input/output bytes, queue wait time and total execution deadline. Define overload responses before accepting side effects; use backpressure or a controlled overflow policy. Budget child work and background work as well as foreground turns. Expose queue depth, rejection count and deadline events without logging secrets. Ensure strict priority scheduling does not indefinitely starve maintenance work.

**Acceptance tests.** Saturate worker slots and submit more work than the configured queue limit. Memory/queue length stay bounded and overload is explicit. Test slow consumers, infinite trickle streams, huge token outputs, repeated fork creation and priority starvation. Limits are effective configuration fields, not ignored YAML keys.

<a id="asy-04"></a>

### ASY-04: Make startup, module lifecycle and shutdown exception-safe

**Priority:** P1  
**Evidence:** C + H  
**Source locations:** [Application startup/shutdown][src-main]; [runtime shutdown][src-runtime]; [module registration/lifecycle wiring][src-module-registry]; [AI worker globals][src-ai].

**Problem.** `main()` does not wrap the complete runtime lifetime in a cleanup `finally`; the no-bridge path returns after runtime startup, and signal-driven cancellation can bypass the normal `gw.shutdown()` tail. Runtime shutdown explicitly closes its main conversation DB but does not visibly own all per-cycle DBs, user/cache stores, background module tasks or AI workers. Module startup/wiring exceptions are logged and may leave partially initialised modules registered; several declared lifecycle hook types are not wired. Verify each owner's cleanup rather than assuming every resource leaks.

**Required work.** Introduce explicit ownership and idempotent async close paths, preferably using structured task groups/exit stacks where appropriate. Run shutdown hooks for successfully started modules in reverse order; roll back failed startup. Do not publish tools from a failed critical module. Await cancellation and close DBs, HTTP clients, browser/MCP resources and queue workers. Decide which unsupported decorator/lifecycle features should be implemented now versus rejected clearly; never silently promise them.

**Acceptance tests.** Test no enabled bridge, partial startup failure, failed critical hook, normal shutdown, SIGTERM/cancellation, repeated shutdown and two runtime lifetimes in separate event loops. Assert no pending owned tasks, stale worker globals, locked databases or open sessions. Tool timeouts/lifecycle hooks either work or produce explicit unsupported-feature errors.

<a id="shl-01"></a>

### SHL-01: Remove blocking subprocess execution from the sandbox HTTP loop

**Status:** Done — sandbox commands now use async subprocesses while keeping the HTTP loop responsive.

**Priority:** P1  
**Evidence:** C  
**Source locations:** [Sandbox `handle_exec()`][src-sandbox]; [Python asyncio subprocess reference][ref-python-subprocess].

**Problem.** The async sandbox HTTP handler calls blocking `subprocess.run()` directly. A long command prevents that event loop from handling another execution request or the health endpoint until it finishes. This is separate from the main tool handler's thread-pool dispatch, which does not change how the sandbox service itself runs.

**Required work.** Use async subprocess APIs with concurrently drained stdout/stderr and controlled concurrency, or a supervised worker architecture that leaves HTTP responsive. Keep shell parsing/authorisation at the established central boundary. Do not accidentally change shell semantics by naively splitting a bash command into words. Implement SHL-02 and SHL-03 with this change rather than adding a thread that remains unmanageable after cancellation.

**Acceptance tests.** While a harmless test command waits, health responds within a reasonable test timeout and a second request follows the configured concurrency policy. Test command completion, both output streams, a non-zero exit and cancellation. Measure event-loop responsiveness rather than asserting only that the final output is correct.

<a id="shl-02"></a>

### SHL-02: Carry timeout budgets through the sandbox protocol

**Status:** Done — validated request timeouts and request IDs now cross the client/server boundary with a transport grace period.

**Priority:** P1  
**Evidence:** C  
**Source locations:** [Shell timeout settings/dispatch][src-shell]; [sandbox fixed timeout][src-sandbox].

**Problem.** The shell tool computes a per-call timeout, but `_run_sandbox()` sends only the command. The server uses its own `SANDBOX_TIMEOUT`, defaulting to 60 seconds. The shell module defaults to 120 seconds and advertises a maximum of 1200 seconds. Client/server behaviour therefore disagrees with the tool contract. A client-side timeout can also expire without a corresponding server-side cancellation. The local default timeout is not clamped through the same explicit-argument path.

**Required work.** Define a versioned request carrying a validated command deadline/timeout and request ID. Enforce an independent server maximum; clamp every default and explicit timeout consistently. Derive transport grace periods deliberately from the execution deadline. Add cancellation/status semantics or an explicit detached-job contract. Do not trust an arbitrary caller-provided timeout simply because the network is internal.

**Acceptance tests.** Test omitted, short, maximum, over-maximum, zero, negative and invalid timeout values. A permitted command longer than 60 seconds must follow its configured policy, not the old hard-coded server default. Use accelerated fake clocks/processes where possible. Transport timeout never silently leaves unowned work running.

<a id="shl-03"></a>

### SHL-03: Bound subprocess output and supervise whole process trees

**Status:** Done — sandbox and local shell output is capped, truncation is reported, and timed-out process groups are terminated and reaped.

**Priority:** P1  
**Evidence:** H with source-backed gaps  
**Source locations:** [Sandbox subprocess capture][src-sandbox]; [local shell subprocess capture][src-shell]; [container limits][src-compose].

**Problem.** Both shell paths capture complete stdout/stderr in memory. Their timeout handling does not show explicit process-group supervision. Stopping the immediate shell is not a demonstrated guarantee that descendants or background work stop, especially for callers allowed untrusted execution. Container CPU/memory limits are useful but are not a complete per-command output/process budget.

**Required work.** Stream output with explicit byte caps and truncation metadata or controlled spill files. Isolate and terminate a process group/job on timeout/cancellation, then reap it. Define whether deliberate background jobs are unsupported or managed. Bound concurrent commands and process counts where supported. Redact sensitive command arguments in operational logs. Keep sandbox network isolation and least-privilege startup intact.

**Acceptance tests.** Use bounded synthetic high-output and child-process fixtures in an isolated test container. Verify memory remains bounded, both pipes are drained without deadlock, descendants are terminated/reaped and truncated output is labelled. No test should start a fork bomb or attack a real endpoint. Container restrictions remain in force after the refactor.

<a id="fil-01"></a>

### FIL-01: Make edits atomic and conflict detection reliable

**Priority:** P1  
**Evidence:** C + H  
**Source locations:** [Filesystem read tracking, writes and edits][src-filesystem]; [shared workspace][src-compose].

**Problem.** Staleness detection compares only whether the current floating-point mtime is greater than the recorded mtime. Equal or backdated timestamps can hide modifications. The timestamp is recorded after reading, creating another mismatch window. Writes/edits can truncate in place and the read/check/write sequence is not atomic against another tool/process. A crash or competing writer can therefore destroy data despite the apparent read-before-write guard.

**Required work.** Define an optimistic version token from the actual opened content/metadata and revalidate it immediately before a controlled write. Use atomic replacement for replacement-style operations, preserving appropriate permissions and durability requirements; coordinate locks/version checks so concurrent writers cannot both pass. Treat append separately where its semantics matter. Reject unexpected mode values. Do not claim mtime alone guarantees concurrency safety or that an advisory lock protects against uncooperative external processes.

**Acceptance tests.** Test two writers starting from the same version, equal/backdated mtime, replacement during read, a crash before replacement and a permission-denied write. One conflicting edit fails clearly instead of overwriting another. The original file remains intact on failed replacement, and safe-path constraints from SEC-05 continue to hold.

<a id="fil-02"></a>

### FIL-02: Fix view line numbers, exact editing semantics and file-handle ownership

**Priority:** P2  
**Evidence:** C + R for line numbering  
**Source locations:** [Filesystem `view`, `write_file`, `edit_file`][src-filesystem].

**Problem.** After slicing a requested line range, text and extracted-document views enumerate from 1 instead of the requested original line. `write_file()` treats unrecognised modes as append for existing files, and `edit_file()` strips trailing whitespace from replacement text without an explicit caller choice. Empty search strings also need a deliberate policy. Text view uses `os.fdopen(...).read()` without an explicit context manager and attempts a separate close on a decoding error, making descriptor ownership/error handling fragile.

**Required work.** Preserve original line numbers and validate range boundaries, including the documented EOF sentinel. Make overwrite/append/prepend a strict enum. Preserve exact replacement bytes/text unless an explicitly requested normalisation mode says otherwise; do not silently strip meaningful whitespace. Reject an empty old string unless an insertion operation is explicitly defined. Use one clearly owned context-managed file descriptor and correct binary-file diagnostics.

**Acceptance tests.** Viewing lines 100–120 labels the first line 100 for text and extracted documents. Test invalid/reversed ranges and EOF. Invalid write mode causes no mutation. Replacement preserves meaningful trailing spaces, newlines and non-ASCII content. Repeated invalid-UTF-8 reads do not leak or double-close descriptors.

<a id="fil-03"></a>

### FIL-03: Bound file/search work and make grep backends agree

**Priority:** P2  
**Evidence:** C + H  
**Source locations:** [Filesystem image/document/text reads, grep and glob][src-filesystem].

**Problem.** Several reads load the entire file/image/document, and result limits often apply only after work/output is already collected. The configured view page size is not used to enforce a default bounded text return in the inspected `view()` path. `_run_rg()` returns stdout without interpreting its failure status/stderr, so invalid patterns and other failures can become "No matches". The Python fallback walks directories but does not handle a file search root like ripgrep does, and its file-type filtering differs.

**Required work.** Add explicit input byte, extraction, regex execution, traversal and output budgets. Stream or page where possible, with accurate continuation metadata. Distinguish no matches from a failed search. Align supported file-root/filter/limit semantics between backends or reject unsupported combinations clearly. Apply containment during traversal, not afterwards. Do not fix bounded output by silently reading arbitrarily large files into memory first.

**Acceptance tests.** Test a file search root, directory root, invalid regex, missing executable, inaccessible file, large file/image/document, many matches and a pathological regex in a safely bounded worker. Both backends return equivalent documented outcomes. Limits bound work/memory as well as final text, and truncation/continuation is explicit.

<a id="api-01"></a>

### API-01: Read complete bounded request bodies and validate their structure

**Priority:** P1  
**Evidence:** C + R  
**Source locations:** [Gateway workspace PUT and request parsing][src-gateway]; [sandbox request parsing][src-sandbox]; [aiohttp StreamReader contract][ref-aiohttp-streams].

**Problem.** Workspace PUT calls `request.content.read(max+1)` once, then immediately parses JSON. `read(n)` can return fewer than n bytes before EOF, so a valid request delivered in fragments can be parsed as incomplete JSON. This was reproduced with aiohttp StreamReader. Multiple routes also assume parsed JSON is a mapping and that fields are strings/lists of the right shape. Sandbox `body.get(...).strip()` has the same structural assumption outside its execution exception block.

**Required work.** Use a complete-body API with the intended size limit or an incremental bounded read loop that continues to EOF. Enforce actual received size independently of Content-Length and return 413 on overflow. Add explicit per-route schemas, strict base64 decoding and decoded attachment limits. Validate JSON-object shape and string/array/member types before access. Return stable 400/413 responses rather than incidental 500s, and do not include sensitive input in error text.

**Acceptance tests.** Send valid JSON split at every byte boundary, chunked transfer, missing/misleading Content-Length, boundary/over-limit bodies, arrays/null/scalars and malformed attachments. Valid fragmented input succeeds; invalid input has no write/permission/action side effect. Test Unicode, invalid base64 and both main and sandbox APIs.

<a id="api-02"></a>

### API-02: Specify lane identity, disconnect and resume semantics

**Priority:** P2  
**Evidence:** V + H  
**Source locations:** [Gateway lane/SSE handlers][src-gateway]; [runtime session-key semantics][src-runtime].

**Problem.** Gateway messages call `runtime.push()` without a stable session key. Runtime documents this as a one-off session keyed by the caller's tail, whereas other bridges can pass a stable cursor key for fork coordination. Several gateway comments still describe an older fan-out registry. The intended API contract for concurrent messages, abort IDs, reconnects and lost final frames needs verification; do not assume a changing node ID and a persistent lane ID are interchangeable.

**Required work.** Define a stable lane/run/cursor contract and implement only the semantics the API promises. Return an actionable run ID early enough for cancellation; make cursor advancement durable or recoverable after a dropped terminal frame. Decide how concurrent messages on one lane fork/settle and how stale cursors are handled. Integrate the disconnect policy from ASY-02 and, if scoped credentials exist, bind lanes to authorised identities. Remove stale fan-out documentation.

**Acceptance tests.** Test two concurrent messages, abort before first model output, disconnect before/after side effects, reconnect without the last done frame, stale cursor and process restart. No duplicate side effects or silent loss of completed history. Document intentionally unsupported resumption/concurrency cases rather than simulating success.

<a id="run-01"></a>

### RUN-01: Compute state from the actual attach point and isolate reset generations

**Priority:** P1  
**Evidence:** C + H  
**Source locations:** [Runtime `push`, `_compute_state_delta`, `seed_session`, `finish_run`][src-runtime].

**Problem.** `push()` computes its state delta from `msg.tail_node_id` before taking the session lock, then may attach the new node to a different `_settled` tail. A field omitted as unchanged against the old cursor can therefore inherit the wrong value from the actual parent. `seed_session()` also changes the attach point without protecting it from an older run finishing later and overwriting the reset. Session-lock/settled maps need an ownership/retention policy for one-off sessions.

**Required work.** Resolve the attach point and compute inherited/delta state consistently within the same serialised transition. Associate resets with a session generation/version so old completions cannot resurrect pre-reset state. Specify last-finisher semantics within a generation rather than removing the current concurrency design wholesale. Preserve caller authority independently of message metadata. Reclaim inactive session bookkeeping without racing active users.

**Acceptance tests.** Use two callers with different state at stale and settled cursors; verify the new node's state matches its actual parent plus incoming values. Start a run, reset, start a new run, then finish the old one: the reset generation remains authoritative. Test concurrent passive messages, forks, failure and session cleanup.

<a id="db-01"></a>

### DB-01: Make state, flags and checkpoints safe under concurrent writers

**Priority:** P1  
**Evidence:** C + H  
**Source locations:** [Database read-modify-write helpers][src-db]; [thread-pool tool execution][src-handler]; [runtime/per-cycle DB ownership][src-runtime].

**Problem.** `set_state()`, flag updates and checkpoint writing perform separate read/merge/write operations without an encompassing concurrency guarantee. They preserve earlier sequential writes but can overwrite changes made between a read and update. `check_same_thread=False` permits cross-thread access; it is not a logical transaction or ownership policy. Multiple runtime/cycle connections make a lock around only one object insufficient.

**Required work.** Choose a clear database access model: serialised writer, correctly scoped transactions, or atomic SQL updates with a compatible schema. Coordinate checkpoint replacement with concurrent deltas. Avoid moving synchronous SQLite work to arbitrary threads without addressing connection ownership. Keep foreign keys and WAL semantics intact. Add bounded busy handling and explicit close ownership. Document whether multiple processes sharing an instance are supported or rejected.

**Acceptance tests.** Force two connections/writers to update distinct state keys and flags with barriers between read and write. Neither update is lost. Race checkpoint creation with state writes. Test lock contention, cancellation, reopen and unsupported second-process startup. No partial tool-exchange/state corruption is accepted as a successful write.

<a id="db-02"></a>

### DB-02: Make migrations and corrupt-state handling explicit

**Priority:** P2  
**Evidence:** C + H  
**Source locations:** [Database schema/migration/state loaders][src-db].

**Problem.** Schema migration catches every SQLite OperationalError and treats it as "column already exists", potentially hiding unrelated failures. JSON state/flags are inconsistently validated: `set_state()` checks for a mapping, but `load_session_state()` assumes a decoded object and calls `.items()`. Invalid JSON can also be replaced with an empty state, concealing corruption. Root initialisation and ancestor ordering/termination should be included in migration/integrity tests.

**Required work.** Track schema versions and apply transactional migrations with explicit preconditions. Suppress only the expected already-applied condition; surface other failures with a recoverable diagnostic. Validate stored JSON shapes and define corruption policy without silently discarding permissions or conversation state. Add instance backup/restore and migration fixtures. Check missing roots, broken parents and cycles defensively where corrupted/imported data can reach the database.

**Acceptance tests.** Upgrade supported historical fixtures, reopen idempotently, fail a migration mid-step and verify rollback/recovery. Test list/null/scalar state JSON, malformed flags, missing roots, corrupt parent chains and permission denied/read-only storage. Restore from backup and compare conversation branches and state.

<a id="cfg-01"></a>

### CFG-01: Resolve shared API-key variables without destructive per-model reads

**Status:** Done. Shared API-key environment variables are resolved for all configured models before being scrubbed and cached.

**Priority:** P1  
**Evidence:** C + R  
**Source locations:** [ModelConfig.api_key][src-config]; [eager per-cycle model construction][src-agent]; [main startup][src-main].

**Problem.** `ModelConfig.api_key` removes its environment variable on the first access and caches the value only on that ModelConfig instance. A second model using the same variable then sees no key. This is a common configuration for primary/fallback/embedding models sharing one provider account; eager construction of multiple LLM clients makes it particularly consequential. The destructive lookup pattern was reproduced using dummy credentials only.

**Required work.** Resolve each referenced secret once per runtime/configuration load and assign it to all intended model configurations before removing it from inherited environment state, if environment scrubbing remains desired. Use an explicit resolver with safe reload/lifetime semantics; avoid an uncontrolled process-global cache that mixes credentials across runtimes. Never log key contents or restore real credentials to a shell-visible environment merely to hide the bug.

**Acceptance tests.** Two chat models and an embedding model share one variable successfully. Distinct variables remain distinct. Missing required keys fail clearly, N/A behaves as documented, repeated access is stable and reload behaviour is explicit. Tests use dummy variables and restore the test environment afterwards.

<a id="cfg-02"></a>

### CFG-02: Make every supported configuration field reach its consumer

**Status:** Done. Supported model, embedding, cache, worker, retry, and fallback settings are loaded, validated, and forwarded to their consumers.

**Priority:** P1  
**Evidence:** C  
**Source locations:** [Configuration model/parsers/loader][src-config]; [AgentCycle._build_llm and fallback loop][src-agent]; [runtime worker setting][src-runtime]; [main cache configuration][src-main].

**Problem.** The inspected loader does not pass model `timeout` into ModelConfig; `_build_llm()` does not forward `budget_tokens`, `reasoning_effort` or `cache_prompts` to LLM. `embed_cache_size` is declared and consumed but not populated from YAML. `max_workers` and `max_empty_retries` are read with getattr defaults without corresponding loaded Config fields in the inspected path. `fallback_on` is parsed but the agent's attempt loop does not apply it. Several numeric bounds are also not validated at load time.

**Required work.** Create a configuration-contract table from YAML through parsed object to consumer. Wire supported keys fully or reject/deprecate them explicitly; do not leave plausible-looking settings inert in `extra`. Apply fallback policy to structured error categories. Validate worker/round/retry counts, budgets, positive finite multipliers, search limits and timeout ceilings. Keep genuinely module-specific keys extensible. Correct default/documentation discrepancies, including minimal tool visibility. Preserve the intentionally supported legacy top-level context fallback until a documented migration removes it.

**Acceptance tests.** Round-trip each supported field through `load()` to a fake consumer and assert the actual request/runtime value. Test non-default timeouts, reasoning/cache parameters, embedding cache size, workers, empty retries and fallback predicates. Zero/negative/NaN/infinite or wrong-shaped values fail with a field-specific message before any service starts.

<a id="rel-01"></a>

### REL-01: Run enforceable checks on the development branch

**Priority:** P2  
**Evidence:** C + H  
**Source locations:** [CI workflow][src-ci]; [test directory][src-tests].

**Problem.** CI triggers cover pushes to main and pull requests targeting main, not ordinary pushes to skygazer. The lint job has job-level `continue-on-error: true`, so its syntax/undefined-name check is non-blocking too. A workflow comment claiming pytest blocks merging is not a substitute for actual branch protection/rulesets. This review did not verify repository-admin merge enforcement.

**Required work.** Run correctness checks for the actual development/release branches and relevant pull requests. Separate mandatory syntax/undefined-name/static correctness checks from advisory style rules. Add targeted regressions from this handover, then the existing suite and container/packaging smoke tests. Configure required status checks/rulesets through an authorised maintainer; do not claim a YAML edit alone enforces merging. Keep permissions minimal and avoid running untrusted pull-request code with secrets.

**Acceptance tests.** A skygazer change triggers the intended checks. A deliberate syntax/undefined-name failure and a failing regression produce failed required checks. Confirm the checks' names match repository rules. Record checks that need maintainer/admin action separately from code commits.

<a id="rel-02"></a>

### REL-02: Verify distributable package assets and make releases reproducible

**Priority:** P2  
**Evidence:** V + H  
**Source locations:** [Project metadata/dependencies][src-pyproject]; [policy resources][src-shell-perms]; [deployment configuration][src-compose].

**Problem.** Editable source installs can hide missing package data. Shell policy YAML and other runtime resources must remain available from a built wheel, but the reviewed project metadata does not itself demonstrate an explicit complete package-data contract. Most dependencies are unconstrained or only broadly constrained, so two fresh installs can resolve differently. No wheel build, clean installation or dependency vulnerability scan was run here; these are verification gates, not confirmed packaging breakage.

**Required work.** Build the wheel/sdist and inspect their contents in a clean environment outside the repository. Include required YAML/JSON/templates explicitly through a maintainable resource strategy. Add a documented reproducibility mechanism for tested application/container deployments while preserving reasonable library dependency ranges if appropriate. Record resolved versions, Python/container compatibility and licence notices. Verify known vulnerabilities with a current scanner rather than inventing findings from package names.

**Acceptance tests.** Install the wheel into a fresh supported Python environment with no source checkout or working-directory dependency. Load policy/module resources and run CLI/agent smoke tests. Rebuild the tested deployment from the recorded dependency set. Missing assets or incompatible runtime dependencies fail release checks.

<a id="rel-03"></a>

### REL-03: Reconcile operator documentation with executable behaviour

**Priority:** P2  
**Evidence:** C  
**Source locations:** [README][src-readme]; [codebase map][src-codebase]; [configuration defaults][src-config]; [main warning][src-main]; [sandbox entrypoint/server][src-entrypoint]; [gateway API][src-gateway].

**Problem.** Documentation is out of sync in several places: the README/default descriptions say minimal tool visibility defaults true while the parsed/default value is false; main logs that an empty gateway key is unauthenticated although middleware rejects protected requests; sandbox comments disagree about root/non-root startup; codebase/gateway comments refer to older paths and fan-out mechanisms. The README's older subagent/tool names and bridge claims also need checking against the installed registry, rather than automatic renaming based on prose.

**Required work.** Generate or test key configuration/API/tool documentation from runtime contracts where practical. Correct authentication and sandbox warnings first because they affect operator decisions. Document current tool/fork names, supported bridges, experimental lifecycle features, data migration and backup requirements. State that model instructions and token sanitisation are not a full prompt-injection boundary; keep the limitations of network-read permissions explicit. Retain accurate beta warnings without using them to excuse known silent failures.

**Acceptance tests.** Follow installation and configuration examples in a clean environment. Verify advertised commands, tools, endpoints and defaults against the running registry/config parser. Grep for retired names and classify each as historical or erroneous. Security claims are backed by tests or clearly described limitations.

<a id="rel-04"></a>

### REL-04: Demonstrate context efficiency with reproducible evaluations

**Priority:** P2  
**Evidence:** H; no performance claim established  
**Source locations:** [Context/token machinery][src-context]; [discovery implementation][src-handler]; [project positioning][src-readme].

**Problem.** Token reduction alone does not establish useful context efficiency. The reviewed evidence does not include a reproduced benchmark showing task success, recall, tool reliability or latency under the advertised small windows. This is an evidence gap, not proof that the architecture is ineffective.

**Required work.** Create a fixed evaluation set for retrieval, long conversations, changed facts, multi-step tool work, interruption/resume and multi-user attribution. Compare a simple history-window baseline against individual optimisations and the full system at documented budgets, including 16k and 32k where supported. Record model/version, prompts, tokeniser, sampling, hardware/provider, dependency set and commit. Report cost/latency together with task success and failure modes, including unsuccessful runs.

**Acceptance tests.** Anyone with the documented environment can reproduce the evaluation and inspect raw results. Include ablations for deduplication, ageing, memory injection and tool discovery. Measure p50/p95 latency, tokens, retrieval/task success and invalid tool payloads. A claimed improvement must not trade away correctness without explicitly reporting that trade-off.


## 4. Implementation sequence and dependencies

**Stage 0: baseline and working environment.** Record the checkout/dirty state, supported interpreter, dependency versions, existing test results and any environment failures. Create isolated fixtures and a dedicated working branch. Read the permission/trust contracts before changing gates.

| Stage | Work | Exit condition |
|---|---|---|
| 1. Authority and input boundaries | SEC-01 through SEC-05, CFG-01, API-01 | Strict authority inputs, no known child-path escape, coherent backend authorisation, working shared-secret resolution, bounded valid HTTP parsing. Gateway/race-resistance design decisions documented. |
| 2. Contracts and configuration | TOL-01, TOL-02, CFG-02 | Correct schemas, one validated invocation object, typed truthful outcomes and effective supported configuration. |
| 3. Context and agent correctness | CTX-01 through CTX-04, AGT-01 through AGT-05 | Final wire payloads preserve exchange/current-task invariants; cycle limits and corrections terminate clearly; state/model/stream behaviour is coherent. |
| 4. Scheduling and execution | ASY-01 through ASY-04, SHL-01 through SHL-03, API-02 | Verified retry policy, cancellable owned work, bounded queues/deadlines, responsive supervised shell service and documented resume semantics. |
| 5. Persistence and file integrity | FIL-01 through FIL-03, RUN-01, DB-01, DB-02 | Conflict-safe edits, correct views/searches, generation-safe state transitions, transactional updates and recoverable migrations. |
| 6. Discovery and performance | TOL-03 through TOL-05, CTX-05 | Correct backend/model selection, cache isolation, deterministic visibility and measured assembly scaling. |
| 7. Release evidence | REL-01 through REL-04 and the validation gates below | Required CI, distributable resources, consistent operator docs and reproducible correctness/efficiency evidence. |

Some work crosses stages. For example, SHL-01/02/03 should share one execution design, while CTX-01/03 and AGT-04/05 should share one final-payload/attempt contract. Split commits by coherent behaviour, not by mechanically implementing every ID in isolation.

Do not change dependency/runtime versions and the concurrency model in the same initial patch unless necessary to reproduce a defect. That makes regressions harder to attribute. Add narrowly justified dependencies only after checking whether existing libraries/stdlib meet the requirement.

## 5. System invariants the tests must enforce

### 5.1 Authority and identity

- Every side-effecting operation uses a validated, server-resolved caller and the exact validated arguments checked by the central capability guard.
- Effective execution location is included in the permission decision. A missing sandbox never quietly becomes host/main-container execution for a sandbox-only caller.
- Message attribution is preserved independently of permission identity. User-controlled content cannot select its caller privileges, and advisory fork output is not silently promoted into a real user's instruction.
- Filesystem access remains within the authorised read/write roots at the time of access, including child traversal and supported concurrent mutations.
- Revocation/override semantics and master-key authority are documented and tested. A name, label, tool schema or prompt is not itself an authorisation boundary.

### 5.2 Conversation and tool protocol

- Every retained tool result matches a retained preceding call; call IDs are unique in the relevant exchange and required result batches are complete before the next unrelated message.
- Successful assembly retains the protected current task. Irreducible overflow returns an explicit error rather than a misleading successful answer.
- The provider receives the same final payload whose size/protocol metadata was validated, including image adaptation and post-assembly hooks.
- Durable history records real side effects and real outcomes. Context compaction changes the model's working view, not the historical truth.
- Termination has a reason and happens once. Tool limits, correction exhaustion, cancellation and provider failure cannot masquerade as an ordinary empty success.

### 5.3 Ownership, durability and bounded work

- Each run, subprocess, transport, task and queue has an owner and a terminal cleanup path, including cancellation while queued and partial startup.
- Concurrency, admission, queue length, output size and duration are separately bounded; one bound is not a substitute for the others.
- A reset creates a new logical generation that an old finishing run cannot overwrite.
- Concurrent state/file updates do not silently lose one another; unsupported concurrency is rejected explicitly.
- A released package is independently installable and contains the resources its runtime needs.

### 5.4 Example final-payload assertion

This is **new illustrative test code**, not a production patch or an assertion already present in TinyCTX. Adapt it to the exact provider contract and call it after provider-specific payload conversion. It deliberately validates completed tool batches, not a partially written in-progress database stream.

```python
from collections.abc import Sequence
from typing import Any


def assert_complete_tool_batches(messages: Sequence[dict[str, Any]]) -> None:
    pending: set[str] = set()
    seen: set[str] = set()

    for message in messages:
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            assert isinstance(call_id, str) and call_id in pending, (
                "Orphaned or duplicate tool result", message
            )
            pending.remove(call_id)
            continue

        assert not pending, ("Message interrupts an unfinished tool batch", pending)
        calls = message.get("tool_calls") or []
        assert not calls or role == "assistant", "Only assistant messages declare calls"
        for call in calls:
            call_id = call.get("id")
            assert isinstance(call_id, str) and call_id, "Missing call ID"
            assert call_id not in seen, ("Duplicate call ID", call_id)
            seen.add(call_id)
            pending.add(call_id)

    assert not pending, ("Unresolved calls in completed request payload", pending)
```

A validator is a backstop, not a substitute for correct transformations. Do not simply delete offending messages until this helper passes: that can lose the current task or falsify action history. Define a safe repair/error path for partial historic batches following an interruption.

## 6. Regression and integration test plan

The filenames below are suggested additions or extensions, not a claim that those files already exist. Reuse existing fixtures where they fit. Keep network/model behaviour deterministic through fake providers and local services; never require real keys for the default correctness suite.

| Test family | Minimum scenarios | Relevant work |
|---|---|---|
| Configuration and authority | Native/quoted booleans; shared secret variables; non-default settings; malformed inputs; actual backend selection; revoked grants | SEC-01/03/04, CFG-01/02, TOL-01 |
| Filesystem confinement | Out-of-root symlink; parent glob; allowed read-only root; swapped parent; read/write/image/document/upload/delivery paths | SEC-02/05, FIL-01/03 |
| Context protocol | Empty/text assistants; multiple calls; every trim/dedup/tokenade combination; images; corrective turns; interrupted batch | CTX-01/02/04, AGT-01/02 |
| Budget correctness | Current-task protection; tool-schema/system-only overflow; post-hook growth; output reserve contract; smaller/vision fallback | CTX-02/03, AGT-05 |
| Agent lifecycle | Exact cycle boundary; explicit terminal reason; enablement across turns/restart; partial-stream fallback; hook composition | AGT-01 through AGT-05 |
| Scheduler/cancellation | Cancel at every queue/await boundary; disconnect; slow consumer; producer failure; bounded overload; two event-loop lifetimes | ASY-01 through ASY-04, API-02 |
| Shell integration | Responsive health under load; timeout request/maximum; process tree; both pipes; output cap; non-zero exits; unknown completion | SHL-01 through SHL-03, TOL-02 |
| Tool discovery | Four backend-toggle combinations; different embedding endpoints/templates/dimensions; stable definitions; hidden tools | TOL-03 through TOL-05 |
| File correctness | Original line labels; strict modes/ranges; whitespace preservation; conflicting writers; invalid UTF-8; grep backend parity | FIL-01 through FIL-03 |
| Runtime/database | Stale vs settled cursor; reset during run; simultaneous state/flag writes; checkpoints; corrupt JSON; migration rollback/reopen | RUN-01, DB-01/02 |
| Distribution and docs | Wheel without checkout; required resources; supported Python; deployment config; advertised names/defaults/endpoints | REL-01 through REL-03 |
| Evaluation | Fixed small-window tasks; baseline/ablations; raw results; latency, tokens and quality together | CTX-05, REL-04 |

### Suggested baseline commands

Run these in a disposable development environment on the supported interpreter, using the project's resolved dependency mechanism once established. They are instructions for the implementation environment, **not commands already executed for this handover**. Installation of agent extras can be substantial and must not reuse a production instance or real secrets.

```bash
git status --short
git rev-parse HEAD
python --version

# Set up/activate a supported Python >=3.14 environment before installation.
python -m pip install -e '.[agent]'
python -m pip install pytest pytest-asyncio flake8 build

python -m compileall -q TinyCTX sandbox
python -m flake8 TinyCTX sandbox tests --select=E9,F63,F7,F82 --show-source --statistics
python -m pytest -q
python -m build
```

Then inspect the built archive, install it outside the source checkout into a second clean environment, and execute the relevant CLI/resource/agent smoke tests. Run Docker integration tests with dedicated temporary instance/configuration paths and no production mounts or credentials. Capture the effective Compose configuration; do not assume an environment variable or resource limit is honoured merely because it appears in YAML.

Record command, commit, runtime/dependency versions, exit status, pass/fail/skip counts and failure logs. Separate pre-existing failures, environment failures and regressions introduced by the patch. Do not convert failures into blanket skips. Add concurrency tests with deterministic barriers rather than relying only on arbitrary sleeps.

### Additional verification gates for incompletely audited areas

These are intentionally **V/H gates**, not invented bug reports. The module directories are present in the reviewed repository, but their complete implementation was not inspected during this handover. The coding LLM must read the relevant code before claiming a defect or proposing a patch.

| Gate | Scope to inspect | Required verification |
|---|---|---|
| V-01 | `TinyCTX/modules/memory`, `TinyCTX/modules/rag` | Extraction/search failure handling, provenance, changed/contradictory facts, deletion/reindexing, idempotent consolidation, stale embeddings, dimension/model changes, allowed file roots, restart safety and prompt/memory injection. Do not assume a knowledge graph preserves all important facts just because history is stored. |
| V-02 | `TinyCTX/modules/cron` and scheduling stores | Creator identity and current grants at fire time, revocation, rename/deletion, channel ownership, duplicate delivery, crash/restart, one-shot semantics, missed runs and timezone/DST behaviour. Preserve the README's stated recheck/ownership protections where implemented; do not report them missing without reading the code. |
| V-03 | `TinyCTX/modules/mcp` | Native schema preservation, explicit tool permission declarations, server process/environment isolation, cancellation/timeouts, duplicate tool names, malformed responses and shutdown. Treat configured server commands as trusted operator code, not arbitrary end-user input. |
| V-04 | `TinyCTX/modules/web`, `comfyui`, `present` and attachment handling | Effective network/URL policy, redirects, private/link-local targets, non-HTTP schemes, downloads/screenshots, output size, path confinement and actual remote side effects. Prove the intended boundary; do not claim network-read alone prevents exfiltration. |
| V-05 | CLI, Discord and Telegram bridge implementations, plus any genuinely installed additional bridge | Allowlist and DM behaviour, stable identity mapping, attachment limits, escaping/mentions, outbound rendering, cursor storage and reconnect behaviour. Distinguish bridges advertised in docs from ones actually implemented in this branch. |
| V-06 | `TinyCTX/users`, sysops and concurrency modules | Permission changes during a long run, stale User objects, stable identity vs rename, cross-session/channel access, fork/nudge authority and shared-memory disclosure policy. Prove current grants are consulted where promised without relying on prompt attribution. |
| V-07 | Module/decorator/hook contracts and custom-module loading | Supported hook phases, timeout metadata, duplicate registrations, startup rollback, thread safety and required permissions. Unsupported features must be rejected or explicitly experimental, not silently accepted as working. |
| V-08 | Actual container/network deployment | Non-root steady state, effective capabilities, read-only mounts, workspace/data separation, private IPv4/IPv6/link-local egress, DNS behaviour and internal service reachability. Use an isolated test network. Source configuration alone is not proof that a deployment enforces the boundary. |

## 7. Isolated checks actually run for this handover

These checks used small source-pattern stand-ins and temporary files. They were not imports of the complete TinyCTX package, and they do not establish compatibility with the required Python 3.14 runtime. Their purpose is to distinguish concrete control-flow/file/stream behaviour from unaudited speculation.

| Check | Observed result | Related item |
|---|---|---|
| Budget tool-pair removal with synthetic costs | Assistant call removed while its tool result survives when assistant text remains | CTX-01 |
| Oversized current request | Latest user entry removed entirely | CTX-02 |
| Irreducible system overflow | System-only content remains above the configured synthetic limit without a hard error | CTX-02 |
| YAML quoted boolean | `"false"` becomes truthy under `bool(value)` | SEC-01 |
| Shared environment-key lookup | Second model-style object fails after the first consumes the same dummy variable | CFG-01 |
| Python grep traversal/open pattern | A file symlink in a temporary workspace reads a marker outside that workspace | SEC-02 |
| Parent-directory glob pattern | `Path.glob('../*.txt')` enumerates an outside temporary file | SEC-02 |
| aiohttp bounded single read | `read(max+1)` returns an incomplete JSON prefix before remaining bytes arrive | API-01 |
| Dataclass copy without identity fields | Author and parent reset to defaults | CTX-04 |
| Dedup return-None semantics | Empty assistant's suppressed calls survive because None means unchanged | CTX-01 |
| Range slicing plus enumerate-from-one | Original line 3 is labelled line 1 | FIL-02 |

**Not verified by these checks:** end-to-end exploitability, full permission policy, a running Docker sandbox, async-generator retries under the project's actual Tenacity version, cancellation/resource-leak magnitude, complete database race behaviour, wheel contents or model task quality. Those remain subject to the real regression/integration plan.

## 8. Required implementation handback

The coding LLM's final handback should contain the following, in addition to the code:

1. **Issue ledger:** Every ID classified as fixed, already fixed, disproved, design decision required, deferred with maintainer acceptance, or blocked with a concrete reason. Link each fixed ID to the changed functions and regression tests.
2. **Verification evidence:** Actual commands/results, environment and resolved versions; no invented pass counts, security guarantees, benchmark numbers or coverage claims.
3. **Compatibility/migration notes:** Configuration changes, API/event changes, cache invalidation, schema versions, dependency changes, backup/restore procedure and rollback boundaries.
4. **Residual risks:** Any remaining P1 item, incomplete validation gate, deployment-dependent security assumption, untested platform or intentionally unsupported feature.

### Completion checklist

- [ ] All confirmed P1 defects have production-path regression tests and fixes, or have been demonstrably resolved in the current checkout.
- [ ] Each verification-first hypothesis has a recorded reproduction or a justified closure; none was patched solely because it appeared in this document.
- [ ] Gateway authority and local/sandbox execution semantics are explicit and agreed.
- [ ] Final provider payloads preserve current-task and tool-exchange invariants.
- [ ] Executed tools retain truthful durable outcomes and unambiguous terminal status.
- [ ] Cancellation, overload, output limits and resource ownership are tested under failure, not only success.
- [ ] Concurrent file/state operations and migrations have integrity/recovery tests.
- [ ] The supported Python environment passes the recorded correctness suite; skipped/unavailable integration checks are disclosed.
- [ ] A clean built-package installation includes its runtime assets and passes smoke tests.
- [ ] Documentation and enforced CI match the delivered behaviour.
- [ ] Model-efficiency claims are backed by reproducible results or explicitly remain unproven goals.
- [ ] No unrelated working-tree changes, production data, credentials or repository access settings were modified.

## 9. Source references

Repository references are pinned to the reviewed commit. Function names in each issue are the primary locations; line numbers can move in the implementation branch. External library references were consulted on 26 September 2026 and must be checked against the versions actually resolved for the implementation.

- [Agent execution cycle][src-agent]: `TinyCTX/agent.py`.
- [History types, assembly and budgets][src-context]: `TinyCTX/context.py`.
- [Context optimisation hooks and copy helper][src-ctx-tools]: `TinyCTX/modules/ctx_tools/__init__.py`.
- [Tool registration, schemas, discovery and execution][src-handler]: `TinyCTX/tool_handling/handler.py`.
- [Configuration contracts and loader][src-config]: `TinyCTX/config/__main__.py`.
- [LLM/embedding clients, queues and caches][src-ai]: `TinyCTX/ai.py`.
- [Sandbox HTTP execution service][src-sandbox]: `sandbox/__main__.py`.
- [Shell module and backend dispatch][src-shell]: `TinyCTX/modules/shell/__init__.py`.
- [Shell capability classification][src-shell-perms]: `TinyCTX/modules/shell/perms.py`.
- [Filesystem tools and search helpers][src-filesystem]: `TinyCTX/modules/filesystem/__init__.py`.
- [HTTP/SSE gateway][src-gateway]: `TinyCTX/gateway/__main__.py`.
- [Runtime sessions, runs and lifecycle][src-runtime]: `TinyCTX/runtime.py`.
- [Conversation database and migrations][src-db]: `TinyCTX/db.py`.
- [Module loading and hook wiring][src-module-registry]: `TinyCTX/module_registry.py`.
- [Application startup/shutdown][src-main]: `TinyCTX/main.py`.
- [Capability semantics][src-permissions]: `TinyCTX/permissions.py`.
- [Container/network configuration][src-compose]: `compose.yaml`.
- [Sandbox firewall startup][src-entrypoint]: `sandbox/entrypoint.sh`.
- [CI triggers and jobs][src-ci]: `.github/workflows/ci.yml`.
- [Package/dependency metadata][src-pyproject]: `pyproject.toml`.
- [Operator-facing README][src-readme]: `README.md`.
- [Codebase map][src-codebase]: `CODEBASE.md`.
- [Existing context tests inspected][src-test-context]: `tests/test_context.py`.
- [Existing test directory][src-tests]: broader suite present; not run for this handover.
- [Python asyncio subprocess documentation][ref-python-subprocess].
- [aiohttp streaming API documentation][ref-aiohttp-streams].
- [Tenacity documentation][ref-tenacity].

[src-agent]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/agent.py
[src-context]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/context.py
[src-ctx-tools]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/modules/ctx_tools/__init__.py
[src-handler]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/tool_handling/handler.py
[src-config]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/config/__main__.py
[src-ai]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/ai.py
[src-sandbox]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/sandbox/__main__.py
[src-shell]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/modules/shell/__init__.py
[src-shell-perms]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/modules/shell/perms.py
[src-filesystem]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/modules/filesystem/__init__.py
[src-gateway]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/gateway/__main__.py
[src-runtime]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/runtime.py
[src-db]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/db.py
[src-module-registry]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/module_registry.py
[src-main]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/main.py
[src-permissions]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/TinyCTX/permissions.py
[src-compose]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/compose.yaml
[src-entrypoint]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/sandbox/entrypoint.sh
[src-ci]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/.github/workflows/ci.yml
[src-pyproject]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/pyproject.toml
[src-readme]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/README.md
[src-codebase]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/CODEBASE.md
[src-test-context]: https://github.com/itzpingcat/TinyCTX/blob/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/tests/test_context.py
[src-tests]: https://github.com/itzpingcat/TinyCTX/tree/902938bc7bb2d1b310b5bd0e855e65c9c4ed32d0/tests
[ref-python-subprocess]: https://docs.python.org/3.14/library/asyncio-subprocess.html
[ref-aiohttp-streams]: https://docs.aiohttp.org/en/stable/streams.html
[ref-tenacity]: https://tenacity.readthedocs.io/en/latest/
