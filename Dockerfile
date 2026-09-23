# check=skip=SecretsUsedInArgOrEnv
# (The check trips over the name BONEHUB_QC_CREDENTIALS_DIR, which holds a folder path,
# not a secret. The keys themselves never pass through ARG or ENV in this file.)
#
# BoneHub Dataset Quality Check server. Run it with docker compose (see docker-compose.yml
# and the README); Docker is the only supported way to run the server.
#
# The image carries only the data schema from BoneHub-Dataset, with its [io] extra
# (numpy, SimpleITK) for reading and writing .seg.nrrd segmentations, plus FastAPI.
# Nothing from the conversion or segmentation stack is installed.
#
# Two mounts:
#   /data                 the dataset, read-write. The server keeps its non-secret state
#                         in /data/.bonehub_qc/<server id>/.
#   /var/lib/bonehub-qc   the server's credentials: its id, private key, admin key and
#                         reviewer accounts. A volume on the Docker host, never the share.

FROM python:3.11-slim

# git is needed only to resolve the `bonehub-dataset @ git+https://...` dependency.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY bonehub_quality_check_server ./bonehub_quality_check_server

RUN pip install --no-cache-dir . \
    && apt-get purge -y git \
    && apt-get autoremove -y

# Readable by the server alone.
RUN mkdir -p /var/lib/bonehub-qc && chmod 700 /var/lib/bonehub-qc

ENV BONEHUB_QC_DATASET_ROOT=/data \
    BONEHUB_QC_CREDENTIALS_DIR=/var/lib/bonehub-qc \
    BONEHUB_QC_HOST=0.0.0.0 \
    BONEHUB_QC_PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# python rather than curl: the slim image ships no HTTP client.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["bonehub-qc-server", "serve"]
