# Video Runtime

English | [中文](README.zh.md)

The Python Video Runtime owns long-lived video projects independently of an agent Session or Run. It stores immutable project versions, versioned artifacts, typed artifact dependencies, change requests, rebuild plans, durable builds, validation results, timelines, exports, and monotonically sequenced project events.

## Run

Content-review failures reach the Agent with the submitted prompt, input references, provider error and persisted replacement history. One automatic compliant correction per failed-task chain may clarify ambiguity or remove unrequested additions while preserving references, model and provider. Repeated rejected prompts, unlinked generation tasks while moderation remains unresolved, and further repairs after rejection are blocked. Exhausted repairs cancel pending descendants and preserve completed artifacts; Studio displays review, repair, recovery or required user revision. Core story preservation is an Agent instruction, not a semantic equivalence guarantee. A substantive content or reference change requires a revised user request; the automatic repair path never resets its budget on checkpoint redelivery or restart.

Failed Harness turns expose the provider error message and code through the Studio event API. The existing error notice displays API credit exhaustion and other model failures, including when replaying session history.

The `cinematic` Workflow reuses Seedance2's agentic planning with `reference_mode: multi_reference`: continuation clips retain the original identity/scene images and add the previous decoded tail as another reference. It does not lock the first frame. Select Cinematic or mention `$cinematic`; existing Seedance2 projects are not switched automatically.

Seedance continuation planning keeps the preceding tail in `start_image_from_step` and the original identity/scene references in `reference_from_steps`. WaveSpeed's documented I2V request has no ordinary-reference field, so a new request combining a strict start frame and reference images is rejected before submission instead of silently dropping identity constraints. Already-submitted jobs remain pollable. A supported combined-input endpoint or explicit user approval for non-strict multi-reference generation is required; the Runtime does not silently change providers or modes.

Subtitle transcription accepts a `prompt` containing known lyrics or names as recognition context. Responses with at least eight words and more than half of their word timestamps collapsed are rejected before subtitle rendering; the Agent can submit a replacement PlanPatch with audio context. This timing check detects one failure pattern and does not certify transcript accuracy.

The standalone entrypoint enables staged planning and continuous PlanPatch by default; explicit environment values override these defaults. On Windows, `scripts/start-video-runtime.ps1` accepts `-Python`, `-CredentialEnvFiles`, `-Port`, and `-MediaServiceUrl` (default `http://127.0.0.1:18080`). It applies the local topology after credential imports and checks media readiness before starting, preventing imported legacy service addresses from redirecting local builds.

Provider dispatch resolves task input ArtifactVersions into ordered image, video, and audio URI lists for direct and atomic calls. Explicit media parameter order takes precedence; remaining task inputs follow their declared order and are merged without duplicates. Missing referenced media and invalid indexed prompt references fail before submission. Resolved input versions and concrete parameters are recorded on output artifacts. A Seedance model identifier supplied in the legacy `provider` field is separated into the WaveSpeed transport and the exact requested model; conflicting version selectors fail before submission. Remote operation IDs and idempotency keys survive dispatch. Reference images remain references even when a caller supplies an obsolete `i2v` mode; only an explicit `start_image_url`, first-frame alias, or prompt slot labelled as the first frame selects strict image-to-video generation.

Failed-task repair accepts new parameters through PlanPatch replacement mappings. Pending descendants are cloned with rewired dependencies and their old steps are cancelled in the same revision transaction. Failed steps retain their original parameters and a `superseded_by` pointer; whole-build retries skip superseded and cancelled steps. Started descendants cannot be rewired. A failed continuous Build remains failed while a repair checkpoint is inspected and resumes only after a valid replacement patch commits against current plan, spec, and project versions.

The Create stop action cancels both the DeepSeek turn and active Runtime builds. Ordinary worker updates cannot change a stopped, failed, or completed Build or BuildStep to another status, and terminal Builds cannot publish a project version; only the explicit resume and retry repository operations can reopen stopped or failed work. Stopped builds retain drafts, pending steps, and remote operation IDs; upstream jobs already submitted may continue and incur charges. Sending a follow-up requires explicit resume confirmation. Resume continues the latest stopped build against its original project version, reuses completed steps, and reconciles existing remote operations instead of creating a replacement build. Refreshing the page does not resume work.

Install the same Python dependencies as the Runtime image, from `pyproject.toml`:

```sh
python -m pip install -e .
python -m uvicorn app.video_runtime.standalone:app --host 127.0.0.1 --port 8001
```

Set `VIDEO_RUNTIME_DATABASE_URL` for Postgres durability. Without it, the standalone service uses the atomic local JSON repository at `VIDEO_RUNTIME_LOCAL_STATE_PATH`; when unset, the path is the same per-user system data directory used by `@cuti-ai/video-agent-harness`. If the canonical file is absent but a legacy checkout-relative state file exists, startup stops and reports the source and migration destination instead of creating an empty project library. Set `VIDEO_RUNTIME_IN_MEMORY=true` only for isolated tests. `deploy/compose.video.yml` applies every ordered migration in `migrations/video_runtime` before starting the service.

For local storage the standalone entrypoint defaults `PUBLIC_BASE_URL` to `http://127.0.0.1:8001`, matching its documented port and `/files/*` mount. Set it explicitly when the Runtime is exposed on another origin. Provider-bound media is still uploaded through the media-egress adapter, so loopback URLs are never sent to remote generation services.

## Runtime APIs

Failed-task repair rewrites declared task selectors together with scheduling dependencies, including ordered video inputs, audio/subtitle sources, and generated reference selectors. Prompt text, URLs, and artifact-version ids are not rewritten. PlanPatch admission checks selectors against non-superseded tasks and adds their scheduling dependencies before execution; missing or superseded targets are rejected. Repair does not mutate already-persisted historical plans.

The versioned API under `/api/video` creates and inspects projects, returns one aggregate editing workspace, previews structured edits and deterministic dependency impact, submits or cancels builds, selects artifact versions, restores project versions, exports a fixed project version, manages configured plugins, and streams project events. Every modifying call has an idempotency key where replay can cause duplicate work. Build, selection, restore, and export operations compare their base project version before changing authoritative state.

## Plugin execution

With staged and continuous planning enabled, artifact regeneration, structured edits and post-production previews create a continuation goal retaining the selected artifacts and exact requested edits. DeepSeek authors executable PlanPatches in the bound Session; edit previews do not reconstruct a fixed workflow DAG or implicitly pass the edited video as a provider reference. Each terminal task failure reaches the Agent before another generation submission. Completion of an edit requires a newly produced artifact, not merely an existing playable selection. Disabled continuous planning retains the deterministic compatibility path.

With staged planning and `VIDEO_CONTINUOUS_PLAN_PATCH_ENABLED` enabled, one Runtime worker owns each Build. It admits ready tasks up to the configured concurrency limit and reloads the persisted plan on task completion, terminal failure, or PlanPatch submission. A finished task requests Agent planning without waiting for unrelated Provider operations. One outstanding checkpoint per Build coalesces subsequent terminal updates into the next snapshot; delivery uses the bound DeepSeek Session, not a second planner. Failed tasks remain recorded and require an Agent-authored replacement with a new id.

During execution, `POST /api/video/projects/{project_id}/builds/{build_id}/checkpoints/live` opens or reuses an active planning snapshot. Resolve its returned id with exact plan/spec revisions and an idempotency key. A patch can append tasks with dependencies on existing or newly appended tasks, or cancel pending tasks together with their pending dependants. Admission and cancellation compare the stored Step status atomically; running tasks cannot be cancelled through a pending-task patch. Goal completion rejects remaining active work. Shutdown preserves remote-operation ids for reconciliation, and recovery resumes continuous Builds even when Agent planning is outstanding. A single Runtime process must own execution for a repository; distributed worker leasing is not provided.

`video-plugin.yaml` declares provider, workflow, style, validator, and media contributions plus dependencies and permissions. `VIDEO_PLUGIN_PATHS` may replace the bundled `plugins` root with platform-separated directories whose immediate child directories contain manifests. A plugin may extend `BaseVideoPlugin`; its `capability_handlers()` maps declared capability ids to executable handlers. Trusted built-ins can load in-process. An untrusted plugin declares `sandbox_runtime` (image, entrypoint, and timeout); the runtime never imports its module and executes the bundle through `services/sandbox-worker` with the signed grant's limits.

`VIDEO_SKILL_PATHS` may replace the process-wide Skill roots. Without it, one `VideoSkillRuntime` discovers the bundled system, built-in, external, and stage-director Skills. Its shared catalog validates executable contracts and Workflow declarations, freezes helper Skill context on each planned Build step, and registers Workflow contributions through `cuti.skill-workflows`. `GET /api/video/workflows` returns manifest and effective Skill workflows.

Installed Workflow Skills can declare `workflow.planning.mode: agentic` with a non-empty `allowed_capabilities` list to use DeepSeek-authored PlanPatch execution without a name-bound compiler. Both `VIDEO_STAGED_PLANNING_ENABLED` and `VIDEO_CONTINUOUS_PLAN_PATCH_ENABLED` must be enabled. The catalog reports `executionKind: agent_plan_patch`; DeepSeek loads the original Skill instructions and submits structured tasks, never executable compiler code. Named Cuti compilers retain their original metadata checks. Other planning modes require a dedicated compiler; missing execution support is unavailable, not a fallback to another Workflow.

Agent-planned Skills may declare `workflow.parameters.completion_artifact_types`, for example `[script]` for a document-only Workflow; the default is `[video]`. Completion requires a newly created, completed deliverable of a declared type and no active tasks. Source artifacts and intent records cannot satisfy it. Upload a ZIP containing a directory named after the Skill and its `SKILL.md`; installation reuses Cuti's archive validation and reloads the shared catalog and resolver. Capability schemas, DAG validation, project access, signed grants, and execution limits remain mandatory. A Skill allowlist restricts capabilities but cannot grant credentials or network access.

The built-in adapters reuse Cuti's text, image, music, video, TTS, FFmpeg, subtitle, and lipsync operations. Selectable workflows (`mv`, `seedance2`, `short-drama-workflow`, `cuti-product-workflow`, and the product-workflow variants) compile provider-neutral `VideoSpec` values; `cuti.style-presets` applies installed prompt defaults before compilation. Production identity uses the standalone service bearer token, and fails closed when it is not configured. Local self-host uses `local-user`.

Every new `ProjectIntent` and `VideoSpec` persists one language contract with independent UI, user-visible content, spoken, subtitle, and provider-prompt languages. The BFF injects that contract into initial planning, follow-up edits, and automatic checkpoint turns; Runtime-generated user artifacts record the contract and reject clear text-language mismatches. Legacy documents with only `language` receive matching defaults for all content fields.

All registered capabilities execute through `RuntimeCapabilityRegistry` and `CapabilityExecutionGateway`. The gateway verifies a server-signed grant bound to the project, Session, user, plugin, capability, domains, cost ceiling, timeout, concurrency, retries, idempotency key, audit id, and expiry. Executable in-process plugins require a server-owned `VIDEO_CAPABILITY_GRANT_SECRET` of at least 32 bytes.

Scene-reference isolation is checked both at the provider boundary and against rendered pixels. A failed scene-reference check enters the staged build's single semantic-repair pass before any dependent video segment runs. `VIDEO_SCENE_REFERENCE_VISUAL_VALIDATION_ENABLED` explicitly enables or disables the pixel check; when omitted it is enabled whenever `OPENAI_API_KEY` is configured. `VIDEO_SCENE_REFERENCE_VALIDATOR_MODEL` selects the vision-capable validation model.

## Compatibility API

A bound project's `thread_id` is its durable Harness Session id. Follow-ups reject a conflicting thread id and append to that Session, retaining conversation, tool results, and Harness compaction summaries. Each follow-up logs current project-version, latest-build, and checkpoint references; sequential planning reads actual preceding artifacts through Runtime tools and uses their version ids as dependencies. Runtime checkpoints persist execution progress and queue continuations into the same Session; they are not LangGraph checkpoints or a separate agent loop. Both the Harness data directory and Runtime repository must be preserved across restarts.

Studio talks to `app.video_runtime.standalone:app`. The `/chat-v1/service` mount is the DeepSeek BFF and the only planning loop.

If a pasted Create-Space URL names a DeepSeek Session whose Project binding is absent from the restored Runtime store, the BFF creates a fresh Session and returns its new `thread_id`; it never attaches a new Project to an unbound historical Session.

## Tests

Artifact regeneration restores the selected output's saved capability and parameters, applies the requested patch, and rebuilds hard dependants without requiring a complete VideoSpec. Workspace specification reads follow the committed ProjectVersion revision. Continuous PlanPatch revisions include execution recipes; follow-up goals retain selected generated artifacts as reusable inputs. Active builds accept live patches and pending-task cancellation; completed outputs are edited through new builds so failures preserve the previous version.

Checkpoint delivery reconciles consumed Session messages with completed turns. A turn that ends without resolving its checkpoint is retried within the existing delivery budget; retries and checkpoint inspection include current task results. Queued messages, active turns, resolved checkpoints, and cancelled Builds are not treated as missing acknowledgements. The lease remains the recovery mechanism for interrupted delivery.

```sh
python -m unittest discover -s tests/video_runtime -v
```

Additional pytest coverage lives under `tests/` for capabilities, media tools, and the DeepSeek BFF.

Live editing snapshots belong to the requesting Agent turn and are excluded from automatic checkpoint delivery. Empty agentic acknowledgements resolve their checkpoint without advancing plan or specification revisions. Real task completion and failure notifications remain automatic; a subsequent user edit can open a fresh live snapshot.

Repeated edits support chaining media operations within one PlanPatch and creating multiple variants of a source. Matching redundant project Artifact URLs are normalized to structured inputs. Persisted execution history does not participate in creative VideoSpec validation.

Dialogue captions use actual-audio transcription without screenplay prompting. The Cuti HyperFrames template consumes persisted transcript cues and owns placement and timing; source-language captions are the default. Explicit translations use one `translated_texts` entry per segment, preserving source timestamps. Atomic video generation is available in workflow-free PlanPatch plans and can feed concatenation. User-requested edits proceed from preview to apply without a second confirmation; queued work must be followed until the requested deliverable exists.

Code-to-Video is an independent, persisted run mode rather than a normal workflow capability. Studio exposes a dedicated toggle and defaults new projects to the mode only when `WAVESPEED_API_KEY` is empty. Video turns in this mode produce one self-contained `media.hyperframes_render` PlanPatch operation; ordinary chat uses no media operation. The Runtime rejects that capability while the persisted mode is off, and Planning-off cannot be enabled at the same time.
