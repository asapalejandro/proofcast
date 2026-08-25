from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from proofcast import __version__
from proofcast.models import ProofCastResults
from proofcast.orchestrator import ProofCastOrchestrator
from proofcast.parsers import TranscriptParseError, discover_inputs, load_transcript
from proofcast.providers.fixture import FixtureProvider
from proofcast.renderers import (
    prepare_output_dir,
    write_aggregate,
    write_json,
    write_run_manifest,
    write_schema,
)
from proofcast.tracing import RunTracer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proofcast",
        description="Evidence-first podcast editorial and fact-check workflow",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Process JSON or TXT podcast transcripts")
    run.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Transcript files or directories containing .json/.txt files",
    )
    run.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("outputs"),
        help="Output directory (default: outputs)",
    )
    run.add_argument(
        "--provider",
        choices=("fixture", "openai"),
        default=os.getenv("PROOFCAST_PROVIDER", "fixture"),
        help="fixture is deterministic and needs no credentials",
    )
    run.add_argument(
        "--fixture",
        type=Path,
        help="Override the deterministic offline fixture JSON path",
    )
    run.add_argument(
        "--model",
        default=os.getenv("PROOFCAST_MODEL", "gpt-5.6-sol"),
        help="OpenAI model identifier",
    )
    run.add_argument(
        "--reasoning-effort",
        choices=("none", "low", "medium", "high", "xhigh", "max"),
        default=os.getenv("PROOFCAST_REASONING_EFFORT", "medium"),
    )
    run.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first episode failure instead of preserving partial success",
    )

    schema = subparsers.add_parser("schema", help="Write the output JSON Schema")
    schema.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("outputs"),
        help="Directory in which to create schema.json",
    )
    return parser


def _make_provider(args: argparse.Namespace):
    if args.provider == "fixture":
        return FixtureProvider(args.fixture)
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is required with --provider openai. "
            "Use --provider fixture for the no-credential demo."
        )
    from proofcast.providers.openai import OpenAIProvider

    return OpenAIProvider(model=args.model, reasoning_effort=args.reasoning_effort)


def run_command(args: argparse.Namespace) -> int:
    output_dir = args.output.expanduser().resolve()
    prepare_output_dir(output_dir)
    tracer = RunTracer(output_dir)
    tracer.emit(
        stage="run",
        action="start",
        status="started",
        detail=f"provider={args.provider}",
    )

    try:
        inputs = discover_inputs(args.inputs)
        provider = _make_provider(args)
    except Exception as exc:  # noqa: BLE001 - sanitize all preflight failures
        tracer.emit(
            stage="preflight",
            action="discover_inputs_and_provider",
            status="failed",
            detail=f"{type(exc).__name__}: {exc}",
        )
        write_run_manifest(
            output_dir,
            reports=[],
            trace_path=tracer.path,
            has_errors=False,
        )
        print(f"proofcast: {exc}", file=sys.stderr)
        return 2

    orchestrator = ProofCastOrchestrator(
        provider=provider,
        output_dir=output_dir,
        tracer=tracer,
    )
    reports = []
    errors: list[dict[str, str]] = []
    episode_ids: set[str] = set()

    for source in inputs:
        try:
            with tracer.span(
                stage="ingestion",
                action="parse_and_validate_transcript",
                detail=source.name,
            ):
                transcript = load_transcript(source)
            episode_key = transcript.episode_id.casefold()
            if episode_key in episode_ids:
                raise TranscriptParseError(
                    f"Duplicate episode_id '{transcript.episode_id}' would overwrite "
                    "output on a case-insensitive filesystem"
                )
            episode_ids.add(episode_key)
            reports.append(orchestrator.process_episode(transcript))
        except Exception as exc:  # noqa: BLE001 - isolate failures by episode
            error = {
                "source_file": source.name,
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            errors.append(error)
            tracer.emit(
                stage="episode",
                action="preserve_failure",
                status="failed",
                detail=f"{source.name}: {type(exc).__name__}: {exc}",
            )
            if args.fail_fast:
                break

    if reports:
        results = ProofCastResults(episodes=reports)
        aggregate_path = write_aggregate(output_dir, results)
        schema_path = write_schema(output_dir)
        tracer.emit(
            stage="run",
            action="write_aggregate",
            status="completed",
            detail=(
                f"episodes={len(reports)} output={aggregate_path.name} "
                f"schema={schema_path.name}"
            ),
        )
    errors_path = output_dir / "errors.json"
    if errors:
        write_json(errors_path, {"errors": errors})
    else:
        errors_path.unlink(missing_ok=True)

    status = (
        "completed" if reports and not errors else "partial" if reports else "failed"
    )
    tracer.emit(
        stage="run",
        action="finish",
        status=status,
        detail=f"succeeded={len(reports)} failed={len(errors)}",
    )
    write_run_manifest(
        output_dir,
        reports=reports,
        trace_path=tracer.path,
        has_errors=bool(errors),
    )
    if reports:
        print(f"Results: {output_dir / 'results.json'}")
        print(f"Trace:   {tracer.path}")
    return 0 if reports and not errors else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "schema":
        path = write_schema(args.output.expanduser().resolve())
        print(path)
        return 0
    return run_command(args)
