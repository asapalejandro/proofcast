FROM python:3.12.16-alpine3.23@sha256:a4ad86fc2eaa4f9a5c3153776c9fb56e502dbd8e23dfa855d164308f376a6dd2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

ARG PROOFCAST_REQUIREMENTS=requirements.txt

RUN apk add --no-cache --upgrade sqlite-libs=3.53.4-r0

COPY requirements.txt requirements-openai.txt ./
RUN if [ "$PROOFCAST_REQUIREMENTS" != "requirements.txt" ] \
        && [ "$PROOFCAST_REQUIREMENTS" != "requirements-openai.txt" ]; then \
        echo "Unsupported requirements set" >&2; exit 2; \
    fi \
    && pip install --no-cache-dir --require-hashes -r "$PROOFCAST_REQUIREMENTS"

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --no-deps .

RUN python -m pip uninstall --yes pip setuptools wheel

RUN addgroup -S -g 10001 proofcast \
    && adduser -S -D -u 10001 -G proofcast -h /home/proofcast proofcast

USER proofcast

ENTRYPOINT ["proofcast"]
CMD ["--help"]
