Public ThriveScale host setup

- Host nginx listens on `80` for `thrivescale.thethrive360.net` and `www.thrivescale.thethrive360.net`.
- The public landing page is served from a plain Docker `nginx:alpine` container on `127.0.0.1:8080`.
- `/dashboard` proxies to the in-cluster frontend NodePort `127.0.0.1:30616`.
- `/api/` proxies to the in-cluster aggregator NodePort `127.0.0.1:30938`.

Important

- The cluster services must stay as `NodePort`, not `LoadBalancer`, or K3s `svclb` will reclaim host port `80`.
- AWS security-group inbound rules must allow `80/tcp` and `443/tcp` to the instance, or the public domain will time out even when nginx is healthy on the server.

Files

- `thrive-public-nginx.conf`: host nginx vhost
- `frontend-container-nginx.conf`: static container nginx config
- `patch-traefik-clusterip.sh`: helper for moving Traefik off external host ports
