# Detailed Deployment Strategy

## Status and scope

ProofCast currently runs as a local, immutable container with a deterministic fixture provider and an optional OpenAI adapter. The topology below is a **reference production design**, not infrastructure claimed to be deployed by this repository.

The production goal is to let an agency upload podcast transcripts, receive evidence-backed editorial drafts, and retain a human approval step before any content reaches a CMS or social-publishing system. The design keeps the workflow portable across approved model providers while making tenant isolation, provenance, cost, and failure behavior explicit.

## Diagram

![ProofCast reference production deployment architecture](deployment-architecture.png)

The rendered PNG is committed so the diagram remains visible without Mermaid tooling. An accessible [`deployment-architecture.svg`](deployment-architecture.svg) and editable [`deployment-architecture.mmd`](deployment-architecture.mmd) are also included.

## Narrative description

| Layer | Responsibilities | Example managed implementation |
|---|---|---|
| Edge and identity | TLS, WAF, rate limits, OIDC/OAuth, request authorization | CDN/WAF plus an API gateway and enterprise identity provider |
| Ingestion API | Upload URLs, schema and size checks, idempotent job creation, review APIs | Autoscaling container or serverless API |
| Data plane | Immutable transcripts, job metadata, checkpoints, reports, evidence metadata | Encrypted object storage plus PostgreSQL or a managed key-value store |
| Messaging | Backpressure, at-least-once delivery, retry isolation, dead-letter handling | Managed queue plus a dead-letter queue |
| Orchestration | Stage transitions, checkpoints, retry policy, timeout policy, replay | Durable workflow service or state machine |
| Workers | Normalize, plan, summarize, extract claims, retrieve evidence, validate | Ephemeral autoscaling containers with no public inbound access |
| Provider gateways | Model capability policy, evidence retrieval, attribution, usage reporting | Private service adapters to approved APIs or client-managed endpoints |
| Review and export | Human approval, version selection, downstream export | Review UI and narrowly scoped CMS integration |
| Operations | Secrets, KMS, redacted telemetry, alerts, budgets, audit events | Managed secrets/KMS and OpenTelemetry-compatible monitoring |

The logical design is cloud-neutral. On AWS, for example, it could map to CloudFront/WAF, API Gateway, ECS/Fargate, S3, Aurora or DynamoDB, SQS, Step Functions, Secrets Manager/KMS, and CloudWatch/OpenTelemetry. Equivalent components can be substituted on another cloud or in a private environment without changing the episode workflow.

## Request and processing lifecycle

1. An authenticated producer requests an upload URL and sends a transcript directly to encrypted object storage. The API records the tenant, content hash, schema version, provider policy, and retention policy without proxying a large file through application memory.
2. The API validates type and size, creates an idempotent job, and enqueues the episode. The caller receives a job identifier immediately rather than holding an HTTP connection while model and search calls run.
3. A durable orchestrator starts an ephemeral worker and checkpoints each bounded stage: normalization, plan creation, editorial analysis, claim extraction, evidence retrieval, adjudication, and deterministic validation.
4. The model gateway sends structured requests only to the tenant-approved provider. The evidence gateway searches an approved knowledge base first and uses allowlisted live search only when freshness policy requires it.
5. ProofCast stores versioned JSON and Markdown results, evidence metadata, validation outcomes, and a content-redacted execution trace.
6. A producer reviews the draft beside transcript quotes, timestamps, verdict rationales, evidence, and retrieval dates. Only an explicit approval can export a version to downstream systems.

The model handles semantic judgment. Deterministic code owns enforceable guarantees: schemas, summary length, quote fidelity, safe paths, bounded plans, accepted verdict labels, and the requirement that an affirmative verdict contain evidence.

## Scalability and performance

The queue separates interactive uploads from long-running generation and provides backpressure during bursts. Workers scale horizontally using queue depth and oldest-message age, with minimum and maximum task counts preventing both cold-start surprises and runaway spend.

Scaling occurs at two levels:

- Episodes run independently across workers.
- Evidence lookups within one episode can run concurrently under a small per-job limit.

Long transcripts should be chunked on speaker turns and semantic boundaries while retaining timestamp ranges. Chunk results pass through a bounded reduce step, but original turns remain authoritative for quote validation.

Hard limits on input bytes, transcript turns, candidate claims, searches, output tokens, stage duration, and repair attempts prevent one pathological job from monopolizing capacity. Provider-specific rate limits are enforced centrally so adding workers cannot accidentally overload an external dependency.

Illustrative pilot objectives—subject to measurement and agreement—are:

| Measure | Initial objective | Validation |
|---|---:|---|
| Job-acceptance API availability | 99.9% monthly | Synthetic monitoring and error-budget reporting |
| Job-acceptance latency | p95 under 500 ms after upload | API metrics and load tests |
| Valid result schema | 100% | Runtime validation and CI fixtures |
| Published quote fidelity | 100% exact transcript match | Deterministic validator |
| Metadata recovery point | 15 minutes or better | Backup and restore exercise |
| Service recovery time | Under 1 hour | Regional recovery drill |

End-to-end generation time depends on transcript length, claim count, model capacity, and evidence latency. It should be measured per transcript minute rather than hidden behind an unsupported universal SLA.

## Cost management

Model inference and search are expected to dominate unit cost. The service records estimated and actual usage per stage, episode, tenant, provider, and model.

```text
episode cost = input tokens × input rate
             + output tokens × output rate
             + search calls × search rate
             + worker runtime × compute rate
             + storage and telemetry
```

Controls include:

- Use smaller approved models for extraction and classification, reserving stronger models for ambiguous evidence synthesis.
- Cache evidence with source-specific freshness windows; current events, regulations, healthcare, and pricing receive shorter lifetimes.
- Apply per-tenant budgets, concurrency quotas, maximum claims, search limits, token limits, and repair limits.
- Scale workers to zero or a low floor when idle.
- Compact prompts without losing speaker or timestamp provenance.
- Alert on token growth, cache-miss changes, unexpected provider routing, and projected budget exhaustion.
- Evaluate every routing change against the same golden cases so cost reduction cannot silently lower fact-check quality.

Hosted inference is the sensible pilot default. Dedicated or self-hosted accelerators become attractive only when measured sustained utilization, latency, or data-residency requirements justify their fixed operational cost.

## Fault tolerance and recovery

Queue delivery is treated as at least once. Idempotency keys, conditional checkpoint writes, and immutable result versions make duplicate delivery safe. A restarted worker resumes from the last completed stage instead of repeating every model and search request.

Retries are bounded and classified:

- Timeouts, throttling, and transient 5xx responses use exponential backoff with jitter.
- Invalid input, authentication failure, policy violations, and unsupported capabilities fail fast.
- Circuit breakers pause calls to an unhealthy provider.
- Exhausted jobs move to a dead-letter queue with the failed stage and a safe diagnostic code.
- Operator replay creates an audited attempt and cannot overwrite an approved result.

Partial degradation is preferable to invented certainty. If evidence retrieval is unavailable, ProofCast can still create editorial notes, but affected claims become `Unverifiable`, the report is marked incomplete, and export remains blocked. Invalid provider JSON receives at most one bounded repair attempt before producing a reviewable failure.

Managed multi-zone data services, object versioning, encrypted backups, and restore tests protect state. A warm secondary region is a better initial tradeoff than active/active complexity; active/active should be introduced only when contractual recovery objectives require it.

## Security, privacy, and tenant isolation

Transcripts, retrieved pages, and model output are all untrusted inputs.

- **Identity:** OIDC/OAuth for users and short-lived workload identities for services.
- **Authorization:** roles for uploader, reviewer, tenant administrator, and operator; tenant context is carried through queues, storage keys, database queries, traces, and provider policy.
- **Encryption:** TLS in transit and managed-key encryption for objects, databases, queues, backups, and logs.
- **Network boundaries:** workers have no public inbound path; egress is restricted to approved model and evidence endpoints.
- **Secrets:** credentials remain in a secrets manager and appear in jobs only as opaque references.
- **Input controls:** strict file limits, schema validation, UTF-8 normalization, safe filenames, and malware scanning before processing.
- **Prompt-injection resistance:** transcript and retrieved content are delimited as data, cannot redefine system policy, and cannot cause arbitrary tool execution or URL fetching.
- **Output controls:** generated content is never executed; URLs are normalized and Markdown/HTML is sanitized before review.
- **Supply chain:** pinned dependencies and images, SBOM generation, vulnerability and secret scanning, signed artifacts, non-root containers, read-only filesystems, and CI gates.
- **Auditability:** immutable events identify who uploaded, reviewed, approved, exported, replayed, or changed configuration and which version was affected.

Raw transcripts, full prompts, retrieved pages, and provider responses are excluded from ordinary logs. Retention is policy-driven by data class; deletion must cover primary objects, derived drafts, indexes, caches, and provider-side retained data where supported. Legal holds are explicit exceptions rather than silent indefinite retention.

## Human review and publishing safety

The initial production state machine is:

```text
DRAFT -> NEEDS_REVIEW -> APPROVED -> EXPORTED
   |          |
   +------> REVISION_REQUESTED -> new immutable DRAFT version
```

Reviewers see source quotes, timestamps, each claim's classification and verdict, evidence excerpts, source dates, retrieval time, warnings, and the model/pipeline version. Any edit, rerun, evidence refresh, or policy change creates a new version and invalidates the previous approval.

Medical, legal, financial, safety-related, defamatory, personally identifying, low-confidence, or conflicting-source claims always require explicit review. A downstream integration receives only an approved result and uses a separate least-privilege credential.

## Observability and quality operations

Each job receives a correlation identifier propagated through the API, queue, orchestrator, worker, provider gateway, evidence gateway, storage, and review layers.

- **Logs:** stage transitions, plan bindings, capability names, retries, safe error codes, validation results, and routing decisions—never hidden chain-of-thought or raw customer content.
- **Metrics:** queue depth, oldest job age, stage latency, provider latency, retries, token/search usage, validation failures, evidence coverage, review overrides, and cost per episode.
- **Traces:** content-redacted spans that reveal where time and failures occur without exposing prompts or transcripts.
- **Alerts:** backlog age, error-budget burn, provider failure, DLQ growth, schema regressions, evidence-coverage drops, and budget anomalies.

Offline evaluations use synthetic transcripts and captured, license-compatible evidence fixtures. Production sampling tracks quote fidelity, schema validity, evidence coverage, reviewer acceptance, override reasons, latency, and unit cost. Prompt, model, retrieval, or policy changes must pass the same regression set before release.

## Deployment and release process

1. CI runs unit, contract, security, dependency, and container tests against an immutable commit.
2. The build produces a pinned image, SBOM, provenance metadata, and vulnerability-scan result.
3. The same image is promoted through development and staging; configuration and secret references vary by environment.
4. Staging runs synthetic end-to-end jobs, failure injection, authorization checks, and provider preflight tests.
5. Production begins with a small canary, measured against validation, latency, quality, and cost thresholds.
6. Rollback switches traffic to the previous signed image; result and schema versions remain readable.

Infrastructure should be defined as code, reviewed through pull requests, and protected by policy checks. Database and schema changes are backward-compatible and expand-before-contract. Provider credentials rotate independently of application releases.

## Provider portability and tradeoffs

The workflow depends on explicit capabilities—planning, editorial generation, claim extraction, evidence retrieval, and adjudication—instead of spreading one vendor SDK through the application. A provider profile should declare structured-output support, tool calling, context limits, attribution, usage reporting, retention, and residency behavior.

Model generation and evidence retrieval remain separate contracts. A client-managed model can be paired with an internal knowledge base, a separately approved search service, or no retrieval. Without approved evidence, factual claims remain `Unverifiable`; switching models cannot manufacture provenance.

The principal design tradeoffs are intentional:

- Asynchronous jobs add infrastructure but make backpressure, retries, and long-running work manageable.
- Human approval adds latency but prevents unreviewed AI output from publishing.
- Immutable versions use more storage but preserve provenance and auditability.
- A provider abstraction adds conformance work but limits vendor coupling.
- Warm regional recovery costs less and is easier to validate than premature active/active operation.

The first production milestone should therefore be a single-region, multi-zone, human-gated pilot with explicit quotas and measured quality. Expansion follows evidence from real workload, reliability, cost, and governance—not architecture for its own sake.
