"""
Settings loader for Proxmox MCP server.

Host definitions are read from YAML. Secrets (token_value) are injected
from environment variables so the config file never needs to contain credentials.

Environment variables:
  PROXMOX_CONFIG_PATH   Path to config YAML  (default: /app/proxmox-config/config.yaml)
  PROXMOX_TOKEN_VALUE   Overrides token_value for every host
  PROXMOX_DEFAULT_HOST  Which host name to treat as the primary (default: homeserver)
  PROXMOX_VERIFY_SSL    "true"/"false" overrides per-host verify_ssl when set
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class HostConfig:
    name: str
    host: str
    port: int
    user: str
    token_name: str
    token_value: str  # populated from env, not YAML
    verify_ssl: bool
    service: str = "PVE"


@dataclass
class Settings:
    hosts: list[HostConfig] = field(default_factory=list)
    default_host: str = "homeserver"

    # Convenience dict keyed by host name
    @property
    def hosts_by_name(self) -> dict[str, HostConfig]:
        return {h.name: h for h in self.hosts}

    def get_host(self, name: Optional[str] = None) -> HostConfig:
        target = name or self.default_host
        hosts = self.hosts_by_name
        if target not in hosts:
            available = ", ".join(hosts.keys())
            raise ValueError(
                f"Host '{target}' not found in config. Available: {available}"
            )
        return hosts[target]


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_settings() -> Settings:
    config_path = Path(
        os.environ.get("PROXMOX_CONFIG_PATH", "/app/proxmox-config/config.yaml")
    )

    if not config_path.exists():
        raise FileNotFoundError(
            f"Proxmox config file not found: {config_path}. "
            "Set PROXMOX_CONFIG_PATH to point to a valid config.yaml."
        )

    with config_path.open() as fh:
        raw = yaml.safe_load(fh)

    token_value_override = os.environ.get("PROXMOX_TOKEN_VALUE")
    verify_ssl_override_raw = os.environ.get("PROXMOX_VERIFY_SSL")
    verify_ssl_override: Optional[bool] = (
        _parse_bool(verify_ssl_override_raw)
        if verify_ssl_override_raw is not None
        else None
    )

    hosts: list[HostConfig] = []
    for entry in raw.get("hosts", []):
        auth = entry.get("auth", {})

        # Determine token value: env var wins, then YAML (but we warn if falling
        # back to YAML so operators know the secret came from the config file).
        token_value = token_value_override
        if not token_value:
            yaml_token = auth.get("token_value", "")
            if yaml_token:
                logger.warning(
                    "Host '%s': token_value loaded from YAML config. "
                    "Set PROXMOX_TOKEN_VALUE env var to avoid storing secrets on disk.",
                    entry["name"],
                )
            token_value = yaml_token

        if not token_value:
            raise ValueError(
                f"Host '{entry['name']}': no token_value found. "
                "Set PROXMOX_TOKEN_VALUE environment variable."
            )

        verify_ssl = (
            verify_ssl_override
            if verify_ssl_override is not None
            else bool(entry.get("verify_ssl", False))
        )

        hosts.append(
            HostConfig(
                name=entry["name"],
                host=entry["host"],
                port=int(entry.get("port", 8006)),
                user=auth.get("user", "mcp@pam"),
                token_name=auth.get("token_name", "mcp"),
                token_value=token_value,
                verify_ssl=verify_ssl,
                service=entry.get("service", "PVE"),
            )
        )

    default_host = os.environ.get("PROXMOX_DEFAULT_HOST", "homeserver")

    settings = Settings(hosts=hosts, default_host=default_host)
    logger.info(
        "Loaded %d host(s): %s  (default: %s)",
        len(hosts),
        ", ".join(h.name for h in hosts),
        default_host,
    )
    return settings
