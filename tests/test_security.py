from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from proofcast.models import EpisodeReport, Evidence
from proofcast.parsers import (
    MAX_TRANSCRIPT_BYTES,
    TranscriptParseError,
    load_transcript,
)
from proofcast.providers.fixture import FixtureProvider
from proofcast.renderers import render_episode_markdown
from proofcast.tracing import RunTracer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_TRANSCRIPT = PROJECT_ROOT / "examples" / "public_demo_transcript.json"
PUBLIC_FIXTURE = PROJECT_ROOT / "fixtures" / "public_demo_reports.json"


def _public_report() -> EpisodeReport:
    transcript = load_transcript(PUBLIC_TRANSCRIPT)
    provider = FixtureProvider(PUBLIC_FIXTURE)
    plan, plan_usage = provider.create_plan(transcript)
    editorial, editorial_usage = provider.create_editorial(transcript)
    claims, claim_usage = provider.extract_claims(transcript)
    checks, verification_usage = provider.verify_claims(transcript, claims)
    return EpisodeReport(
        episode_id=transcript.episode_id,
        title=transcript.title,
        host=transcript.host,
        guests=transcript.guests,
        source_file=transcript.source_file,
        summary=editorial.summary,
        notes=editorial.notes,
        fact_checks=checks.fact_checks,
        execution_plan=plan,
        provider=provider.name,
        model=provider.model,
        usage=[
            plan_usage,
            editorial_usage,
            claim_usage,
            *verification_usage,
        ],
    )


class InputBoundaryTests(unittest.TestCase):
    def test_oversized_transcript_is_rejected_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "oversized.json"
            source.write_bytes(b" " * (MAX_TRANSCRIPT_BYTES + 1))

            with self.assertRaisesRegex(TranscriptParseError, "input limit"):
                load_transcript(source)


class TraceBoundaryTests(unittest.TestCase):
    def test_caller_supplied_run_id_cannot_traverse_directories(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(ValueError, "run_id must contain"),
        ):
            RunTracer(Path(temporary), run_id="../../outside")

    def test_existing_trace_symlink_cannot_escape_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_dir = root / "output" / "traces"
            trace_dir.mkdir(parents=True)
            outside = root / "outside.jsonl"
            try:
                (trace_dir / "trace-test.jsonl").symlink_to(outside)
            except OSError as exc:  # pragma: no cover - platform permission edge
                self.skipTest(f"symlinks are unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "trace path escapes"):
                RunTracer(root / "output", run_id="trace-test")
            self.assertFalse(outside.exists())


class RenderingBoundaryTests(unittest.TestCase):
    def test_untrusted_markdown_is_rendered_as_inert_text(self) -> None:
        report = _public_report().model_copy(
            update={
                "title": "<img src=x> [click](https://attacker.invalid)",
                "host": "**spoofed host**",
            }
        )

        rendered = render_episode_markdown(report)

        self.assertIn(r"\<img src=x\>", rendered)
        self.assertIn(r"\[click\](https://attacker.invalid)", rendered)
        self.assertIn(r"\*\*spoofed host\*\*", rendered)

    def test_evidence_url_rejects_markdown_delimiters(self) -> None:
        with self.assertRaises(ValidationError):
            Evidence(
                title="Unsafe source",
                url="https://example.com/>[spoof](https://attacker.invalid)",
                evidence_note="Unsafe Markdown delimiter in a URL.",
            )


if __name__ == "__main__":
    unittest.main()
