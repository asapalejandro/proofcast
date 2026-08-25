# Public-safe offline demonstration

This demonstration is wholly synthetic. Its people, show, episode, and dialogue
were created for this repository and do not reproduce, summarize, or transform
private or third-party source material.

Run the same production parser, orchestrator, validators, renderers, and trace
writer without credentials or network access:

```bash
proofcast run examples/public_demo_transcript.json \
  --provider fixture \
  --fixture fixtures/public_demo_reports.json \
  --output outputs/public-demo
```

The equivalent container path mounts the synthetic input and fixture read-only,
uses no network, and mounts only the dedicated output directory read/write:

```bash
docker build -t proofcast:local .
mkdir -p outputs/public-demo

docker run --rm \
  --network none \
  --read-only \
  --tmpfs /tmp \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD/examples/public_demo_transcript.json,target=/inputs/public_demo_transcript.json,readonly" \
  --mount "type=bind,source=$PWD/fixtures/public_demo_reports.json,target=/fixtures/public_demo_reports.json,readonly" \
  --mount "type=bind,source=$PWD/outputs/public-demo,target=/outputs" \
  proofcast:local run \
  /inputs/public_demo_transcript.json \
  --provider fixture \
  --fixture /fixtures/public_demo_reports.json \
  --output /outputs
```

The command writes canonical JSON, reviewer-friendly Markdown, JSON Schema, and
a structured JSONL execution trace. The fixture is deterministic so a reviewer
can reproduce the same editorial and fact-check content; generated timestamps
and trace identifiers will differ between runs.

## Why this example exists

The synthetic episode deliberately exercises all three verdicts:

- a present claim supported by primary documentation;
- an overstated security claim that evidence does not support; and
- a future prediction that cannot yet be verified.

It also includes a 200–300 word summary, exactly five takeaways, exact
timestamped source quotes, slug-style topic tags, atomic claims, and a bounded
dependency-aware plan.

`confidence` means confidence that the assigned verdict is appropriate for the
available evidence; it is not a probability that the underlying claim is true.
Each normalized claim also retains a verbatim `source_excerpt`, allowing
deterministic validation of transcript provenance before retrieval.

## Source limitation

Fixture mode does not retrieve the linked evidence and must not be presented as
a live web fact-check. The evidence links are a reviewed snapshot captured for
the deterministic demonstration. A production or current-facts run should use a
live retrieval provider, record retrieval time, and treat unavailable, stale, or
conflicting sources conservatively. The command-line option implemented by this
project is `--fixture` (not `--fixture-path`).
