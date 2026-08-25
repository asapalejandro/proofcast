from __future__ import annotations

from pathlib import Path

from proofcast.models import ClaimType, EpisodeReport, EpisodeTranscript, UsageRecord
from proofcast.providers.base import ModelProvider
from proofcast.renderers import write_episode
from proofcast.tracing import RunTracer
from proofcast.validators import (
    validate_claim_extraction,
    validate_execution_plan,
    validate_report,
)


class ProofCastOrchestrator:
    """Execute a bounded, observable agent workflow for one episode."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        output_dir: Path,
        tracer: RunTracer,
    ) -> None:
        self.provider = provider
        self.output_dir = output_dir
        self.tracer = tracer

    def process_episode(self, transcript: EpisodeTranscript) -> EpisodeReport:
        episode_id = transcript.episode_id
        usage: list[UsageRecord] = []

        with self.tracer.span(
            stage="plan",
            action="provider.create_plan",
            episode_id=episode_id,
            detail="Create a bounded execution plan from transcript metadata",
        ):
            plan, plan_usage = self.provider.create_plan(transcript)
            usage.append(plan_usage)

        with self.tracer.span(
            stage="plan",
            action="validate_and_bind_plan",
            episode_id=episode_id,
            detail="Bind model-authored steps to the approved workflow capabilities",
        ):
            plan_bindings = validate_execution_plan(plan)

        for capability, step_id in plan_bindings.items():
            self.tracer.emit(
                stage="plan",
                action="bind_capability",
                status="approved",
                episode_id=episode_id,
                detail=f"capability={capability} step={step_id}",
            )

        with self.tracer.span(
            stage="normalization",
            action="canonicalize_validated_transcript",
            episode_id=episode_id,
            detail=(
                f"planned_step={plan_bindings['input normalization']} "
                "confirm the schema-validated transcript representation"
            ),
        ):
            transcript = EpisodeTranscript.model_validate(
                transcript.model_dump(mode="json")
            )

        with self.tracer.span(
            stage="editorial",
            action="provider.create_editorial",
            episode_id=episode_id,
            detail=(
                f"planned_step={plan_bindings['editorial analysis']} draft summary, "
                "takeaways, exact quotes, and topic labels"
            ),
        ):
            editorial, editorial_usage = self.provider.create_editorial(transcript)
            usage.append(editorial_usage)

        with self.tracer.span(
            stage="claim_extraction",
            action="provider.extract_claims",
            episode_id=episode_id,
            detail=(
                f"planned_step={plan_bindings['claim extraction']} select atomic "
                "claims and classify their verifiability"
            ),
        ):
            claims, extraction_usage = self.provider.extract_claims(transcript)
            usage.append(extraction_usage)

        self.tracer.emit(
            stage="claim_extraction",
            action="record_candidate_count",
            status="completed",
            episode_id=episode_id,
            detail=f"Selected {len(claims.claims)} candidate claims",
        )

        with self.tracer.span(
            stage="claim_extraction",
            action="validate_claim_lineage_and_routing",
            episode_id=episode_id,
            detail="Verify verbatim source excerpts and deterministic search routing",
        ):
            validate_claim_extraction(claims, transcript)

        searchable_types = {ClaimType.FACT, ClaimType.TIME_SENSITIVE_FACT}
        for index, claim in enumerate(claims.claims, start=1):
            if claim.checkable and claim.claim_type in searchable_types:
                route = (
                    "web_search_then_structured_adjudication"
                    if self.provider.name == "openai"
                    else "fixture_evidence_lookup"
                )
            else:
                route = "policy_unverifiable_without_search"
            self.tracer.emit(
                stage="verification",
                action="route_claim",
                status="approved",
                episode_id=episode_id,
                detail=(
                    f"claim_index={index} type={claim.claim_type.value} route={route}"
                ),
            )

        with self.tracer.span(
            stage="verification",
            action="provider.verify_claims",
            episode_id=episode_id,
            detail=(
                f"planned_steps={plan_bindings['evidence retrieval']},"
                f"{plan_bindings['claim adjudication']} retrieve evidence for "
                "checkable claims and conservatively adjudicate every candidate"
            ),
        ):
            fact_checks, verification_usage = self.provider.verify_claims(
                transcript, claims
            )
            usage.extend(verification_usage)

        expected_claims = [
            (item.claim, item.timestamp, item.speaker, item.claim_type)
            for item in claims.claims
        ]
        returned_claims = [
            (item.claim, item.timestamp, item.speaker, item.claim_type)
            for item in fact_checks.fact_checks
        ]
        if returned_claims != expected_claims:
            raise RuntimeError(
                "Provider fact checks must preserve every extracted claim, in order, "
                "with its source timestamp, speaker, and claim type"
            )

        for index, (claim, fact_check) in enumerate(
            zip(claims.claims, fact_checks.fact_checks, strict=True), start=1
        ):
            fact_check.source_excerpt = claim.source_excerpt
            self.tracer.emit(
                stage="verification",
                action="record_claim_outcome",
                status="completed",
                episode_id=episode_id,
                detail=(
                    f"claim_index={index} verdict={fact_check.verdict.value} "
                    f"evidence_sources={len(fact_check.evidence)} "
                    f"human_review={fact_check.needs_human_review}"
                ),
            )

        with self.tracer.span(
            stage="assembly",
            action="assemble_typed_report",
            episode_id=episode_id,
            detail=(
                f"planned_step={plan_bindings['report assembly']} combine provider "
                "results with immutable source metadata"
            ),
        ):
            report = EpisodeReport(
                episode_id=transcript.episode_id,
                title=transcript.title,
                host=transcript.host,
                guests=transcript.guests,
                source_file=transcript.source_file,
                summary=editorial.summary,
                notes=editorial.notes,
                fact_checks=fact_checks.fact_checks,
                execution_plan=plan,
                provider=self.provider.name,
                model=self.provider.model,
                usage=usage,
            )

        with self.tracer.span(
            stage="validation",
            action="validate_report_contract",
            episode_id=episode_id,
            detail=(
                f"planned_step={plan_bindings['quality validation']} enforce word "
                "count, exact quote and claim provenance, plan integrity, and "
                "evidence policy"
            ),
        ):
            validate_report(report, transcript)

        with self.tracer.span(
            stage="persistence",
            action="write_json_and_markdown",
            episode_id=episode_id,
            detail=(
                f"planned_step={plan_bindings['result persistence']} atomically "
                "persist machine-readable and reviewer-readable outputs"
            ),
        ):
            write_episode(self.output_dir, report)

        return report
