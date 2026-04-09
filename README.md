# mcp-proxmox

Proxmox VE API for the homelab. Inspect and manage nodes, VMs, and LXC containers. All tools return a uniform `{success, data, error}` envelope.

## Tools

| Tool | Description |
|------|-------------|
| `get_nodes` | List all Proxmox nodes and their status |
| `get_vms` | List VMs across all nodes |
| `get_containers` | List LXC containers across all nodes |
| `get_vm_status` | Get detailed status of a specific VM |
| `get_node_status` | Get CPU, memory, and storage for a node |
| `start_vm` | Start a VM or container |
| `stop_vm` | Stop a VM or container |
| `get_cluster_resources` | Overview of all cluster resources |

Read operations are cached for 60 seconds.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `PROXMOX_HOST` | Proxmox API URL (e.g. `https://192.168.0.100:8006`) |
| `PROXMOX_USER` | API user (e.g. `root@pam`) |
| `PROXMOX_TOKEN_NAME` | API token name |
| `PROXMOX_TOKEN_VALUE` | API token value |
| `PROXMOX_VERIFY_SSL` | `true` or `false` |

## MCP Connection

```json
{
  "type": "http",
  "url": "http://<host>:<PORT>/mcp"
}
```

## CI/CD

Images are built on every push to `main` and pushed to:
- Harbor: `harbor.homeserverlocal.com/mcp/mcp-proxmox:latest`
- Docker Hub: `dataopsfusion/mcp-proxmox:latest`

