"""
Proxmox MCP server — FastMCP-based replacement for proxmoxmcp-plus.

All tools follow a uniform response envelope:
  {"success": bool, "data": Any, "error": str | None}

Start with:
  python server.py
  OR via the Docker CMD.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from services.proxmox_service import (
    AuthError,
    ConnectionError as PxConnectionError,
    NotFoundError,
    ProxmoxError,
    ProxmoxService,
)
from settings import load_settings

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("proxmox_mcp")

# ---------------------------------------------------------------------------
# Application bootstrap
# ---------------------------------------------------------------------------

settings = load_settings()
service = ProxmoxService(settings)

mcp = FastMCP(
    name="proxmox-mcp",
    instructions=(
        "Proxmox VE cluster management for a multi-node homelab. "
        "Nodes: homeserver (192.168.0.100), workload-node (192.168.0.95), officeserver (192.168.0.98). "
        "Workflow: "
        "1. Call proxmox_health() first to verify connectivity to all nodes. "
        "2. Call proxmox_list_nodes() to see node status and resource usage. "
        "3. Call proxmox_list_vms(node='homeserver') or proxmox_list_lxc(node='homeserver') to find VMs/containers. "
        "4. Use proxmox_find_vm(vmid=1001) to locate a VM across all nodes without knowing which node it's on. "
        "5. Start/stop/shutdown operations return a task UPID — always poll proxmox_task_status(node, upid) to confirm completion. "
        "SAFETY: Use proxmox_vm_shutdown() for graceful stops. "
        "proxmox_vm_stop() is a DESTRUCTIVE force-kill — use only when the guest is unresponsive. "
        "vmtype must be 'qemu' for KVM VMs (default) or 'lxc' for containers."
    ),
)

# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _ok(data: Any) -> dict:
    return {"success": True, "data": data, "error": None}


def _err(message: str) -> dict:
    return {"success": False, "data": None, "error": message}


def _wrap(fn):
    """Call *fn()* and wrap the result; translate ProxmoxErrors into _err responses."""
    try:
        return _ok(fn())
    except AuthError as exc:
        logger.error("Auth error: %s", exc)
        return _err(str(exc))
    except NotFoundError as exc:
        logger.warning("Not found: %s", exc)
        return _err(str(exc))
    except PxConnectionError as exc:
        logger.error("Connection error: %s", exc)
        return _err(str(exc))
    except ProxmoxError as exc:
        logger.error("Proxmox error: %s", exc)
        return _err(str(exc))
    except ValueError as exc:
        logger.warning("Validation error: %s", exc)
        return _err(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in tool call")
        return _err(f"Internal error: {type(exc).__name__}")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    description="Check cluster health, quorum, and HA status. Call this first to verify the cluster is healthy before making changes.",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def proxmox_cluster_status(host_name: Optional[str] = None) -> dict:
    return _wrap(lambda: service.cluster_status(host_name))


@mcp.tool(
    description="List all Proxmox nodes with CPU%, memory%, and uptime. Use this to find which nodes are online before listing VMs on a specific node.",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def proxmox_list_nodes(host_name: Optional[str] = None) -> dict:
    return _wrap(lambda: service.list_nodes(host_name))


@mcp.tool(
    description="Get detailed CPU, memory, swap, and disk stats for one node, e.g. node='homeserver'. Use proxmox_list_nodes() to find valid node names.",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def proxmox_node_status(
    node: str = None,
    host_name: Optional[str] = None,
) -> dict:
    return _wrap(lambda: service.node_status(node, host_name))


@mcp.tool(
    description="List all KVM/QEMU VMs on a node with status, CPU%, memory, and uptime. node must be a name from proxmox_list_nodes(), e.g. 'homeserver'.",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def proxmox_list_vms(
    node: str = None,
    host_name: Optional[str] = None,
) -> dict:
    return _wrap(lambda: service.list_vms(node, host_name))


@mcp.tool(
    description="List all LXC containers on a node with status, CPU%, memory, and uptime. node must be a name from proxmox_list_nodes(), e.g. 'homeserver'.",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def proxmox_list_lxc(
    node: str = None,
    host_name: Optional[str] = None,
) -> dict:
    return _wrap(lambda: service.list_lxc(node, host_name))


@mcp.tool(
    description=(
        "Get full status and config for a VM or LXC container by numeric VMID (e.g. 1001). "
        "If node is omitted, all online nodes are searched automatically — use proxmox_find_vm() instead for a cleaner cluster-wide search."
    ),
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def proxmox_vm_status(
    vmid: int = None,
    node: Optional[str] = None,
    host_name: Optional[str] = None,
) -> dict:
    return _wrap(lambda: service.vm_status(vmid, node, host_name))


@mcp.tool(
    description=(
        "Find a VM or LXC container by VMID across all nodes in the cluster. "
        "Use this instead of proxmox_vm_status() when you don't know which node the VM is on. "
        "Returns node name, status, and basic stats."
    ),
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def proxmox_find_vm(
    vmid: int = None,
    host_name: Optional[str] = None,
) -> dict:
    """Search all nodes for a VM/LXC by VMID."""
    return _wrap(lambda: service.vm_status(vmid, node=None, host_name=host_name))


@mcp.tool(
    description=(
        "Start a stopped VM or LXC container. "
        "Returns a task UPID — poll proxmox_task_status(node, upid) to confirm the VM is running. "
        "vmtype: 'qemu' for KVM VMs (default), 'lxc' for containers."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
def proxmox_vm_start(
    node: str = None,
    vmid: int = None,
    vmtype: str = "qemu",
    host_name: Optional[str] = None,
) -> dict:
    result = _wrap(lambda: service.vm_start(node, vmid, vmtype, host_name))
    if result.get("success") and result.get("data"):
        upid = result["data"].get("task_id") or str(result["data"])
        result["next_steps"] = (
            f"VM {vmid} start task submitted. "
            f"Poll: proxmox_task_status(node='{node}', taskid='{upid}')"
        )
    return result


@mcp.tool(
    description=(
        "Send an ACPI shutdown signal (graceful) to a VM or LXC. The guest OS handles the shutdown cleanly. "
        "Returns a task UPID — poll proxmox_task_status() to confirm it stopped. "
        "Prefer this over proxmox_vm_stop() unless the guest is unresponsive."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
def proxmox_vm_shutdown(
    node: str = None,
    vmid: int = None,
    vmtype: str = "qemu",
    host_name: Optional[str] = None,
) -> dict:
    result = _wrap(lambda: service.vm_shutdown(node, vmid, vmtype, host_name))
    if result.get("success") and result.get("data"):
        upid = result["data"].get("task_id") or str(result["data"])
        result["next_steps"] = (
            f"VM {vmid} shutdown task submitted. "
            f"Poll: proxmox_task_status(node='{node}', taskid='{upid}')"
        )
    return result


@mcp.tool(
    description=(
        "DESTRUCTIVE: Immediately force-kill a VM or LXC — equivalent to pulling the power cord. "
        "May cause data loss or filesystem corruption in the guest. "
        "Only use when proxmox_vm_shutdown() fails to stop the guest. "
        "Returns a task UPID to poll."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
)
def proxmox_vm_stop(
    node: str = None,
    vmid: int = None,
    vmtype: str = "qemu",
    host_name: Optional[str] = None,
) -> dict:
    result = _wrap(lambda: service.vm_stop(node, vmid, vmtype, host_name))
    if result.get("success") and result.get("data"):
        upid = result["data"].get("task_id") or str(result["data"])
        result["next_steps"] = (
            f"VM {vmid} force-stop task submitted. "
            f"Poll: proxmox_task_status(node='{node}', taskid='{upid}')"
        )
    return result


@mcp.tool(
    description=(
        "Poll an async Proxmox task by its UPID (returned by start/stop/shutdown tools). "
        "Returns 'running' or 'stopped' status, exit code, and log lines. "
        "Keep polling until status is 'stopped'."
    ),
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def proxmox_task_status(
    node: str = None,
    taskid: str = None,
    host_name: Optional[str] = None,
) -> dict:
    return _wrap(lambda: service.task_status(node, taskid, host_name))


@mcp.tool(
    description="List storage pools on a node with type, used/total/free GB, and usage%. Use after provisioning to verify disk usage.",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def proxmox_list_storage(
    node: str = None,
    host_name: Optional[str] = None,
) -> dict:
    return _wrap(lambda: service.list_storage(node, host_name))


@mcp.tool(
    description="Check connectivity and auth to all configured Proxmox hosts. Call this first when troubleshooting connection or auth errors.",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def proxmox_health() -> dict:
    return _wrap(lambda: service.health_check())


@mcp.resource("proxmox://nodes")
def proxmox_nodes_resource() -> str:
    """Browse Proxmox cluster nodes as a resource."""
    result = service.list_nodes()
    nodes = result if isinstance(result, list) else result.get("nodes", [])
    lines = ["Proxmox cluster nodes:", ""]
    for n in nodes:
        name = n.get("node", "?")
        status = n.get("status", "?")
        cpu = n.get("cpu_pct", "?")
        mem = n.get("mem_pct", "?")
        lines.append(f"  {name}: {status} | CPU {cpu}% | Mem {mem}%")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "sse":
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8080"))
        logger.info("Starting Proxmox MCP server (SSE) on %s:%d", host, port)
        mcp.run(transport="sse", host=host, port=port)
    else:
        logger.info("Starting Proxmox MCP server (stdio)")
        mcp.run(transport="stdio")
