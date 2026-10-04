# The cloud job's image (D36): the agent plus the Google Cloud client libraries. No browser:
# the FPL login is done on your own machine and uploaded to Secret Manager.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY fpl_agent ./fpl_agent
RUN pip install ".[cloud]"

# Cloud Scheduler passes the mode: --mode check|save|final
ENTRYPOINT ["python", "-m", "fpl_agent.run"]
