FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1
RUN pip install --no-cache-dir uv==0.10.10 && useradd --uid 10001 --create-home foundry
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable && mkdir /data && chown foundry:foundry /data
ENV PATH="/app/.venv/bin:$PATH" FOUNDRY_DB=/data/foundry.db
USER foundry
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4)"
ENTRYPOINT ["foundry"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765"]
