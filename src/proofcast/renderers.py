from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from proofcast.models import EpisodeReport, ProofCastResults

RUN_MANIFEST = ".proofcast-run.json"
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _atomic_text_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_json(path: Path, value: object) -> None:
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")  # type: ignore[attr-defined]
    else:
        payload = value
    _atomic_text_write(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
    )


def write_episode(output_dir: Path, report: EpisodeReport) -> tuple[Path, Path]:
    output_root = output_dir.resolve()
    episodes_dir = (output_root / "episodes").resolve()
    if not episodes_dir.is_relative_to(output_root):
        raise ValueError("managed episodes directory escapes the output directory")
    json_path = (episodes_dir / f"{report.episode_id}.json").resolve()
    markdown_path = (episodes_dir / f"{report.episode_id}.md").resolve()
    if json_path.parent != episodes_dir or markdown_path.parent != episodes_dir:
        raise ValueError("episode output path escaped the managed episodes directory")
    write_json(json_path, report)
    try:
        _atomic_text_write(markdown_path, render_episode_markdown(report))
    except Exception:
        json_path.unlink(missing_ok=True)
        markdown_path.unlink(missing_ok=True)
        raise
    return json_path, markdown_path


def write_aggregate(output_dir: Path, results: ProofCastResults) -> Path:
    path = output_dir / "results.json"
    write_json(path, results)
    return path


def write_schema(output_dir: Path) -> Path:
    path = output_dir / "schema.json"
    write_json(path, ProofCastResults.model_json_schema())
    return path


def _is_managed_relative_path(path: Path) -> bool:
    if path.is_absolute() or ".." in path.parts:
        return False
    if len(path.parts) == 1:
        return path.name in {"results.json", "schema.json", "errors.json"}
    if len(path.parts) != 2:
        return False
    directory, filename = path.parts
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    if directory == "episodes":
        return suffix in {".json", ".md"} and _SAFE_ID_RE.fullmatch(stem) is not None
    if directory == "traces":
        return suffix == ".jsonl" and _SAFE_ID_RE.fullmatch(stem) is not None
    return False


def prepare_output_dir(output_dir: Path) -> None:
    """Remove only artifacts proven to be managed by a previous ProofCast run."""

    output_dir.mkdir(parents=True, exist_ok=True)
    managed = {
        Path("results.json"),
        Path("schema.json"),
        Path("errors.json"),
    }
    manifest_path = output_dir / RUN_MANIFEST
    if manifest_path.exists():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            listed = payload.get("managed_files", [])
            if isinstance(listed, list):
                managed.update(
                    Path(value) for value in listed if isinstance(value, str)
                )
        except (json.JSONDecodeError, OSError):
            pass

    results_path = output_dir / "results.json"
    if results_path.exists():
        try:
            previous = ProofCastResults.model_validate_json(
                results_path.read_text(encoding="utf-8")
            )
            for report in previous.episodes:
                managed.add(Path("episodes") / f"{report.episode_id}.json")
                managed.add(Path("episodes") / f"{report.episode_id}.md")
        except (ValueError, OSError):
            pass

    output_root = output_dir.resolve()
    for relative_path in managed:
        if not _is_managed_relative_path(relative_path):
            continue
        target = (output_root / relative_path).resolve()
        if target.is_relative_to(output_root):
            target.unlink(missing_ok=True)
    manifest_path.unlink(missing_ok=True)


def write_run_manifest(
    output_dir: Path,
    *,
    reports: list[EpisodeReport],
    trace_path: Path,
    has_errors: bool,
) -> Path:
    managed_files = [
        str(trace_path.resolve().relative_to(output_dir.resolve())),
    ]
    if reports:
        managed_files.extend(["results.json", "schema.json"])
        for report in reports:
            managed_files.extend(
                [
                    f"episodes/{report.episode_id}.json",
                    f"episodes/{report.episode_id}.md",
                ]
            )
    if has_errors:
        managed_files.append("errors.json")
    path = output_dir / RUN_MANIFEST
    write_json(
        path,
        {
            "run_id": trace_path.stem,
            "managed_files": managed_files,
        },
    )
    return path


def _markdown_text(value: str) -> str:
    """Render untrusted content as inert inline Markdown."""

    collapsed = " ".join(value.split())
    escaped = collapsed.replace("\\", "\\\\")
    return re.sub(r"([`*_{}\[\]<>|])", r"\\\1", escaped)


def _inline_code(value: str) -> str:
    return " ".join(value.split()).replace("`", "'")


def render_episode_markdown(report: EpisodeReport) -> str:
    lines = [
        f"# {_markdown_text(report.title)}",
        "",
        f"- **Episode:** `{report.episode_id}`",
        f"- **Host:** {_markdown_text(report.host)}",
        (
            f"- **Guests:** {_markdown_text(', '.join(report.guests))}"
            if report.guests
            else "- **Guests:** None listed"
        ),
        (
            f"- **Provider/model:** `{_inline_code(report.provider)}` / "
            f"`{_inline_code(report.model)}`"
        ),
        f"- **Generated:** {_markdown_text(report.generated_at)}",
        "",
        "## Summary",
        "",
        _markdown_text(report.summary),
        "",
        "## Top five takeaways",
        "",
    ]
    lines.extend(
        f"{index}. {_markdown_text(item)}"
        for index, item in enumerate(report.notes.takeaways, 1)
    )
    lines.extend(["", "## Notable quotes", ""])
    for quote in report.notes.notable_quotes:
        lines.extend(
            [
                f"> “{_markdown_text(quote.quote)}”",
                f"> — {_markdown_text(quote.speaker)}, `{quote.timestamp}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Topics",
            "",
            " ".join(f"`{topic}`" for topic in report.notes.topics),
            "",
            "## Fact checks",
            "",
            "| Time | Claim | Verdict | Verdict confidence | Human review |",
            "|---|---|---|---:|:---:|",
        ]
    )
    if not report.fact_checks:
        lines.extend(
            [
                "",
                "_No material claims were identified for external fact-checking._",
            ]
        )
    for item in report.fact_checks:
        lines.append(
            f"| `{item.timestamp}` | {_markdown_text(item.claim)} | "
            f"{item.verdict.value} | "
            f"{item.confidence:.0%} | {'Yes' if item.needs_human_review else 'No'} |"
        )
    for index, item in enumerate(report.fact_checks, start=1):
        lines.extend(
            [
                "",
                f"### Check {index}: {_markdown_text(item.claim)}",
                "",
                (
                    f"**Source excerpt:** “{_markdown_text(item.source_excerpt)}” "
                    f"— {_markdown_text(item.speaker)}, `{item.timestamp}`"
                ),
                "",
                f"**Verification:** {_markdown_text(item.verification)}",
                "",
            ]
        )
        if item.evidence:
            lines.append("**Evidence:**")
            lines.append("")
            for evidence in item.evidence:
                publisher = (
                    f" — {_markdown_text(evidence.publisher)}"
                    if evidence.publisher
                    else ""
                )
                published = (
                    f" Published {_markdown_text(evidence.published_at)}."
                    if evidence.published_at
                    else ""
                )
                lines.append(
                    f"- [{_markdown_text(evidence.title)}](<{evidence.url}>)"
                    f"{publisher}. {_markdown_text(evidence.evidence_note)}"
                    f"{published} Retrieved {_markdown_text(evidence.retrieved_at)}."
                )
        else:
            lines.append(
                "**Evidence:** No sufficiently attributable source was available."
            )
    lines.extend(["", "## Bounded execution plan", ""])
    for step in report.execution_plan.steps:
        dependencies = (
            f" (after {', '.join(step.depends_on)})" if step.depends_on else ""
        )
        lines.append(
            f"- **{step.id}: {_markdown_text(step.action)}**{dependencies} — "
            f"{_markdown_text(step.purpose)}"
        )
    if report.validation_warnings:
        lines.extend(["", "## Validation warnings", ""])
        lines.extend(
            f"- {_markdown_text(warning)}" for warning in report.validation_warnings
        )
    lines.append("")
    return "\n".join(lines)
