from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from proofcast.cli import main
from proofcast.models import ProofCastResults, Verdict
from proofcast.parsers import load_transcript
from proofcast.validators import count_words, validate_report

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_TRANSCRIPT = PROJECT_ROOT / "examples" / "public_demo_transcript.json"
PUBLIC_FIXTURE = PROJECT_ROOT / "fixtures" / "public_demo_reports.json"


class PublicDemoTests(unittest.TestCase):
    def test_synthetic_fixture_runs_end_to_end_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = Path(temporary_dir) / "public-demo-output"
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "run",
                str(PUBLIC_TRANSCRIPT),
                "--provider",
                "fixture",
                "--fixture",
                str(PUBLIC_FIXTURE),
                "--output",
                str(output_dir),
            ]

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(argv)

            self.assertEqual(exit_code, 0, stderr.getvalue() or stdout.getvalue())
            results = ProofCastResults.model_validate_json(
                (output_dir / "results.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(results.episodes), 1)
            report = results.episodes[0]
            transcript = load_transcript(PUBLIC_TRANSCRIPT)
            validate_report(report, transcript)

            self.assertEqual(report.episode_id, "public-demo-001")
            self.assertEqual(len(report.notes.takeaways), 5)
            self.assertGreaterEqual(count_words(report.summary), 200)
            self.assertLessEqual(count_words(report.summary), 300)
            self.assertEqual(
                {item.verdict for item in report.fact_checks},
                {
                    Verdict.VERIFIED_TRUE,
                    Verdict.POSSIBLY_OUTDATED,
                    Verdict.UNVERIFIABLE,
                },
            )
            self.assertTrue(
                (output_dir / "episodes" / "public-demo-001.json").is_file()
            )
            markdown = (output_dir / "episodes" / "public-demo-001.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("## Summary", markdown)
            self.assertIn("## Fact checks", markdown)

            trace_paths = list((output_dir / "traces").glob("*.jsonl"))
            self.assertEqual(len(trace_paths), 1)
            trace_events = [
                json.loads(line)
                for line in trace_paths[0].read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(trace_events)
            self.assertFalse(
                any(event.get("status") == "failed" for event in trace_events)
            )


if __name__ == "__main__":
    unittest.main()
