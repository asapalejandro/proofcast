from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from proofcast.models import EpisodeTranscript

SUPPORTED_SUFFIXES = {".json", ".txt"}
MAX_TRANSCRIPT_BYTES = 1_000_000
TURN_RE = re.compile(
    r"^\[(?P<timestamp>\d{2}:\d{2}(?::\d{2})?)\]\s*"
    r"(?P<label>[^:]+):\s*(?P<text>.+)$"
)
SPEAKER_SECTION_RE = re.compile(r"^(?P<speaker>.*?)(?:\s*\((?P<section>[^()]*)\))?$")
META_KEYS = {
    "episode id": "episode_id",
    "episode_id": "episode_id",
    "title": "title",
    "host": "host",
    "guests": "guests",
}


class TranscriptParseError(ValueError):
    pass


def discover_inputs(paths: list[Path]) -> list[Path]:
    """Resolve files and directories into a stable, de-duplicated input list."""

    discovered: list[Path] = []
    for path in paths:
        candidate = path.expanduser()
        if candidate.is_file():
            if candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
                raise TranscriptParseError(
                    f"Unsupported input type for {candidate}; expected .json or .txt"
                )
            discovered.append(candidate.resolve())
            continue
        if candidate.is_dir():
            discovered.extend(
                item.resolve()
                for item in sorted(candidate.iterdir())
                if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES
            )
            continue
        raise TranscriptParseError(f"Input does not exist: {candidate}")

    unique: list[Path] = []
    seen: set[Path] = set()
    for item in discovered:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    if not unique:
        raise TranscriptParseError("No .json or .txt transcript files were found")
    return unique


def load_transcript(path: Path) -> EpisodeTranscript:
    try:
        if path.suffix.lower() == ".json":
            payload = _load_json(path)
        elif path.suffix.lower() == ".txt":
            payload = _load_txt(path)
        else:
            raise TranscriptParseError(f"Unsupported input type: {path.suffix}")
        payload["source_file"] = path.name
        return EpisodeTranscript.model_validate(payload)
    except TranscriptParseError:
        raise
    except ValidationError as exc:
        safe_issues = []
        for issue in exc.errors(include_input=False, include_url=False):
            location = ".".join(str(part) for part in issue.get("loc", ()))
            message = str(issue.get("msg", "validation error"))
            safe_issues.append(f"{location}: {message}" if location else message)
        raise TranscriptParseError(
            f"Transcript schema validation failed for {path.name}: "
            + "; ".join(safe_issues)
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise TranscriptParseError(f"Could not read {path}: {exc}") from exc


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_read_source_text(path))
    except json.JSONDecodeError as exc:
        raise TranscriptParseError(
            f"Invalid JSON in {path.name} at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(payload, dict):
        raise TranscriptParseError(f"{path.name} must contain one JSON object")
    return payload


def _load_txt(path: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    turns: list[dict[str, str]] = []

    for line_number, raw_line in enumerate(
        _read_source_text(path).splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.casefold() == "transcript:":
            continue

        turn_match = TURN_RE.match(line)
        if turn_match:
            label = turn_match.group("label").strip()
            label_match = SPEAKER_SECTION_RE.fullmatch(label)
            if label_match is None:
                raise TranscriptParseError(
                    f"Invalid speaker label on line {line_number} of {path.name}"
                )
            turn = {
                "timestamp": turn_match.group("timestamp"),
                "speaker": label_match.group("speaker").strip(),
                "text": turn_match.group("text").strip(),
            }
            section = label_match.group("section")
            if section:
                turn["section"] = section.strip()
            turns.append(turn)
            continue

        if turns:
            turns[-1]["text"] = f"{turns[-1]['text']} {line}"
            continue

        if ":" not in line:
            raise TranscriptParseError(
                f"Expected metadata or a timestamped turn on line {line_number} "
                f"of {path.name}"
            )
        raw_key, value = line.split(":", 1)
        key = META_KEYS.get(raw_key.strip().casefold())
        if key is None:
            continue
        cleaned = value.strip()
        if key == "guests":
            metadata[key] = [
                guest.strip() for guest in cleaned.split(",") if guest.strip()
            ]
        else:
            metadata[key] = cleaned

    if not turns:
        raise TranscriptParseError(
            f"No timestamped transcript turns found in {path.name}"
        )

    speakers = list(dict.fromkeys(turn["speaker"] for turn in turns))
    episode_id = metadata.get("episode_id") or EpisodeTranscript.source_stem(path)
    title = (
        metadata.get("title") or path.stem.replace("_", " ").replace("-", " ").title()
    )
    host = metadata.get("host") or speakers[0]
    guests = metadata.get("guests")
    if guests is None:
        guests = [speaker for speaker in speakers if speaker != host]

    return {
        "episode_id": episode_id,
        "title": title,
        "host": host,
        "guests": guests,
        "transcript": turns,
    }


def _read_source_text(path: Path) -> str:
    """Read one UTF-8 transcript with a hard memory and provider-cost ceiling."""

    with path.open("rb") as handle:
        payload = handle.read(MAX_TRANSCRIPT_BYTES + 1)
    if len(payload) > MAX_TRANSCRIPT_BYTES:
        raise TranscriptParseError(
            f"{path.name} exceeds the {MAX_TRANSCRIPT_BYTES:,}-byte input limit"
        )
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TranscriptParseError(f"{path.name} must be valid UTF-8") from exc
