from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TRANSCRIPT_TURNS = 5_000
MAX_TURN_TEXT_CHARS = 20_000


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def validate_timestamp_value(value: str) -> str:
    if not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", value):
        raise ValueError("timestamp must use MM:SS or HH:MM:SS")
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        _, seconds = parts
    else:
        _, minutes, seconds = parts
        if minutes >= 60:
            raise ValueError("minutes must be below 60 in HH:MM:SS")
    if seconds >= 60:
        raise ValueError("seconds must be below 60")
    return value


def validate_episode_id_value(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError(
            "episode_id must contain only letters, numbers, dots, underscores, "
            "or hyphens"
        )
    if ".." in value:
        raise ValueError("episode_id must not contain consecutive dots")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TranscriptTurn(StrictModel):
    timestamp: str
    speaker: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=MAX_TURN_TEXT_CHARS)
    section: str | None = Field(default=None, max_length=200)

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return validate_timestamp_value(value)


class EpisodeTranscript(StrictModel):
    episode_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=500)
    host: str = Field(min_length=1, max_length=200)
    guests: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=100
    )
    transcript: list[TranscriptTurn] = Field(
        min_length=1, max_length=MAX_TRANSCRIPT_TURNS
    )
    source_file: str | None = Field(default=None, max_length=255)

    @field_validator("episode_id")
    @classmethod
    def validate_episode_id(cls, value: str) -> str:
        return validate_episode_id_value(value)

    def as_prompt_text(self) -> str:
        metadata = [
            f"Episode ID: {self.episode_id}",
            f"Title: {self.title}",
            f"Host: {self.host}",
            f"Guests: {', '.join(self.guests) if self.guests else 'None listed'}",
            "Transcript:",
        ]
        turns = [
            f"[{turn.timestamp}] {turn.speaker}"
            f"{f' ({turn.section})' if turn.section else ''}: {turn.text}"
            for turn in self.transcript
        ]
        return "\n".join(metadata + turns)

    @classmethod
    def source_stem(cls, source: Path) -> str:
        value = re.sub(r"[^a-z0-9._-]+", "-", source.stem.casefold())
        value = value.strip("._-").replace("..", ".")
        return value or "episode"


class PlanStep(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    action: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)


class ExecutionPlan(StrictModel):
    objective: str = Field(min_length=1)
    steps: list[PlanStep] = Field(min_length=8, max_length=8)


class NotableQuote(StrictModel):
    timestamp: str
    speaker: str = Field(min_length=1)
    quote: str = Field(min_length=1)

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return validate_timestamp_value(value)


class EpisodeNotes(StrictModel):
    takeaways: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=5, max_length=5
    )
    notable_quotes: list[NotableQuote] = Field(min_length=2, max_length=5)
    topics: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=4, max_length=8
    )


class EditorialAnalysis(StrictModel):
    summary: str = Field(min_length=1)
    notes: EpisodeNotes


class ClaimType(str, Enum):
    FACT = "externally_verifiable_fact"
    TIME_SENSITIVE_FACT = "time_sensitive_fact"
    PREDICTION = "prediction"
    OPINION = "opinion_or_advice"
    PERSONAL_ANECDOTE = "personal_anecdote"


class ClaimCandidate(StrictModel):
    claim: str = Field(min_length=1)
    timestamp: str
    speaker: str = Field(min_length=1)
    source_excerpt: str = Field(
        min_length=1,
        description="Verbatim transcript excerpt from the identified source turn",
    )
    claim_type: ClaimType
    checkable: bool
    search_query: str | None = None

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return validate_timestamp_value(value)


class ClaimExtraction(StrictModel):
    claims: list[ClaimCandidate] = Field(default_factory=list)


class Verdict(str, Enum):
    VERIFIED_TRUE = "Verified true"
    POSSIBLY_OUTDATED = "Possibly outdated/inaccurate"
    UNVERIFIABLE = "Unverifiable"


class Evidence(StrictModel):
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    publisher: str | None = None
    published_at: str | None = None
    retrieved_at: str = Field(default_factory=utc_now_iso)
    evidence_note: str = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if (
            len(value) > 2_048
            or any(character.isspace() for character in value)
            or any(character in "<>" for character in value)
        ):
            raise ValueError("evidence URL must be a valid HTTP(S) URL")
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("evidence URL must be a valid HTTP(S) URL") from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("evidence URL must be a valid HTTP(S) URL")
        return value


class FactCheck(StrictModel):
    claim: str = Field(min_length=1)
    timestamp: str
    speaker: str = Field(min_length=1)
    source_excerpt: str = Field(
        min_length=1,
        description=(
            "Verbatim transcript excerpt copied from the corresponding extracted "
            "claim before final report validation"
        ),
    )
    claim_type: ClaimType
    verdict: Verdict
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Confidence in the assigned verdict given the available evidence; "
            "not the probability that the claim is true"
        ),
    )
    verification: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    needs_human_review: bool = Field(
        description=(
            "Claim-specific escalation beyond the mandatory editorial review of "
            "every generated report"
        )
    )

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        return validate_timestamp_value(value)


class FactCheckBatch(StrictModel):
    fact_checks: list[FactCheck] = Field(default_factory=list)


class UsageRecord(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    web_search_calls: int = Field(default=0, ge=0)
    response_id: str | None = None


class EpisodeReport(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    episode_id: str
    title: str
    host: str
    guests: list[str]
    source_file: str | None = None
    summary: str
    notes: EpisodeNotes
    fact_checks: list[FactCheck] = Field(default_factory=list)
    execution_plan: ExecutionPlan
    provider: str
    model: str
    generated_at: str = Field(default_factory=utc_now_iso)
    validation_warnings: list[str] = Field(default_factory=list)
    usage: list[UsageRecord] = Field(default_factory=list)

    @field_validator("episode_id")
    @classmethod
    def validate_episode_id(cls, value: str) -> str:
        return validate_episode_id_value(value)


class ProofCastResults(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    generated_at: str = Field(default_factory=utc_now_iso)
    episodes: list[EpisodeReport] = Field(min_length=1)
