# Safe EC2 Workflow

This project uses a conservative EC2 workflow to avoid destabilizing the remote SSH service.

## Default Rule

- Do not use `scp`.
- Do not use streamed file copies over `ssh`.
- Do not use long chained `ssh` commands.
- Do not keep remote sessions open with `sleep`, long polls, or combined build-and-verify commands.

## Standard Operating Pattern

1. Prepare and validate changes locally first.
2. Apply code or manifest updates directly on the EC2 host in a local terminal session on the box.
3. Use only short remote checks from outside the box:
   - `ssh ... "echo ok"`
   - `ssh ... "kubectl get ..."`
   - `ssh ... "kubectl logs ... --tail ..."`
   - `ssh ... "kubectl rollout restart ..."`
4. Stop remote automation immediately if SSH fails once.

## Allowed Remote Command Shapes

- One connectivity check
- One read-only Kubernetes query
- One rollout command
- One short log command

Each command should do one thing only.

## Disallowed Remote Command Shapes

- `scp ...`
- `cat ... | ssh ...`
- `ssh ... "sleep 90; ..."`
- `ssh ... "build && import && rollout && logs"`
- wide multi-file copy attempts

## Build And Deploy Split

If a remote rebuild is needed, split it into separate local-on-EC2 steps:

1. Build image
2. Import image into K3s
3. Restart deployment
4. Inspect pods
5. Inspect logs

## Reason For This Policy

The EC2 SSH service for this project has shown instability during longer or transfer-heavy remote sessions. This workflow favors reliability over convenience.
