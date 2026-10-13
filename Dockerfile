FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY config.example.yaml ./config.yaml
RUN useradd --create-home --uid 10001 gateway \
    && mkdir -p /app/data \
    && chown -R gateway:gateway /app
USER gateway
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready')"
CMD ["llm-gateway", "--config", "/app/config.yaml"]

