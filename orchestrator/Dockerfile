# TKBK Cloud Agents orchestrator.
# Needs: git (clone/push on behalf of the agent), docker CLI (talks to the host
# daemon through /var/run/docker.sock), python runtime.
FROM docker:29-cli AS dockercli

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY pyproject.toml ./
COPY tkbk_agents ./tkbk_agents
RUN pip install --no-cache-dir .
# The orchestrator runs as root on purpose: it owns the Docker socket anyway and
# must chown task workspaces for the sandbox user (uid 10001).
EXPOSE 8090
CMD ["python", "-m", "tkbk_agents", "--host", "0.0.0.0", "--port", "8090"]
