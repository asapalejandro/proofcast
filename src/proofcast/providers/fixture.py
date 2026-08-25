from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from proofcast.models import (
    ClaimExtraction,
    EditorialAnalysis,
    EpisodeTranscript,
    ExecutionPlan,
    FactCheckBatch,
    UsageRecord,
)


class FixtureProvider:
    """Deterministic provider for review, CI, and no-credential execution."""

    name = "fixture"
    model = "proofcast-golden-v1"

    def __init__(self, fixture_path: Path | None = None) -> None:
        self.fixture_path = fixture_path or self._find_default_fixture()
        try:
            raw = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Offline fixture not found at {self.fixture_path}. "
                "Pass --fixture with the fixture file path."
            ) from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Offline fixture is invalid JSON at line {exc.lineno}: "
                f"{self.fixture_path}"
            ) from exc
        if not isinstance(raw, dict):
            raise TypeError(
                "Offline fixture root must be an object keyed by episode ID"
            )
        self._fixtures: dict[str, dict[str, Any]] = raw

    @staticmethod
    def _find_default_fixture() -> Path:
        candidates = [
            Path.cwd() / "fixtures" / "offline_reports.json",
            Path(__file__).resolve().parents[3] / "fixtures" / "offline_reports.json",
        ]
        return next((path for path in candidates if path.exists()), candidates[0])

    def _episode(self, transcript: EpisodeTranscript) -> dict[str, Any]:
        try:
            return self._fixtures[transcript.episode_id]
        except KeyError as exc:
            raise RuntimeError(
                f"No offline result exists for episode '{transcript.episode_id}'. "
                "Use --provider openai for new transcripts."
            ) from exc

    def _usage(self, transcript: EpisodeTranscript, stage: str) -> UsageRecord:
        return UsageRecord(response_id=f"fixture:{transcript.episode_id}:{stage}")

    def create_plan(
        self, transcript: EpisodeTranscript
    ) -> tuple[ExecutionPlan, UsageRecord]:
        value = ExecutionPlan.model_validate(self._episode(transcript)["plan"])
        return value, self._usage(transcript, "plan")

    def create_editorial(
        self, transcript: EpisodeTranscript
    ) -> tuple[EditorialAnalysis, UsageRecord]:
        value = EditorialAnalysis.model_validate(self._episode(transcript)["editorial"])
        return value, self._usage(transcript, "editorial")

    def extract_claims(
        self, transcript: EpisodeTranscript
    ) -> tuple[ClaimExtraction, UsageRecord]:
        value = ClaimExtraction.model_validate(self._episode(transcript)["claims"])
        return value, self._usage(transcript, "claims")

    def verify_claims(
        self, transcript: EpisodeTranscript, extraction: ClaimExtraction
    ) -> tuple[FactCheckBatch, list[UsageRecord]]:
        payload = deepcopy(self._episode(transcript)["fact_checks"])
        raw_fact_checks = payload.get("fact_checks", [])
        if len(raw_fact_checks) != len(extraction.claims):
            raise RuntimeError(
                f"Offline fixture claim-count mismatch for '{transcript.episode_id}'."
            )
        for raw_fact_check, claim in zip(
            raw_fact_checks, extraction.claims, strict=True
        ):
            raw_fact_check["source_excerpt"] = claim.source_excerpt
        value = FactCheckBatch.model_validate(payload)
        return value, [self._usage(transcript, "verification")]
