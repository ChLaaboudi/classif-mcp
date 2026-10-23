FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md endpoints.toml ./
COPY src ./src
COPY data ./data
RUN pip install --no-cache-dir .

# Remote MCP over Streamable HTTP, served at /mcp on $PORT (set by the host platform)
ENV CLASSIF_MCP_TRANSPORT=http \
    CLASSIF_MCP_CONFIG=/app/endpoints.toml \
    HOST=0.0.0.0 \
    PORT=8000
EXPOSE 8000
CMD ["classif-mcp"]
