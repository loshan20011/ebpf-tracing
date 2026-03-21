.DEFAULT_GOAL := help

REPO_PREFIX ?= loshans
TAG ?= v1
APP_NS ?= sock-shop
CONTROL_NS ?= thrive-scale
WORKLOADS_FILE ?= deploy/02-demo-apps/workloads.yaml
SLO_FILE ?= deploy/02-demo-apps/my-slos.yaml
SOCKSHOP_SLO_FILE ?= deploy/03-evaluation/sockshop-slos.calibrated.yaml
SOCKSHOP_REPO ?= https://github.com/ocp-power-demos/sock-shop-demo.git
SOCKSHOP_DIR ?= $(HOME)/sock-shop-demo
RENDER_DIR ?= /tmp/thrivescale-rendered-$(CONTROL_NS)
KUBECTL ?= kubectl
PYTHON ?= python3
K3S_IMPORT ?= sudo k3s ctr images import -

WORKLOAD_DIRS := $(wildcard src/test-workloads/*)
WORKLOAD_IMAGES := $(notdir $(WORKLOAD_DIRS))

.PHONY: help build build-system build-workloads load load-system load-workloads
.PHONY: deploy deploy-all deploy-system deploy-apps deploy-slos deploy-thrivescale deploy-demo-workloads
.PHONY: deploy-sockshop-demo deploy-sockshop-slos deploy-sockshop-stack
.PHONY: create-ns create-control-ns create-app-ns render-system-manifests
.PHONY: validate validate-runtime status status-thrivescale status-sockshop
.PHONY: logs-controller logs-aggregator logs-agent logs-frontend
.PHONY: traffic stop-traffic compare-demo compare-worldcup compare-sockshop-scenarios profile-sockshop-routes clean clean-images wipe-k3s-demo

help:
	@printf "%s\n" \
	  "Targets:" \
	  "  make build-system           Build ThriveScale control-plane images" \
	  "  make load-system            Import ThriveScale control-plane images into K3s" \
	  "  make deploy-thrivescale     Deploy ThriveScale into $(CONTROL_NS)" \
	  "  make deploy-demo-workloads  Deploy built-in synthetic workloads and SLOs" \
	  "  make deploy-sockshop-demo   Clone/patch/deploy Sock Shop demo into $(APP_NS)" \
	  "  make deploy-sockshop-slos   Apply calibrated Sock Shop ServiceSLOs" \
	  "  make deploy-sockshop-stack  Deploy ThriveScale, Sock Shop, and Sock Shop SLOs" \
	  "  make compare-worldcup       Run a World Cup-style Sock Shop ThriveScale vs HPA comparison" \
	  "  make compare-sockshop-scenarios Run the 3 thesis Sock Shop scenarios for HPA and ThriveScale" \
	  "  make profile-sockshop-routes Identify which Sock Shop routes show the strongest CPU-bound signals" \
	  "  make wipe-k3s-demo          Remove ThriveScale and app namespaces plus ThriveScale cluster RBAC/CRD" \
	  "  make status                 Show ThriveScale and app status" \
	  "  make validate               Run local validation checks"

build: build-system build-workloads
	@echo "Built all ThriveScale images locally."

build-system:
	docker build --no-cache -t $(REPO_PREFIX)/bpf-agent:$(TAG) src/agent
	docker build --no-cache -t $(REPO_PREFIX)/aggregator:$(TAG) src/aggregator
	docker build --no-cache -t $(REPO_PREFIX)/controller:$(TAG) src/controller
	docker build --no-cache -t $(REPO_PREFIX)/frontend:$(TAG) src/frontend

build-workloads:
	@for svc in $(WORKLOAD_DIRS); do \
		name=$$(basename $$svc); \
		echo "Building $$name..."; \
		docker build --no-cache -t $(REPO_PREFIX)/$$name:$(TAG) $$svc; \
	done

load: load-system load-workloads
	@echo "Imported all images into K3s."

load-system:
	docker save $(REPO_PREFIX)/bpf-agent:$(TAG) | $(K3S_IMPORT)
	docker save $(REPO_PREFIX)/aggregator:$(TAG) | $(K3S_IMPORT)
	docker save $(REPO_PREFIX)/controller:$(TAG) | $(K3S_IMPORT)
	docker save $(REPO_PREFIX)/frontend:$(TAG) | $(K3S_IMPORT)

load-workloads:
	@for name in $(WORKLOAD_IMAGES); do \
		echo "Loading $$name..."; \
		docker save $(REPO_PREFIX)/$$name:$(TAG) | $(K3S_IMPORT); \
	done

create-ns: create-control-ns create-app-ns

create-control-ns:
	@$(KUBECTL) get namespace $(CONTROL_NS) >/dev/null 2>&1 || $(KUBECTL) create namespace $(CONTROL_NS)

create-app-ns:
	@$(KUBECTL) get namespace $(APP_NS) >/dev/null 2>&1 || $(KUBECTL) create namespace $(APP_NS)

render-system-manifests:
	@rm -rf $(RENDER_DIR)
	@mkdir -p $(RENDER_DIR)/00-setup $(RENDER_DIR)/01-system
	@cp deploy/00-setup/crd-definition.yaml $(RENDER_DIR)/00-setup/crd-definition.yaml
	@sed -e "s/namespace: default/namespace: $(CONTROL_NS)/g" deploy/00-setup/rbac.yaml > $(RENDER_DIR)/00-setup/rbac.yaml
	@for f in deploy/01-system/*.yaml; do \
		sed \
		  -e 's/namespace: default/namespace: $(CONTROL_NS)/g' \
		  -e 's/value: "default"/value: "$(CONTROL_NS)"/g' \
		  -e 's/value: "sock-shop"/value: "$(APP_NS)"/g' \
		  $$f > $(RENDER_DIR)/01-system/$$(basename $$f); \
	done

deploy: deploy-all

deploy-all: deploy-system deploy-apps deploy-slos
	@echo "Deployment finished for control=$(CONTROL_NS) app=$(APP_NS)."

deploy-system: create-control-ns render-system-manifests
	$(KUBECTL) apply -f $(RENDER_DIR)/00-setup/
	$(KUBECTL) apply -n $(CONTROL_NS) -f $(RENDER_DIR)/01-system/
	@$(KUBECTL) rollout status -n $(CONTROL_NS) deploy/aggregator --timeout=120s
	@$(KUBECTL) rollout status -n $(CONTROL_NS) deploy/custom-autoscaler --timeout=120s
	@$(KUBECTL) rollout status -n $(CONTROL_NS) deploy/frontend --timeout=120s
	@$(KUBECTL) rollout status -n $(CONTROL_NS) deploy/redis --timeout=120s
	@$(KUBECTL) rollout status -n $(CONTROL_NS) daemonset/bpf-agent --timeout=120s

deploy-apps: create-app-ns
	$(KUBECTL) apply -n $(APP_NS) -f $(WORKLOADS_FILE)

deploy-slos: create-app-ns
	$(KUBECTL) apply -n $(APP_NS) -f $(SLO_FILE)

deploy-thrivescale: deploy-system

deploy-demo-workloads: deploy-apps deploy-slos

deploy-sockshop-demo: create-app-ns
	APP_NS="$(APP_NS)" SOCKSHOP_DIR="$(SOCKSHOP_DIR)" SOCKSHOP_REPO="$(SOCKSHOP_REPO)" KUBECTL="$(KUBECTL)" bash scripts/deploy_sock_shop_demo.sh

deploy-sockshop-slos: create-app-ns
	$(KUBECTL) apply -n $(APP_NS) -f $(SOCKSHOP_SLO_FILE)

deploy-sockshop-stack: deploy-thrivescale deploy-sockshop-demo deploy-sockshop-slos
	@echo "Sock Shop and ThriveScale are deployed."

traffic:
	$(KUBECTL) apply -n $(APP_NS) -f deploy/02-demo-apps/traffic-generator.yaml

stop-traffic:
	$(KUBECTL) delete -n $(APP_NS) -f deploy/02-demo-apps/traffic-generator.yaml --ignore-not-found

compare-demo:
	bash scripts/run_demo_compare.sh

compare-worldcup:
	bash scripts/run_worldcup_compare.sh

compare-sockshop-scenarios:
	bash scripts/run_sockshop_three_scenarios.sh

profile-sockshop-routes:
	$(PYTHON) scripts/profile_sockshop_routes.py --freeze-thrivescale --reset-aggregator

validate:
	$(PYTHON) -m py_compile src/agent/agent.py src/aggregator/aggregator.py src/controller/controller.py
	$(PYTHON) -m unittest tests.test_repo_layout

validate-runtime:
	$(KUBECTL) get pods -A
	$(KUBECTL) get services -n $(CONTROL_NS)
	@echo "Use logs-controller, logs-aggregator, and logs-agent for deeper inspection."

status:
	$(KUBECTL) get ns
	$(MAKE) status-thrivescale
	$(MAKE) status-sockshop

status-thrivescale:
	$(KUBECTL) get pods -n $(CONTROL_NS) -o wide
	$(KUBECTL) get svc -n $(CONTROL_NS)

status-sockshop:
	$(KUBECTL) get deploy -n $(APP_NS)
	$(KUBECTL) get pods -n $(APP_NS) -o wide

logs-controller:
	$(KUBECTL) logs -n $(CONTROL_NS) deploy/custom-autoscaler --tail=200

logs-aggregator:
	$(KUBECTL) logs -n $(CONTROL_NS) deploy/aggregator --tail=200

logs-agent:
	$(KUBECTL) logs -n $(CONTROL_NS) daemonset/bpf-agent --tail=200

logs-frontend:
	$(KUBECTL) logs -n $(CONTROL_NS) deploy/frontend --tail=200

clean:
	-$(KUBECTL) delete -n $(APP_NS) -f $(SLO_FILE) --ignore-not-found
	-$(KUBECTL) delete -n $(APP_NS) -f $(WORKLOADS_FILE) --ignore-not-found
	-if [ -d "$(RENDER_DIR)/01-system" ]; then $(KUBECTL) delete -n $(CONTROL_NS) -f $(RENDER_DIR)/01-system --ignore-not-found; fi
	-if [ -f "$(RENDER_DIR)/00-setup/rbac.yaml" ]; then $(KUBECTL) delete -f $(RENDER_DIR)/00-setup/rbac.yaml --ignore-not-found; fi
	-rm -rf $(RENDER_DIR)

clean-images:
	-docker rmi $$(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^$(REPO_PREFIX)/')

wipe-k3s-demo:
	APP_NS="$(APP_NS)" CONTROL_NS="$(CONTROL_NS)" KUBECTL="$(KUBECTL)" bash scripts/wipe_k3s_demo.sh
