"""
Proxmox service layer.

Wraps proxmoxer with:
  - Lazy, cached clients per host (thread-safe)
  - Unified error classification (auth, not-found, connection, general)
  - Human-readable formatting helpers (bytes → GB, uptime seconds → Xd Xh Xm)
  - Audit logging for every destructive operation
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

import requests
from proxmoxer import ProxmoxAPI
from proxmoxer.core import ResourceException

from settings import HostConfig, Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _bytes_to_gb(value: Any) -> float:
    """Convert bytes (int/float) to GB, rounded to two decimal places."""
    try:
        return round(int(value) / (1024**3), 2)
    except (TypeError, ValueError):
        return 0.0


def _bytes_to_mb(value: Any) -> float:
    try:
        return round(int(value) / (1024**2), 2)
    except (TypeError, ValueError):
        return 0.0


def _uptime_str(seconds: Any) -> str:
    """Convert seconds to a human-readable uptime string: Xd Xh Xm."""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "unknown"

    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "0m"


def _pct(used: Any, total: Any) -> float:
    """Return used/total as a percentage rounded to one decimal place."""
    try:
        u, t = int(used), int(total)
        return round(u / t * 100, 1) if t else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


class ProxmoxError(Exception):
    """Base error for all Proxmox service failures."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class AuthError(ProxmoxError):
    """401 / 403 — token missing, expired, or insufficient permissions."""


class NotFoundError(ProxmoxError):
    """404 — resource does not exist on the node."""


class ConnectionError(ProxmoxError):  # noqa: A001
    """Network / TLS / DNS failure reaching the Proxmox API."""


def _classify(exc: Exception, host_name: str) -> ProxmoxError:
    """Map low-level exceptions to typed ProxmoxError subclasses."""
    if isinstance(exc, ResourceException):
        code = getattr(exc, "status_code", None)
        # Sanitise the message — it could contain the request URL with token info.
        safe_msg = f"Proxmox API error on '{host_name}' (HTTP {code})"
        if code in (401, 403):
            return AuthError(
                f"{safe_msg}: authentication or permission denied.", code
            )
        if code == 404:
            return NotFoundError(f"{safe_msg}: resource not found.", code)
        return ProxmoxError(safe_msg, code)

    if isinstance(exc, requests.exceptions.ConnectionError):
        return ConnectionError(
            f"Cannot connect to Proxmox host '{host_name}': {type(exc).__name__}"
        )
    if isinstance(exc, requests.exceptions.Timeout):
        return ConnectionError(f"Timeout connecting to Proxmox host '{host_name}'.")
    if isinstance(exc, requests.exceptions.SSLError):
        return ConnectionError(
            f"TLS/SSL error connecting to '{host_name}'. "
            "Check verify_ssl setting or certificate configuration."
        )

    return ProxmoxError(
        f"Unexpected error from '{host_name}': {type(exc).__name__}"
    )


# ---------------------------------------------------------------------------
# ProxmoxService
# ---------------------------------------------------------------------------


class ProxmoxService:
    """
    Provides all Proxmox operations used by the MCP tools.

    Clients are created lazily the first time a host is accessed and then
    reused across tool calls.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[str, ProxmoxAPI] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Client management
    # ------------------------------------------------------------------

    def _get_client(self, host_name: Optional[str] = None) -> tuple[str, ProxmoxAPI]:
        """Return (resolved_host_name, proxmoxer_client), creating if needed."""
        cfg = self._settings.get_host(host_name)

        with self._lock:
            if cfg.name not in self._clients:
                logger.debug("Creating proxmoxer client for host '%s'", cfg.name)
                self._clients[cfg.name] = self._create_client(cfg)

        return cfg.name, self._clients[cfg.name]

    @staticmethod
    def _create_client(cfg: HostConfig) -> ProxmoxAPI:
        return ProxmoxAPI(
            cfg.host,
            port=cfg.port,
            user=cfg.user,
            token_name=cfg.token_name,
            token_value=cfg.token_value,
            verify_ssl=cfg.verify_ssl,
            service=cfg.service,
        )

    def _invalidate_client(self, host_name: str) -> None:
        """Remove a cached client so it will be recreated on next call."""
        with self._lock:
            self._clients.pop(host_name, None)

    def all_host_names(self) -> list[str]:
        return [h.name for h in self._settings.hosts]

    # ------------------------------------------------------------------
    # Internal call wrapper
    # ------------------------------------------------------------------

    def _call(self, host_name: Optional[str], fn):
        """Execute *fn(client)* against a resolved host, translating exceptions."""
        resolved, client = self._get_client(host_name)
        try:
            return fn(client)
        except (ResourceException, requests.exceptions.RequestException) as exc:
            raise _classify(exc, resolved) from exc

    # ------------------------------------------------------------------
    # Cluster & node
    # ------------------------------------------------------------------

    def cluster_status(self, host_name: Optional[str] = None) -> dict:
        """Return cluster health, quorum, and HA status."""
        data = self._call(host_name, lambda px: px.cluster.status.get())

        cluster_info: dict[str, Any] = {}
        nodes: list[dict] = []

        for item in data:
            if item.get("type") == "cluster":
                cluster_info = {
                    "name": item.get("name"),
                    "quorum": item.get("quorum"),
                    "nodes": item.get("nodes"),
                    "version": item.get("version"),
                }
            elif item.get("type") == "node":
                nodes.append(
                    {
                        "name": item.get("name"),
                        "online": bool(item.get("online")),
                        "local": bool(item.get("local")),
                        "level": item.get("level"),
                        "nodeid": item.get("nodeid"),
                    }
                )

        # HA status — optional, not all clusters have HA configured
        ha_status: Optional[dict] = None
        try:
            ha_raw = self._call(host_name, lambda px: px.cluster.ha.status.current.get())
            ha_status = {"resources": ha_raw} if ha_raw else {"resources": []}
        except ProxmoxError:
            ha_status = None

        return {
            "cluster": cluster_info,
            "nodes": nodes,
            "ha_status": ha_status,
        }

    def list_nodes(self, host_name: Optional[str] = None) -> list[dict]:
        """Return all nodes with status, CPU%, memory%, uptime."""
        raw = self._call(host_name, lambda px: px.nodes.get())
        result = []
        for n in raw:
            max_mem = int(n.get("maxmem") or 0)
            mem = int(n.get("mem") or 0)
            max_cpu = n.get("maxcpu", 1)
            result.append(
                {
                    "node": n.get("node"),
                    "status": n.get("status"),
                    "cpu_pct": round(float(n.get("cpu", 0)) * 100, 1),
                    "cpu_cores": max_cpu,
                    "mem_used_gb": _bytes_to_gb(mem),
                    "mem_total_gb": _bytes_to_gb(max_mem),
                    "mem_pct": _pct(mem, max_mem),
                    "uptime": _uptime_str(n.get("uptime")),
                    "uptime_seconds": n.get("uptime"),
                    "disk_used_gb": _bytes_to_gb(n.get("disk", 0)),
                    "disk_total_gb": _bytes_to_gb(n.get("maxdisk", 0)),
                    "level": n.get("level"),
                    "id": n.get("id"),
                }
            )
        return sorted(result, key=lambda x: x["node"])

    def node_status(self, node: str, host_name: Optional[str] = None) -> dict:
        """Return detailed resource information for a single node."""
        raw = self._call(host_name, lambda px: px.nodes(node).status.get())
        cpu = raw.get("cpu", {})
        mem = raw.get("memory", {})
        swap = raw.get("swap", {})
        disk = raw.get("rootfs", {})

        return {
            "node": node,
            "uptime": _uptime_str(raw.get("uptime")),
            "uptime_seconds": raw.get("uptime"),
            "cpu": {
                "usage_pct": round(float(raw.get("cpu", 0)) * 100, 1),
                "cores": raw.get("cpuinfo", {}).get("cores"),
                "sockets": raw.get("cpuinfo", {}).get("sockets"),
                "model": raw.get("cpuinfo", {}).get("model"),
                "mhz": raw.get("cpuinfo", {}).get("mhz"),
            },
            "memory": {
                "used_gb": _bytes_to_gb(mem.get("used")),
                "total_gb": _bytes_to_gb(mem.get("total")),
                "free_gb": _bytes_to_gb(mem.get("free")),
                "used_pct": _pct(mem.get("used", 0), mem.get("total", 1)),
            },
            "swap": {
                "used_gb": _bytes_to_gb(swap.get("used")),
                "total_gb": _bytes_to_gb(swap.get("total")),
                "free_gb": _bytes_to_gb(swap.get("free")),
                "used_pct": _pct(swap.get("used", 0), swap.get("total", 1)),
            },
            "rootfs": {
                "used_gb": _bytes_to_gb(disk.get("used")),
                "total_gb": _bytes_to_gb(disk.get("total")),
                "free_gb": _bytes_to_gb(disk.get("free")),
                "used_pct": _pct(disk.get("used", 0), disk.get("total", 1)),
            },
            "kernel": raw.get("kversion"),
            "pve_version": raw.get("pveversion"),
            "load_average": raw.get("loadavg"),
        }

    # ------------------------------------------------------------------
    # VM / LXC listing
    # ------------------------------------------------------------------

    def list_vms(self, node: str, host_name: Optional[str] = None) -> list[dict]:
        """Return all QEMU VMs on the given node."""
        raw = self._call(host_name, lambda px: px.nodes(node).qemu.get())
        return [_format_vm_summary(v) for v in raw]

    def list_lxc(self, node: str, host_name: Optional[str] = None) -> list[dict]:
        """Return all LXC containers on the given node."""
        raw = self._call(host_name, lambda px: px.nodes(node).lxc.get())
        return [_format_vm_summary(v) for v in raw]

    # ------------------------------------------------------------------
    # VM / LXC detail
    # ------------------------------------------------------------------

    def vm_status(
        self,
        vmid: int,
        node: Optional[str] = None,
        host_name: Optional[str] = None,
    ) -> dict:
        """
        Return detailed status + config for a VM or LXC.

        If *node* is None, searches all nodes on the host for the VMID.
        Tries qemu first, then lxc.
        """
        if node:
            return self._vm_status_on_node(node, vmid, host_name)

        # Search all nodes
        nodes = self.list_nodes(host_name)
        for n in nodes:
            if n["status"] != "online":
                continue
            node_name = n["node"]
            try:
                return self._vm_status_on_node(node_name, vmid, host_name)
            except NotFoundError:
                continue
            except ProxmoxError:
                continue

        raise NotFoundError(f"VMID {vmid} not found on any node.")

    def _vm_status_on_node(
        self, node: str, vmid: int, host_name: Optional[str]
    ) -> dict:
        """Try qemu then lxc on a specific node."""
        for vmtype in ("qemu", "lxc"):
            try:
                return self._fetch_vm_detail(node, vmid, vmtype, host_name)
            except NotFoundError:
                continue
        raise NotFoundError(f"VMID {vmid} not found on node '{node}'.")

    def _fetch_vm_detail(
        self, node: str, vmid: int, vmtype: str, host_name: Optional[str]
    ) -> dict:
        resolved, client = self._get_client(host_name)
        try:
            endpoint = getattr(client.nodes(node), vmtype)(vmid)
            status_raw = endpoint.status.current.get()
            config_raw = endpoint.config.get()
        except ResourceException as exc:
            raise _classify(exc, resolved) from exc
        except requests.exceptions.RequestException as exc:
            raise _classify(exc, resolved) from exc

        mem_total = int(status_raw.get("maxmem") or config_raw.get("memory", 0) * 1024 * 1024)
        mem_used = int(status_raw.get("mem") or 0)
        disk_total = int(status_raw.get("maxdisk") or 0)
        disk_used = int(status_raw.get("disk") or 0)

        return {
            "vmid": vmid,
            "node": node,
            "vmtype": vmtype,
            "name": status_raw.get("name") or config_raw.get("name", f"vm-{vmid}"),
            "status": status_raw.get("status"),
            "cpu_pct": round(float(status_raw.get("cpu", 0)) * 100, 2),
            "cpu_cores": config_raw.get("cores") or config_raw.get("cpus"),
            "memory": {
                "used_mb": _bytes_to_mb(mem_used),
                "total_mb": _bytes_to_mb(mem_total),
                "used_pct": _pct(mem_used, mem_total),
            },
            "disk": {
                "used_gb": _bytes_to_gb(disk_used),
                "total_gb": _bytes_to_gb(disk_total),
                "used_pct": _pct(disk_used, disk_total),
            },
            "uptime": _uptime_str(status_raw.get("uptime")),
            "uptime_seconds": status_raw.get("uptime"),
            "netin_mb": _bytes_to_mb(status_raw.get("netin", 0)),
            "netout_mb": _bytes_to_mb(status_raw.get("netout", 0)),
            "pid": status_raw.get("pid"),
            "ha_state": status_raw.get("ha", {}).get("state") if status_raw.get("ha") else None,
            "config": _sanitise_config(config_raw),
        }

    # ------------------------------------------------------------------
    # Start / stop / shutdown
    # ------------------------------------------------------------------

    def vm_start(
        self,
        node: str,
        vmid: int,
        vmtype: str = "qemu",
        host_name: Optional[str] = None,
    ) -> dict:
        logger.info("ACTION start  node=%s vmid=%s vmtype=%s", node, vmid, vmtype)
        task_id = self._vm_action(node, vmid, vmtype, "start", host_name)
        return {"node": node, "vmid": vmid, "vmtype": vmtype, "task_id": task_id}

    def vm_shutdown(
        self,
        node: str,
        vmid: int,
        vmtype: str = "qemu",
        host_name: Optional[str] = None,
    ) -> dict:
        logger.info("ACTION shutdown  node=%s vmid=%s vmtype=%s", node, vmid, vmtype)
        task_id = self._vm_action(node, vmid, vmtype, "shutdown", host_name)
        return {"node": node, "vmid": vmid, "vmtype": vmtype, "task_id": task_id}

    def vm_stop(
        self,
        node: str,
        vmid: int,
        vmtype: str = "qemu",
        host_name: Optional[str] = None,
    ) -> dict:
        logger.warning(
            "DESTRUCTIVE ACTION stop  node=%s vmid=%s vmtype=%s", node, vmid, vmtype
        )
        task_id = self._vm_action(node, vmid, vmtype, "stop", host_name)
        return {"node": node, "vmid": vmid, "vmtype": vmtype, "task_id": task_id}

    def _vm_action(
        self,
        node: str,
        vmid: int,
        vmtype: str,
        action: str,
        host_name: Optional[str],
    ) -> str:
        _validate_vmtype(vmtype)
        resolved, client = self._get_client(host_name)
        try:
            endpoint = getattr(client.nodes(node), vmtype)(vmid).status
            task_id: str = getattr(endpoint, action).post()
            return task_id
        except (ResourceException, requests.exceptions.RequestException) as exc:
            raise _classify(exc, resolved) from exc

    # ------------------------------------------------------------------
    # Task status
    # ------------------------------------------------------------------

    def task_status(
        self, node: str, taskid: str, host_name: Optional[str] = None
    ) -> dict:
        resolved, client = self._get_client(host_name)
        try:
            status_raw = client.nodes(node).tasks(taskid).status.get()
            log_lines: list[str] = []
            if status_raw.get("status") == "stopped":
                try:
                    log_raw = client.nodes(node).tasks(taskid).log.get(limit=20)
                    log_lines = [entry.get("t", "") for entry in log_raw]
                except Exception:
                    pass
            return {
                "taskid": taskid,
                "node": node,
                "status": status_raw.get("status"),
                "exit_status": status_raw.get("exitstatus"),
                "type": status_raw.get("type"),
                "user": status_raw.get("user"),
                "start_time": status_raw.get("starttime"),
                "end_time": status_raw.get("endtime"),
                "log": log_lines,
            }
        except (ResourceException, requests.exceptions.RequestException) as exc:
            raise _classify(exc, resolved) from exc

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def list_storage(
        self, node: str, host_name: Optional[str] = None
    ) -> list[dict]:
        raw = self._call(host_name, lambda px: px.nodes(node).storage.get())
        result = []
        for s in raw:
            total = int(s.get("total") or 0)
            used = int(s.get("used") or 0)
            avail = int(s.get("avail") or 0)
            result.append(
                {
                    "storage": s.get("storage"),
                    "type": s.get("type"),
                    "active": bool(s.get("active")),
                    "enabled": bool(s.get("enabled")),
                    "shared": bool(s.get("shared")),
                    "content": s.get("content"),
                    "used_gb": _bytes_to_gb(used),
                    "total_gb": _bytes_to_gb(total),
                    "avail_gb": _bytes_to_gb(avail),
                    "used_pct": _pct(used, total),
                }
            )
        return result

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self) -> list[dict]:
        """Probe every configured host and report connectivity status."""
        results = []
        for host_cfg in self._settings.hosts:
            entry: dict[str, Any] = {
                "host": host_cfg.name,
                "address": host_cfg.host,
                "port": host_cfg.port,
                "reachable": False,
                "node_count": None,
                "error": None,
            }
            try:
                _, client = self._get_client(host_cfg.name)
                nodes = client.nodes.get()
                entry["reachable"] = True
                entry["node_count"] = len(nodes)
                entry["nodes"] = [n.get("node") for n in nodes]
            except ProxmoxError as exc:
                entry["error"] = str(exc)
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: connection failed"
            results.append(entry)
        return results


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _format_vm_summary(raw: dict) -> dict:
    mem = int(raw.get("mem") or 0)
    max_mem = int(raw.get("maxmem") or 0)
    return {
        "vmid": raw.get("vmid"),
        "name": raw.get("name"),
        "status": raw.get("status"),
        "cpu_pct": round(float(raw.get("cpu", 0)) * 100, 1),
        "mem_used_mb": _bytes_to_mb(mem),
        "mem_total_mb": _bytes_to_mb(max_mem),
        "mem_pct": _pct(mem, max_mem),
        "uptime": _uptime_str(raw.get("uptime")),
        "uptime_seconds": raw.get("uptime"),
        "disk_gb": _bytes_to_gb(raw.get("disk", 0)),
        "pid": raw.get("pid"),
        "tags": raw.get("tags"),
    }


def _sanitise_config(config: dict) -> dict:
    """Return the config dict, stripping keys that might contain auth material."""
    sensitive = {"password", "cipassword", "sshkeys"}
    return {k: v for k, v in config.items() if k.lower() not in sensitive}


def _validate_vmtype(vmtype: str) -> None:
    if vmtype not in ("qemu", "lxc"):
        raise ValueError(
            f"Invalid vmtype '{vmtype}'. Must be 'qemu' or 'lxc'."
        )
