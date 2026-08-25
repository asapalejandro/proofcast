from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)(api[_ -]?key\s*[=:]\s*)\S+"),
)
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def _safe_text(value: object, limit: int = 500) -> str:
    text = str(value).replace("\n", " ").strip()
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:limit]


class RunTracer:
    """Content-redacted JSONL audit events plus readable console status."""

    def __init__(self, output_dir: Path, run_id: str | None = None) -> None:
        candidate_run_id = run_id or uuid.uuid4().hex[:12]
        if RUN_ID_RE.fullmatch(candidate_run_id) is None:
            raise ValueError(
                "run_id must contain only letters, numbers, underscores, or hyphens"
            )
        self.run_id = candidate_run_id
        output_root = output_dir.resolve()
        trace_dir = (output_root / "traces").resolve()
        if not trace_dir.is_relative_to(output_root):
            raise ValueError("managed traces directory escapes the output directory")
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_path = (trace_dir / f"{self.run_id}.jsonl").resolve()
        if trace_path.parent != trace_dir:
            raise ValueError("managed trace path escapes the traces directory")
        self.path = trace_path

    def emit(
        self,
        *,
        stage: str,
        action: str,
        status: str,
        episode_id: str | None = None,
        duration_ms: int | None = None,
        attempt: int = 1,
        detail: str | None = None,
        error_type: str | None = None,
    ) -> None:
        event: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "trace_id": self.run_id,
            "episode_id": episode_id,
            "stage": stage,
            "action": action,
            "status": status,
            "attempt": attempt,
            "retry_count": max(attempt - 1, 0),
        }
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        if detail:
            event["detail"] = _safe_text(detail)
        if error_type:
            event["error_type"] = _safe_text(error_type, 100)

        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

        subject = episode_id or "run"
        timing = f" ({duration_ms} ms)" if duration_ms is not None else ""
        safe_detail = f" — {_safe_text(detail, 180)}" if detail else ""
        print(f"[{self.run_id}] [{subject}] {stage}: {status}{timing}{safe_detail}")

    @contextmanager
    def span(
        self,
        *,
        stage: str,
        action: str,
        episode_id: str | None = None,
        attempt: int = 1,
        detail: str | None = None,
    ) -> Iterator[None]:
        started = time.perf_counter()
        self.emit(
            stage=stage,
            action=action,
            status="started",
            episode_id=episode_id,
            attempt=attempt,
            detail=detail,
        )
        try:
            yield
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            self.emit(
                stage=stage,
                action=action,
                status="failed",
                episode_id=episode_id,
                duration_ms=elapsed,
                attempt=attempt,
                detail=f"{type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
            )
            raise
        elapsed = int((time.perf_counter() - started) * 1000)
        self.emit(
            stage=stage,
            action=action,
            status="completed",
            episode_id=episode_id,
            duration_ms=elapsed,
            attempt=attempt,
            detail=detail,
        )
