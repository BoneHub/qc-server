# BoneHub Dataset Quality Check server.
#
# The image carries only the data schema from BoneHub-Dataset, with its [io] extra
# (numpy, SimpleITK) for reading and writing .seg.nrrd segmentations, plus FastAPI.
# Nothing from the conversion or segmentation stack is installed.
#
#   docker build -t bonehub-qc-server .
#   docker run -p 8000:8000 -v /path/to/BoneHub_Dataset:/data bonehub-qc-server

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

# The dataset folder is mounted here; all server state lives in /data/.bonehub_qc.
ENV BONEHUB_QC_DATASET_ROOT=/data \
    BONEHUB_QC_HOST=0.0.0.0 \
    BONEHUB_QC_PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# python rather than curl: the slim image ships no HTTP client.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["bonehub-qc-server", "serve"]
