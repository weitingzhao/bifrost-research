# Dagster orchestration stubs (Wave 5.1)

Minimal manifests for `dagster-webserver` + `dagster-daemon` in namespace `research`.

- **replicas: 0** by design — local primary path is `make dagster-dev`.
- Scale up only after instance storage, secrets, and image with `[orchestration]` are ready.

See `CLAUDE.md` § Wave 5.1 for production blockers.
