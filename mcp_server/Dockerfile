# One image for every source; configure with env vars (see server.py docstring).
# Build from the repository root:  docker build -f mcp_server/Dockerfile -t fpa-mcp-source .
FROM python:3.12-slim
ARG DB_DRIVER_PIP=""
WORKDIR /app
COPY mcp_server/requirements.txt /app/mcp_server/requirements.txt
RUN pip install --no-cache-dir -r /app/mcp_server/requirements.txt ${DB_DRIVER_PIP}
COPY fpa_agent/__init__.py /app/fpa_agent/__init__.py
COPY fpa_agent/query /app/fpa_agent/query
COPY fpa_agent/connectors /app/fpa_agent/connectors
COPY fpa_agent/semantic/__init__.py fpa_agent/semantic/models.py /app/fpa_agent/semantic/
COPY mcp_server /app/mcp_server
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "mcp_server/server.py"]
