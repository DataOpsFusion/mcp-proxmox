FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer cache-friendly)
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application source
COPY . .

# The server reads config from /app/proxmox-config/config.yaml by default.
# Mount the config directory as a volume at runtime.
# Tokens must be supplied via PROXMOX_TOKEN_VALUE environment variable.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PROXMOX_CONFIG_PATH=/app/proxmox-config/config.yaml \
    MCP_TRANSPORT=stdio

CMD ["python", "server.py"]
