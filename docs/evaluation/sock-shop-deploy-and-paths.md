# Sock Shop Deploy And Paths

## Purpose

This note describes:

- how to deploy the upstream Sock Shop demo into the `sock-shop` namespace on k3s
- how the deployment is patched for this environment
- which internal and external paths were verified as working

## Deployment

The project includes a helper script that deploys Sock Shop and applies the k3s-specific fixes:

- removes the worker-only node selector
- replaces incompatible PVC assumptions with `local-path`
- fixes security context issues that block some upstream containers
- sets the `front-end` service to a NodePort

Run:

```bash
bash src/sockshop/deploy_sockshop_k3s.sh
```

Optional override example:

```bash
NAMESPACE=sock-shop \
FRONTEND_NODE_PORT=30001 \
MONGO_PASSWORD=admin \
SESSION_DB_IMAGE=redis:7-alpine \
bash src/sockshop/deploy_sockshop_k3s.sh
```

## Expected Namespace

The application is installed into:

```bash
sock-shop
```

Verify:

```bash
kubectl get deploy,pods,svc -n sock-shop
```

## Front-End Access

The front-end is exposed with NodePort `30001`.

Base URL:

```bash
FE_URL="http://172.31.32.23:30001"
```

Quick check:

```bash
curl -I "$FE_URL/"
```

## Internal Service Path Checks

These commands verify that the Sock Shop `front-end` pod can reach the expected downstream services:

```bash
kubectl -n sock-shop exec deploy/front-end -- sh -c 'wget -qO- http://catalogue/catalogue >/dev/null && echo catalogue_ok'
kubectl -n sock-shop exec deploy/front-end -- sh -c 'wget -qO- http://user/health >/dev/null && echo user_ok'
kubectl -n sock-shop exec deploy/front-end -- sh -c 'wget -qO- http://carts/health >/dev/null && echo carts_ok'
kubectl -n sock-shop exec deploy/front-end -- sh -c 'wget -qO- http://orders/health >/dev/null && echo orders_ok'
kubectl -n sock-shop exec deploy/front-end -- sh -c 'wget -qO- http://shipping/health >/dev/null && echo shipping_ok'
kubectl -n sock-shop exec deploy/front-end -- sh -c 'wget -qO- http://payment/health >/dev/null && echo payment_ok'
```

Expected success output:

- `catalogue_ok`
- `user_ok`
- `carts_ok`
- `orders_ok`
- `shipping_ok`
- `payment_ok`

## External Front-End Paths

These paths were used as the main external reachability checks:

```bash
FE_URL="http://172.31.32.23:30001"
curl -fsS -o /dev/null "$FE_URL/"
curl -fsS -o /dev/null "$FE_URL/catalogue"
curl -fsS -o /dev/null "$FE_URL/basket.html"
curl -fsS -o /dev/null "$FE_URL/customers"
echo "front-end_http_ok"
```

### Verified external paths

- `/`
- `/catalogue`
- `/basket.html`
- `/customers`

## Notes About Path Meaning

- `/` checks the front-end landing page
- `/catalogue` exercises the front-end to catalogue path
- `/basket.html` is a front-end page used in the functional checks
- `/customers` verifies a user-related API path through the front-end service

## Recommended Pre-Evaluation Check

Before running workload or functional experiments, run:

```bash
kubectl get pods -n sock-shop
kubectl get svc -n sock-shop

kubectl -n sock-shop exec deploy/front-end -- sh -c 'wget -qO- http://catalogue/catalogue >/dev/null && echo catalogue_ok'
kubectl -n sock-shop exec deploy/front-end -- sh -c 'wget -qO- http://user/health >/dev/null && echo user_ok'
kubectl -n sock-shop exec deploy/front-end -- sh -c 'wget -qO- http://carts/health >/dev/null && echo carts_ok'
kubectl -n sock-shop exec deploy/front-end -- sh -c 'wget -qO- http://orders/health >/dev/null && echo orders_ok'
kubectl -n sock-shop exec deploy/front-end -- sh -c 'wget -qO- http://shipping/health >/dev/null && echo shipping_ok'
kubectl -n sock-shop exec deploy/front-end -- sh -c 'wget -qO- http://payment/health >/dev/null && echo payment_ok'

FE_URL="http://172.31.32.23:30001"
curl -fsS -o /dev/null "$FE_URL/"
curl -fsS -o /dev/null "$FE_URL/catalogue"
curl -fsS -o /dev/null "$FE_URL/basket.html"
curl -fsS -o /dev/null "$FE_URL/customers"
echo "front-end_http_ok"
```

If all checks pass, Sock Shop is in a good state for evaluation traffic.
