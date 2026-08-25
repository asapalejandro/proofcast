from __future__ import annotations

import builtins
import os
import sys
import unittest
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

from proofcast.models import (
    ClaimCandidate,
    ClaimExtraction,
    ClaimType,
    EpisodeTranscript,
    ExecutionPlan,
    TranscriptTurn,
    Verdict,
)
from proofcast.providers.openai import (
    EvidenceSearchResult,
    EvidenceSearchSource,
    OpenAIProvider,
    OpenAIProviderConfigurationError,
    OpenAIResponseError,
)

ResponseFactory = Callable[[dict[str, Any]], Any]


class FakeResponses:
    def __init__(
        self,
        *,
        parse_factory: ResponseFactory | None = None,
        create_factory: ResponseFactory | None = None,
    ) -> None:
        self.parse_factory = parse_factory
        self.create_factory = create_factory
        self.parse_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.parse_calls.append(kwargs)
        if self.parse_factory is None:
            raise AssertionError("responses.parse must not be called")
        return self.parse_factory(kwargs)

    def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        if self.create_factory is None:
            raise AssertionError("responses.create must not be called")
        return self.create_factory(kwargs)


def _client(responses: FakeResponses) -> SimpleNamespace:
    return SimpleNamespace(responses=responses)


def _transcript() -> EpisodeTranscript:
    return EpisodeTranscript(
        episode_id="ep-openai-test",
        title="Provider Test",
        host="Ada",
        transcript=[
            TranscriptTurn(
                timestamp="00:10",
                speaker="Ada",
                text="The factual claim and opinion were stated here.",
            )
        ],
    )


def _claim(
    *,
    claim_type: ClaimType = ClaimType.FACT,
    checkable: bool = True,
    text: str = "The factual claim is accurate.",
) -> ClaimCandidate:
    return ClaimCandidate(
        claim=text,
        timestamp="00:10",
        speaker="Ada",
        source_excerpt="The factual claim and opinion were stated here.",
        claim_type=claim_type,
        checkable=checkable,
        search_query="authoritative evidence for the factual claim"
        if checkable
        else None,
    )


def _usage(*, input_tokens: int = 10, output_tokens: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _plan_payload() -> dict[str, Any]:
    names = [
        "normalize",
        "editorial",
        "claims",
        "search",
        "adjudicate",
        "assemble",
        "validate",
        "persist",
    ]
    return {
        "objective": "Create an evidence-first podcast report.",
        "steps": [
            {
                "id": name,
                "action": name.replace("-", " ").title(),
                "purpose": f"Complete the {name} stage.",
                "depends_on": [] if index == 0 else [names[index - 1]],
            }
            for index, name in enumerate(names)
        ],
    }


class OpenAIProviderConfigurationTests(unittest.TestCase):
    def test_openai_sdk_import_is_lazy_and_missing_sdk_error_is_actionable(
        self,
    ) -> None:
        provider = OpenAIProvider()
        original_import = builtins.__import__

        def block_openai_import(
            name: str,
            globals: dict[str, Any] | None = None,
            locals: dict[str, Any] | None = None,
            fromlist: tuple[str, ...] = (),
            level: int = 0,
        ) -> Any:
            if name == "openai":
                raise ImportError("blocked for deterministic test")
            return original_import(name, globals, locals, fromlist, level)

        # Construction succeeds because the SDK is imported only on first use.
        self.assertEqual(provider.model, "gpt-5.6-sol")
        with (
            patch("builtins.__import__", side_effect=block_openai_import),
            self.assertRaisesRegex(
                OpenAIProviderConfigurationError,
                r"official Python SDK.*pip install -e",
            ),
        ):
            provider.create_plan(_transcript())

    def test_missing_api_key_error_does_not_leak_environment_changes(self) -> None:
        fake_openai = ModuleType("openai")

        class MustNotConstruct:
            def __init__(self, **kwargs: Any) -> None:
                raise AssertionError("client must not be built without an API key")

        fake_openai.OpenAI = MustNotConstruct  # type: ignore[attr-defined]
        original_api_key = os.environ.get("OPENAI_API_KEY")

        with (
            patch.dict(sys.modules, {"openai": fake_openai}),
            patch.dict(os.environ, {"OPENAI_API_KEY": ""}),
            self.assertRaisesRegex(
                OpenAIProviderConfigurationError,
                r"No OpenAI API key.*OPENAI_API_KEY",
            ),
        ):
            OpenAIProvider().create_plan(_transcript())

        self.assertEqual(os.environ.get("OPENAI_API_KEY"), original_api_key)


class OpenAIProviderResponseTests(unittest.TestCase):
    def test_structured_parse_success_and_usage_capture(self) -> None:
        def parse_plan(kwargs: dict[str, Any]) -> SimpleNamespace:
            schema = kwargs["text_format"]
            return SimpleNamespace(
                id="resp_plan_1",
                status="completed",
                error=None,
                incomplete_details=None,
                output=[],
                output_parsed=schema.model_validate(_plan_payload()),
                usage=_usage(input_tokens=120, output_tokens=40),
            )

        responses = FakeResponses(parse_factory=parse_plan)
        provider = OpenAIProvider(
            client=_client(responses),
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )

        plan, usage = provider.create_plan(_transcript())

        self.assertIsInstance(plan, ExecutionPlan)
        self.assertEqual(len(plan.steps), 8)
        self.assertEqual(usage.input_tokens, 120)
        self.assertEqual(usage.output_tokens, 40)
        self.assertEqual(usage.total_tokens, 160)
        self.assertEqual(usage.response_id, "resp_plan_1")
        self.assertEqual(provider.usage_history, (usage,))

        request = responses.parse_calls[0]
        self.assertEqual(request["model"], "gpt-5.6-sol")
        self.assertEqual(request["reasoning"], {"effort": "high"})
        self.assertIs(request["text_format"], ExecutionPlan)
        self.assertIs(request["store"], False)
        self.assertNotIn("OPENAI_API_KEY", request)

    def test_incomplete_response_fails_after_capturing_usage(self) -> None:
        def incomplete(_: dict[str, Any]) -> SimpleNamespace:
            return SimpleNamespace(
                id="resp_incomplete_1",
                status="incomplete",
                error=None,
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                output=[],
                output_parsed=None,
                usage=_usage(input_tokens=30, output_tokens=20),
            )

        provider = OpenAIProvider(
            client=_client(FakeResponses(parse_factory=incomplete))
        )

        with self.assertRaisesRegex(
            OpenAIResponseError,
            r"status=incomplete, reason=max_output_tokens",
        ):
            provider.create_plan(_transcript())

        self.assertEqual(len(provider.usage_history), 1)
        self.assertEqual(provider.usage_history[0].total_tokens, 50)

    def test_refusal_is_detected_without_echoing_refusal_content(self) -> None:
        refusal_text = "private refusal content must not be logged"

        def refused(_: dict[str, Any]) -> SimpleNamespace:
            return SimpleNamespace(
                id="resp_refused_1",
                status="completed",
                error=None,
                incomplete_details=None,
                output=[
                    SimpleNamespace(
                        type="message",
                        content=[SimpleNamespace(type="refusal", refusal=refusal_text)],
                    )
                ],
                output_parsed=None,
                usage=_usage(),
            )

        provider = OpenAIProvider(client=_client(FakeResponses(parse_factory=refused)))

        with self.assertRaises(OpenAIResponseError) as raised:
            provider.create_plan(_transcript())

        self.assertIn("refused", str(raised.exception))
        self.assertNotIn(refusal_text, str(raised.exception))

    def test_search_extracts_cited_and_consulted_sources_safely(self) -> None:
        def search(_: dict[str, Any]) -> SimpleNamespace:
            return SimpleNamespace(
                id="resp_search_1",
                status="completed",
                error=None,
                incomplete_details=None,
                output_text="The primary source corroborates the claim.",
                usage=_usage(input_tokens=80, output_tokens=25),
                output=[
                    SimpleNamespace(
                        type="web_search_call",
                        action=SimpleNamespace(
                            sources=[
                                SimpleNamespace(
                                    url="https://www.example.com/cited",
                                    title="Consulted duplicate title",
                                ),
                                SimpleNamespace(
                                    url="https://other.example/source",
                                    title="Other consulted source",
                                ),
                                SimpleNamespace(
                                    url="javascript:alert(1)", title="Script URL"
                                ),
                                SimpleNamespace(
                                    url="file:///etc/passwd", title="File URL"
                                ),
                                SimpleNamespace(
                                    url="https://user:password@example.com/private",
                                    title="Credential URL",
                                ),
                                SimpleNamespace(
                                    url="https://example.com:invalid/port",
                                    title="Invalid port",
                                ),
                            ]
                        ),
                    ),
                    SimpleNamespace(
                        type="message",
                        content=[
                            SimpleNamespace(
                                type="output_text",
                                annotations=[
                                    SimpleNamespace(
                                        type="url_citation",
                                        url="https://www.example.com/cited",
                                        title="Cited primary source",
                                        start_index=0,
                                        end_index=10,
                                    )
                                ],
                            )
                        ],
                    ),
                ],
            )

        responses = FakeResponses(create_factory=search)
        provider = OpenAIProvider(client=_client(responses))

        result, usage = provider.search_evidence(_claim())

        self.assertEqual(
            [(source.source_id, source.url, source.cited) for source in result.sources],
            [
                ("S1", "https://www.example.com/cited", True),
                ("S2", "https://other.example/source", False),
            ],
        )
        self.assertEqual(result.sources[0].title, "Cited primary source")
        self.assertEqual(result.sources[0].publisher, "example.com")
        self.assertEqual(usage.total_tokens, 105)

        request = responses.create_calls[0]
        self.assertEqual(request["tools"], [{"type": "web_search"}])
        self.assertEqual(request["tool_choice"], "required")
        self.assertEqual(request["include"], ["web_search_call.action.sources"])
        self.assertIs(request["store"], False)

    def test_prediction_and_opinion_are_unverifiable_without_search(self) -> None:
        responses = FakeResponses()
        provider = OpenAIProvider(client=_client(responses))
        extraction = ClaimExtraction(
            claims=[
                _claim(
                    claim_type=ClaimType.PREDICTION,
                    checkable=True,
                    text="This technology will replace all clinicians.",
                ),
                _claim(
                    claim_type=ClaimType.OPINION,
                    checkable=False,
                    text="Founders should always bootstrap.",
                ),
            ]
        )

        batch, usage = provider.verify_claims(_transcript(), extraction)

        self.assertEqual(usage, [])
        self.assertEqual(responses.create_calls, [])
        self.assertEqual(responses.parse_calls, [])
        self.assertEqual(
            [fact_check.verdict for fact_check in batch.fact_checks],
            [Verdict.UNVERIFIABLE, Verdict.UNVERIFIABLE],
        )
        self.assertTrue(
            all(fact_check.needs_human_review for fact_check in batch.fact_checks)
        )
        self.assertTrue(
            all(not fact_check.evidence for fact_check in batch.fact_checks)
        )

    def test_verified_adjudication_without_valid_cited_source_is_downgraded(
        self,
    ) -> None:
        def adjudicate(kwargs: dict[str, Any]) -> SimpleNamespace:
            schema = kwargs["text_format"]
            parsed = schema.model_validate(
                {
                    "verdict": "Verified true",
                    "confidence": 0.99,
                    "verification": "The source appears to support the claim.",
                    "used_sources": [
                        {
                            "source_id": "S1",
                            "evidence_note": "This source was only consulted.",
                        }
                    ],
                    "needs_human_review": False,
                }
            )
            return SimpleNamespace(
                id="resp_adjudication_1",
                status="completed",
                error=None,
                incomplete_details=None,
                output=[
                    SimpleNamespace(
                        type="message",
                        content=[SimpleNamespace(type="output_text")],
                    )
                ],
                output_parsed=parsed,
                usage=_usage(input_tokens=90, output_tokens=30),
            )

        provider = OpenAIProvider(
            client=_client(FakeResponses(parse_factory=adjudicate))
        )
        search_result = EvidenceSearchResult(
            query="authoritative evidence",
            synthesis="A result was found, but it was not cited in the synthesis.",
            sources=[
                EvidenceSearchSource(
                    source_id="S1",
                    title="Consulted source",
                    url="https://example.com/consulted",
                    publisher="example.com",
                    cited=False,
                )
            ],
        )

        fact_check, usage = provider.adjudicate_claim(_claim(), search_result)

        self.assertEqual(fact_check.verdict, Verdict.UNVERIFIABLE)
        self.assertGreaterEqual(fact_check.confidence, 0.85)
        self.assertTrue(fact_check.needs_human_review)
        self.assertIn("downgraded", fact_check.verification)
        self.assertEqual(fact_check.evidence, [])
        self.assertEqual(usage.response_id, "resp_adjudication_1")


if __name__ == "__main__":
    unittest.main()
