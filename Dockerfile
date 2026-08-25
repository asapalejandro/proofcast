FROM python:3.12.14-alpine3.23@sha256:31a768b01976652c222e318fe5bd6e7c252f056cbf489c88fa256f1bf0af58e3

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
