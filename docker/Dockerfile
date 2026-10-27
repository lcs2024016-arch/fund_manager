# Non-root, minimal, pinned. No secrets baked in.
FROM python:3.12-slim@sha256:0f6f0e13b0112b1d68a29df5cf3e5d3f7b60c1ec0e79c8e4b0e2f7cb6db1b914

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN adduser --disabled-password --uid 10001 bot
WORKDIR /app

COPY pyproject.toml ./
COPY src/ src/
COPY config/ config/
COPY scripts/ scripts/
RUN pip install --no-cache-dir . && pip install --no-cache-dir httpx websockets pydantic pydantic-settings PyYAML

USER bot
# Note: paper mode is the only mode this container starts by default.
# TESTNET/prod requires an operator command AND --i-understand-this-is-live.
ENTRYPOINT ["python", "-m", "main"]
CMD ["--strategy", "credit_spread", "--mode", "paper"]
