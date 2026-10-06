FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.10.8 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY vendor/ vendor/
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable
ENV PATH="/app/.venv/bin:$PATH"
USER 1001
EXPOSE 8200
ENTRYPOINT ["alerts-bi-admin"]
CMD ["serve"]
