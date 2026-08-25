from __future__ import annotations

from typing import Protocol

from proofcast.models import (
    ClaimExtraction,
    EditorialAnalysis,
    EpisodeTranscript,
    ExecutionPlan,
    FactCheckBatch,
    UsageRecord,
)


class ModelProvider(Protocol):
    """The bounded capabilities the workflow is allowed to invoke."""

    name: str
    model: str

    def create_plan(
        self, transcript: EpisodeTranscript
    ) -> tuple[ExecutionPlan, UsageRecord]: ...

    def create_editorial(
        self, transcript: EpisodeTranscript
    ) -> tuple[EditorialAnalysis, UsageRecord]: ...

    def extract_claims(
        self, transcript: EpisodeTranscript
    ) -> tuple[ClaimExtraction, UsageRecord]: ...

    def verify_claims(
        self, transcript: EpisodeTranscript, extraction: ClaimExtraction
    ) -> tuple[FactCheckBatch, list[UsageRecord]]: ...
