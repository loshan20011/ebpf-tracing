#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

APP_NS="${APP_NS:-sock-shop}"
CONTROL_NS="${CONTROL_NS:-thrive-scale}"
K3S_INSTALL_URL="${K3S_INSTALL_URL:-https://get.k3s.io}"
K3S_INSTALL_EXEC="${K3S_INSTALL_EXEC:-server --write-kubeconfig-mode 644}"
KUBECTL="${KUBECTL:-kubectl}"

echo "[phase] uninstall existing k3s"
if [[ -x /usr/local/bin/k3s-uninstall.sh ]]; then
  sudo /usr/local/bin/k3s-uninstall.sh || true
fi
if [[ -x /usr/local/bin/k3s-agent-uninstall.sh ]]; then
  sudo /usr/local/bin/k3s-agent-uninstall.sh || true
fi

echo "[phase] remove leftover k3s state"
sudo rm -rf /etc/rancher/k3s /var/lib/rancher/k3s /var/lib/kubelet /etc/cni/net.d || true
sudo ip link delete cni0 2>/dev/null || true
sudo ip link delete flannel.1 2>/dev/null || true

echo "[phase] install fresh k3s"
curl -sfL "$K3S_INSTALL_URL" | INSTALL_K3S_EXEC="$K3S_INSTALL_EXEC" sh -s -

echo "[phase] wait for k3s server"
sudo systemctl enable k3s >/dev/null 2>&1 || true
sudo systemctl restart k3s
sleep 10
sudo k3s kubectl get nodes

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

echo "[phase] setup paper-like environment"
bash scripts/setup_paperlike_worldcup_env.sh

echo "[phase] final verification"
$KUBECTL get nodes
$KUBECTL get pods -A
$KUBECTL get deploy -n "$APP_NS"

echo "[done] k3s reinstalled and environment rebuilt"
