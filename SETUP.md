# ThriveScale Setup

This guide is the simplest deployment path for the current project.

It covers:

- cleaning the K3s cluster
- deploying ThriveScale
- deploying the external Sock Shop demo
- applying Sock Shop `ServiceSLO` objects
- checking that the system is healthy

## Prerequisites

Run these on the EC2 K3s host:

- `docker`
- `kubectl`
- `sudo k3s ctr`
- `git`
- `python3`
- `make`

The project repo should be available on the EC2 host, for example:

```bash
cd ~/new-arch-setup
```

## Recommended Clean Deployment Flow

Use this sequence for a fresh deployment:

```bash
make wipe-k3s-demo
make build-system
make load-system
make deploy-thrivescale
make deploy-sockshop-demo
make deploy-sockshop-slos
make status
```

What each step does:

- `make wipe-k3s-demo`
  - deletes `sock-shop`
  - deletes `thrive-scale`
  - removes the ThriveScale CRD and cluster-wide RBAC
- `make build-system`
  - builds `bpf-agent`, `aggregator`, `controller`, and `frontend`
- `make load-system`
  - imports those images into the local K3s runtime
- `make deploy-thrivescale`
  - deploys the ThriveScale control plane into `thrive-scale`
- `make deploy-sockshop-demo`
  - clones the external Sock Shop repo on the Linux host if needed
  - applies the K3s compatibility fixes
  - deploys the app into `sock-shop`
- `make deploy-sockshop-slos`
  - applies calibrated Sock Shop `ServiceSLO` resources
- `make status`
  - shows ThriveScale and app deployment state

## What The Sock Shop Script Handles

`scripts/deploy_sock_shop_demo.sh` automates the compatibility work we already validated:

- converts the OpenShift namespace object into a Kubernetes `Namespace`
- removes the OpenShift route from the base kustomization
- removes worker-only node selectors
- removes `runAsNonRoot: true` lines that break some images on this setup
- changes `storageClassName: nfs-client` to `local-path`
- replaces the Red Hat Redis image with `redis:7-alpine`
- rewrites the DB deployments to use K3s-safe `emptyDir` layouts
- increases memory for `shipping` and `queue-master` so they do not get `OOMKilled`

## Core Status Commands

Check everything:

```bash
make status
```

Check ThriveScale only:

```bash
make status-thrivescale
```

Check Sock Shop only:

```bash
make status-sockshop
```

Direct Kubernetes checks:

```bash
kubectl get pods -n thrive-scale -o wide
kubectl get svc -n thrive-scale
kubectl get deploy -n sock-shop
kubectl get pods -n sock-shop -o wide
kubectl get serviceslos -n sock-shop
```

## Logs

ThriveScale control-plane logs:

```bash
make logs-controller
make logs-aggregator
make logs-agent
make logs-frontend
```

If needed, inspect Sock Shop service logs directly:

```bash
kubectl logs -n sock-shop deploy/front-end --tail=100
kubectl logs -n sock-shop deploy/catalogue --tail=100
kubectl logs -n sock-shop deploy/orders --tail=100
```

## Synthetic Demo Deployment

If you want the built-in synthetic workload instead of Sock Shop:

```bash
make deploy-thrivescale
make deploy-demo-workloads
make traffic
make status
```

Stop the synthetic traffic:

```bash
make stop-traffic
```

## Validation

Run local validation from the repo:

```bash
make validate
```

Runtime validation on the cluster:

```bash
make validate-runtime
```

## Notes

- Default control-plane namespace: `thrive-scale`
- Default app namespace: `sock-shop`
- Default Sock Shop SLO file: `deploy/03-evaluation/sockshop-slos.calibrated.yaml`
- Default synthetic SLO file: `deploy/02-demo-apps/my-slos.yaml`
- The frontend is part of the standard ThriveScale deployment path
- The safe remote access policy is documented in `EC2_WORKFLOW.md`
