# Project Variables
REPO_PREFIX ?= loshans
TAG ?= latest

# Commands
DOCKER_BUILD = docker build -t
DOCKER_BUILD_FORCE = docker build --no-cache -t
DOCKER_PUSH = docker push
K3S_IMPORT = sudo k3s ctr images import -

.PHONY: all build force-build push load deploy clean clean-images traffic stop-traffic
.PHONY: agent force-agent push-agent load-agent
.PHONY: aggregator force-aggregator push-aggregator load-aggregator
.PHONY: controller force-controller push-controller load-controller
.PHONY: frontend force-frontend push-frontend load-frontend
.PHONY: workloads force-workloads push-workloads load-workloads

# ==============================================================================
# 1. BUILD TARGETS (Standard & Forced)
# ==============================================================================

# --- AGENT ---
agent:
	@echo "🚧 Building Agent (Cached)..."
	$(DOCKER_BUILD) $(REPO_PREFIX)/bpf-agent:$(TAG) src/agent

force-agent:
	@echo "☢️  Force Building Agent (No Cache)..."
	$(DOCKER_BUILD_FORCE) $(REPO_PREFIX)/bpf-agent:$(TAG) src/agent

push-agent:
	@echo "⬆️  Pushing Agent to Registry..."
	$(DOCKER_PUSH) $(REPO_PREFIX)/bpf-agent:$(TAG)

load-agent:
	@echo "📦 Loading Agent into K3s..."
	docker save $(REPO_PREFIX)/bpf-agent:$(TAG) | $(K3S_IMPORT)

# --- AGGREGATOR ---
aggregator:
	@echo "🚧 Building Aggregator (Cached)..."
	$(DOCKER_BUILD) $(REPO_PREFIX)/aggregator:$(TAG) src/aggregator

force-aggregator:
	@echo "☢️  Force Building Aggregator (No Cache)..."
	$(DOCKER_BUILD_FORCE) $(REPO_PREFIX)/aggregator:$(TAG) src/aggregator

push-aggregator:
	@echo "⬆️  Pushing Aggregator to Registry..."
	$(DOCKER_PUSH) $(REPO_PREFIX)/aggregator:$(TAG)

load-aggregator:
	@echo "📦 Loading Aggregator into K3s..."
	docker save $(REPO_PREFIX)/aggregator:$(TAG) | $(K3S_IMPORT)

# --- CONTROLLER ---
controller:
	@echo "🚧 Building Controller (Cached)..."
	$(DOCKER_BUILD) $(REPO_PREFIX)/controller:$(TAG) src/controller

force-controller:
	@echo "☢️  Force Building Controller (No Cache)..."
	$(DOCKER_BUILD_FORCE) $(REPO_PREFIX)/controller:$(TAG) src/controller

push-controller:
	@echo "⬆️  Pushing Controller to Registry..."
	$(DOCKER_PUSH) $(REPO_PREFIX)/controller:$(TAG)

load-controller:
	@echo "📦 Loading Controller into K3s..."
	docker save $(REPO_PREFIX)/controller:$(TAG) | $(K3S_IMPORT)

# --- FRONTEND ---
frontend:
	@echo "🚧 Building Frontend (Cached)..."
	$(DOCKER_BUILD) $(REPO_PREFIX)/frontend:$(TAG) src/frontend

force-frontend:
	@echo "☢️  Force Building Frontend (No Cache)..."
	$(DOCKER_BUILD_FORCE) $(REPO_PREFIX)/frontend:$(TAG) src/frontend

push-frontend:
	@echo "⬆️  Pushing Frontend to Registry..."
	$(DOCKER_PUSH) $(REPO_PREFIX)/frontend:$(TAG)

load-frontend:
	@echo "📦 Loading Frontend into K3s..."
	docker save $(REPO_PREFIX)/frontend:$(TAG) | $(K3S_IMPORT)

# --- WORKLOADS ---
workloads:
	@echo "🚧 Building Workloads (Cached)..."
	@for svc in src/test-workloads/*; do \
		if [ -d "$$svc" ]; then \
			svc_name=$$(basename $$svc); \
			$(DOCKER_BUILD) $(REPO_PREFIX)/$$svc_name:$(TAG) $$svc; \
		fi \
	done

force-workloads:
	@echo "☢️  Force Building Workloads (No Cache)..."
	@for svc in src/test-workloads/*; do \
		if [ -d "$$svc" ]; then \
			svc_name=$$(basename $$svc); \
			$(DOCKER_BUILD_FORCE) $(REPO_PREFIX)/$$svc_name:$(TAG) $$svc; \
		fi \
	done

push-workloads:
	@echo "⬆️  Pushing Workloads to Registry..."
	@for svc in src/test-workloads/*; do \
		if [ -d "$$svc" ]; then \
			svc_name=$$(basename $$svc); \
			$(DOCKER_PUSH) $(REPO_PREFIX)/$$svc_name:$(TAG); \
		fi \
	done

load-workloads:
	@echo "📦 Loading Workloads into K3s..."
	@for svc in src/test-workloads/*; do \
		if [ -d "$$svc" ]; then \
			svc_name=$$(basename $$svc); \
			docker save $(REPO_PREFIX)/$$svc_name:$(TAG) | $(K3S_IMPORT); \
		fi \
	done

# ==============================================================================
# 2. GROUP COMMANDS
# ==============================================================================

# Build Everything (FORCE / NO CACHE by default)
build: force-agent force-aggregator force-controller force-frontend force-workloads
	@echo "✅ All images built (Force Mode)."

# Push Everything to Docker Hub
push: push-agent push-aggregator push-controller push-frontend push-workloads
	@echo "✅ All images pushed to Docker Hub."

# Load Everything (Legacy K3s)
load: load-agent load-aggregator load-controller load-frontend load-workloads
	@echo "✅ All images loaded into K3s."

# ==============================================================================
# 3. DEPLOY & UTILITIES
# ==============================================================================
deploy:
	@echo "1️⃣  Deploying Foundations..."
	kubectl apply -f deploy/00-setup/
	
	@echo "2️⃣  Deploying Microservices..."
	kubectl apply -f deploy/02-demo-apps/workloads.yaml
	@sleep 5
	
	@echo "4️⃣  Deploying SLO-based Autoscaler..."
	kubectl apply -f deploy/02-demo-apps/my-slos.yaml

	@echo "3️⃣  Deploying Observability Stack..."
	kubectl apply -f deploy/01-system/

	@echo "✅ Deployment Complete."

traffic:
	@echo "🚀 Starting Full Spectrum Traffic..."
	kubectl apply -f deploy/02-demo-apps/traffic-generator.yaml

stop-traffic:
	@echo "🛑 Stopping Traffic Generator..."
	kubectl delete -f deploy/02-demo-apps/traffic-generator.yaml --ignore-not-found

clean:
	@echo "🧹 Cleaning up Kubernetes resources..."
	kubectl delete -f deploy/02-demo-apps/ --ignore-not-found
	kubectl delete -f deploy/01-system/ --ignore-not-found
	kubectl delete -f deploy/00-setup/ --ignore-not-found

clean-images:
	@echo "🗑️  Removing local project images..."
	-docker rmi $$(docker images --format '{{.Repository}}:{{.Tag}}' | grep '$(REPO_PREFIX)')