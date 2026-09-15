FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=120 \
    HOME=/tmp

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY app/mortgage_agent.py app/mortgage_api.py ./

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && chown -R app:app /app

ENV PATH="/app/.venv/bin:${PATH}"

USER 10001:10001

EXPOSE 8080

ENTRYPOINT ["uvicorn", "mortgage_api:app"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--limit-concurrency", "4"]
