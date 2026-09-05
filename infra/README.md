# infra/ — reference cloud deployment

**Nothing here is deployed.** This folder is real, validated Terraform
(`terraform validate` passes, azurerm ~> 5.4) showing how MISO Copilot would
run in production — for the presentation and for future work.

![Reference deployment](../docs/terraform-architecture.svg)

(Also as a [PNG](../docs/terraform-architecture.png).)

## What maps to what

| Local (today) | Production (this file) |
|---|---|
| `uvicorn` on a laptop | `api` pods on AKS, N replicas behind a load balancer |
| the poller inside the API process | its own single-replica workload — splitting it out is what lets the api scale |
| in-memory answer cache + rate limiter | Redis — shared across all api replicas |
| embedded Chroma reading local files | Chroma in client/server mode, one replica on a persistent volume |
| nothing in front of the API | App Gateway with WAF: load balancing, OWASP rules, >60 req/min per IP blocked at the edge |
| `data/` folder | Azure Files share (snapshots, request log, Chroma volume) |
| `.env` with the API key | Key Vault secret, read via workload identity |
| `/tmp/miso-backend.log` | Log Analytics, **forwarded to SIEM** (workspace onboarded) |
| — | ACR for container images, built by CI |
| — | MCP server as its own deployment, so other AI assistants can automate |

## File layout

```
versions.tf        # terraform + provider pins
variables.tf       # prefix, region, API key (sensitive)
main.tf            # resource group
network.tf         # vnet, subnets, public IP
edge.tf            # WAF policy (firewall + per-IP rate limit) + App Gateway
cluster.tf         # AKS + the four workloads it runs
registry.tf        # ACR + the cluster's pull permission
storage.tf         # Redis (cache + rate-limit state) + Azure Files
secrets.tf         # Key Vault + workload identity
observability.tf   # Log Analytics, forwarded to SIEM
frontend.tf        # Static Web App
outputs.tf         # public entrypoint + frontend hostname
```

## Why AKS and not plain VMs

Autoscaling (2–5 nodes), rolling deploys, and one scheduler for four
distinct workloads (`api`, `poller`, `mcp`, `chroma`) beats hand-managing
machines. The k8s manifests for those workloads would live in `k8s/` — this
file is the platform underneath them.

## The rules that carry over from the laptop

- **One poller, ever.** The rate guard means exactly one thing talks to
  MISO, at most once per link per minute — same rule as `AGENTS.md`, now
  enforced by a single-replica deployment instead of a single laptop.
- **Answers still say how fresh they are**, and the WAF is defense at the
  edge while the app's own per-IP limiter stays as depth.

## If someone ever did apply this

```bash
cd infra
export TF_VAR_claude_api_key=sk-ant-...   # never commit
terraform init
terraform plan
```

Still missing before it could really run: the k8s manifests, a CI job
building images into ACR, and a TLS certificate on the gateway — all
deliberately out of scope for a reference sketch.
