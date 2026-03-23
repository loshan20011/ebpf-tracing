Sock Shop app-specific helpers:

- `deploy_sockshop_k3s.sh`: deploy and patch the Sock Shop application on the target cluster.
- `login_load_probe.py`: app-specific `/login` load probe used by benchmark runners.
- `seed_customers.py`: seed customer records through the Sock Shop front-end API.
- `working_endpoints.txt`: quick endpoint notes captured during route validation.

Keep this folder for Sock Shop application deployment and app-specific probes only.

Benchmark orchestration scripts and reusable benchmark seeders live under:

- `src/scripts/benchmark/`
