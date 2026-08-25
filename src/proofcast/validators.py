from __future__ import annotations

import re
import unicodedata

from proofcast.models import (
    ClaimExtraction,
    ClaimType,
    EpisodeReport,
    EpisodeTranscript,
    ExecutionPlan,
    TranscriptTurn,
    Verdict,
)

WORD_RE = re.compile(r"\b[\w’'-]+\b", flags=re.UNICODE)
TOPIC_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PLAN_CAPABILITIES = (
    ("input normalization", ("normaliz", "ingest", "parse")),
    ("editorial analysis", ("editorial", "summary", "takeaway", "synthes")),
    ("claim extraction", ("claim",)),
    ("evidence retrieval", ("evidence", "search", "retriev")),
    ("claim adjudication", ("adjudicat", "verdict")),
    ("report assembly", ("assembl",)),
    ("quality validation", ("validat", "quality")),
    ("result persistence", ("persist", "write", "output", "render")),
)


class ReportValidationError(ValueError):
    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues))


def count_words(value: str) -> int:
    return len(WORD_RE.findall(value))


def normalize_excerpt(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _normalized_unique(values: list[str]) -> bool:
    normalized = [normalize_excerpt(value).casefold() for value in values]
    return len(normalized) == len(set(normalized))


def _turn_index(
    transcript: EpisodeTranscript,
) -> dict[tuple[str, str], list[TranscriptTurn]]:
    turns_by_key: dict[tuple[str, str], list[TranscriptTurn]] = {}
    for turn in transcript.transcript:
        key = (turn.timestamp, normalize_excerpt(turn.speaker).casefold())
        turns_by_key.setdefault(key, []).append(turn)
    return turns_by_key


def validate_execution_plan(plan: ExecutionPlan) -> dict[str, str]:
    """Validate and bind model-authored steps to distinct approved capabilities."""

    issues: list[str] = []
    plan_ids = [step.id for step in plan.steps]
    if len(plan_ids) != len(set(plan_ids)):
        issues.append("execution plan step IDs must be unique")

    seen_plan_ids: set[str] = set()
    for step in plan.steps:
        unknown = [
            dependency
            for dependency in step.depends_on
            if dependency not in seen_plan_ids
        ]
        if unknown:
            issues.append(
                f"plan step {step.id} depends on missing or later step(s): "
                + ", ".join(unknown)
            )
        seen_plan_ids.add(step.id)

    bindings: dict[str, str] = {}
    claimed_steps: set[str] = set()
    for capability, keywords in PLAN_CAPABILITIES:
        matched_step = next(
            (
                step
                for step in plan.steps
                if step.id not in claimed_steps
                and any(
                    keyword in f"{step.id} {step.action} {step.purpose}".casefold()
                    for keyword in keywords
                )
            ),
            None,
        )
        if matched_step is None:
            issues.append(
                f"execution plan does not cover {capability} in a distinct step"
            )
            continue
        bindings[capability] = matched_step.id
        claimed_steps.add(matched_step.id)

    ancestors: dict[str, set[str]] = {}
    for step in plan.steps:
        step_ancestors = set(step.depends_on)
        for dependency in step.depends_on:
            step_ancestors.update(ancestors.get(dependency, set()))
        ancestors[step.id] = step_ancestors

    required_precedence = (
        ("input normalization", "editorial analysis"),
        ("input normalization", "claim extraction"),
        ("claim extraction", "evidence retrieval"),
        ("evidence retrieval", "claim adjudication"),
        ("editorial analysis", "report assembly"),
        ("claim adjudication", "report assembly"),
        ("report assembly", "quality validation"),
        ("quality validation", "result persistence"),
    )
    if len(bindings) == len(PLAN_CAPABILITIES):
        for prerequisite, dependent in required_precedence:
            prerequisite_step = bindings[prerequisite]
            dependent_step = bindings[dependent]
            if prerequisite_step not in ancestors.get(dependent_step, set()):
                issues.append(
                    f"execution plan must make {dependent} depend on {prerequisite}"
                )

    if issues:
        raise ReportValidationError(issues)
    return bindings


def validate_claim_extraction(
    extraction: ClaimExtraction, transcript: EpisodeTranscript
) -> None:
    """Prove claim provenance and routing policy before retrieval is allowed."""

    issues: list[str] = []
    turns_by_key = _turn_index(transcript)
    seen_claims: set[tuple[str, str, str]] = set()
    factual_types = {ClaimType.FACT, ClaimType.TIME_SENSITIVE_FACT}

    for index, claim in enumerate(extraction.claims, start=1):
        key = (claim.timestamp, normalize_excerpt(claim.speaker).casefold())
        source_turns = turns_by_key.get(key, [])
        if not source_turns:
            issues.append(
                f"claim {index} timestamp/speaker does not identify a source turn"
            )
        elif not any(
            normalize_excerpt(claim.source_excerpt)
            in normalize_excerpt(source_turn.text)
            for source_turn in source_turns
        ):
            issues.append(f"claim {index} source_excerpt is not verbatim source text")

        identity = (
            normalize_excerpt(claim.claim).casefold(),
            claim.timestamp,
            normalize_excerpt(claim.speaker).casefold(),
        )
        if identity in seen_claims:
            issues.append(f"claim {index} duplicates an earlier extracted claim")
        seen_claims.add(identity)

        expected_checkable = claim.claim_type in factual_types
        if claim.checkable != expected_checkable:
            issues.append(f"claim {index} checkable flag conflicts with its claim type")
        if claim.checkable and not claim.search_query:
            issues.append(f"claim {index} is checkable but has no search query")
        if not claim.checkable and claim.search_query is not None:
            issues.append(f"claim {index} is non-checkable but has a search query")

    if issues:
        raise ReportValidationError(issues)


def validate_report(report: EpisodeReport, transcript: EpisodeTranscript) -> None:
    """Enforce requirements that a model schema alone cannot guarantee."""

    issues: list[str] = []

    if report.episode_id != transcript.episode_id:
        issues.append("episode_id does not match the source transcript")
    if report.title != transcript.title:
        issues.append("title does not match the source transcript")
    if report.host != transcript.host:
        issues.append("host does not match the source transcript")
    if report.guests != transcript.guests:
        issues.append("guests do not match the source transcript")
    if report.source_file != transcript.source_file:
        issues.append("source_file does not match the source transcript")

    summary_words = count_words(report.summary)
    if not 200 <= summary_words <= 300:
        issues.append(
            f"summary contains {summary_words} words; expected 200 through 300"
        )

    if not _normalized_unique(report.notes.takeaways):
        issues.append("takeaways must be unique after normalization")
    if not _normalized_unique(report.notes.topics):
        issues.append("topics must be unique after normalization")
    invalid_topics = [
        topic for topic in report.notes.topics if TOPIC_RE.fullmatch(topic) is None
    ]
    if invalid_topics:
        issues.append("topics must be lowercase slug-style labels")

    turns_by_key = _turn_index(transcript)
    for index, quote in enumerate(report.notes.notable_quotes, start=1):
        key = (quote.timestamp, normalize_excerpt(quote.speaker).casefold())
        source_turns = turns_by_key.get(key, [])
        if not source_turns:
            issues.append(
                f"quote {index} timestamp/speaker does not identify a source turn"
            )
            continue
        if not any(
            normalize_excerpt(quote.quote) in normalize_excerpt(source_turn.text)
            for source_turn in source_turns
        ):
            issues.append(f"quote {index} is not an exact source-turn excerpt")

    try:
        validate_execution_plan(report.execution_plan)
    except ReportValidationError as exc:
        issues.extend(exc.issues)

    fact_check_keys = [
        (
            normalize_excerpt(item.claim).casefold(),
            item.timestamp,
            normalize_excerpt(item.speaker).casefold(),
        )
        for item in report.fact_checks
    ]
    if len(fact_check_keys) != len(set(fact_check_keys)):
        issues.append("fact checks must not contain duplicate claims")

    for index, fact_check in enumerate(report.fact_checks, start=1):
        key = (
            fact_check.timestamp,
            normalize_excerpt(fact_check.speaker).casefold(),
        )
        source_turns = turns_by_key.get(key, [])
        if not source_turns:
            issues.append(
                f"fact check {index} timestamp/speaker does not identify a source turn"
            )
        elif not any(
            normalize_excerpt(fact_check.source_excerpt)
            in normalize_excerpt(source_turn.text)
            for source_turn in source_turns
        ):
            issues.append(
                f"fact check {index} source_excerpt is not verbatim source text"
            )
        if len(fact_check.verification) < 20:
            issues.append(
                f"fact check {index} needs a substantive verification explanation"
            )
        if (
            fact_check.verdict
            in {
                Verdict.VERIFIED_TRUE,
                Verdict.POSSIBLY_OUTDATED,
            }
            and not fact_check.evidence
        ):
            issues.append(
                f"fact check {index} uses verdict '{fact_check.verdict.value}' "
                "without evidence"
            )
        if (
            fact_check.verdict == Verdict.POSSIBLY_OUTDATED
            and not fact_check.needs_human_review
        ):
            issues.append(
                f"fact check {index} must request human review when evidence is "
                "outdated, incomplete, or conflicting"
            )
        evidence_urls = [item.url for item in fact_check.evidence]
        if len(evidence_urls) != len(set(evidence_urls)):
            issues.append(f"fact check {index} contains duplicate evidence URLs")

    if issues:
        raise ReportValidationError(issues)
