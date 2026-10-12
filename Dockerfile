FROM python:3.12-slim
RUN python -m venv /opt/poetry \
    && /opt/poetry/bin/pip install --no-cache-dir "poetry==2.3.1"
ENV POETRY_VIRTUALENVS_IN_PROJECT=true
ENV POETRY_NO_INTERACTION=1
WORKDIR /app
COPY pyproject.toml poetry.lock README.md ./
COPY packages/ packages/
COPY src/ src/
COPY compat/ compat/
COPY config/ config/
COPY alembic.ini ./
RUN /opt/poetry/bin/poetry check --lock \
    && /opt/poetry/bin/poetry sync --only main --no-root \
    && /opt/poetry/bin/poetry --directory packages/shared build --output /app/dist \
    && /opt/poetry/bin/poetry --directory packages/operations build --output /app/dist \
    && /opt/poetry/bin/poetry build --format wheel \
    && .venv/bin/python -m pip install --no-deps --force-reinstall dist/*.whl
RUN mkdir -p /app/out && chgrp -R 0 /app && chmod -R g=u /app
ENV ADMIN_OUT_DIR=/app/out
ENV PATH="/app/.venv/bin:$PATH"
USER 1001
EXPOSE 8200
ENTRYPOINT ["alerts-bi-admin"]
CMD ["serve"]
