# Single image for API, worker and the one-shot verifier.
# Base: pinned Python minor on Debian bookworm.
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# --- Reproducible, version-pinned LibreOffice ---------------------------------
# Freeze apt to a single Debian snapshot and install an exact LibreOffice
# build, so "fixed version in the container" is a concrete, reproducible
# binary: 1:7.4.7-1+deb12u14 from snapshot 20260701T084025Z.
ARG DEBIAN_SNAPSHOT=20260701T084025Z
ARG LIBREOFFICE_VERSION=4:7.4.7-1+deb12u14
RUN set -eux; \
    printf 'Acquire::Check-Valid-Until "false";\nAcquire::Retries "5";\n' \
        > /etc/apt/apt.conf.d/99snapshot; \
    rm -f /etc/apt/sources.list.d/debian.sources; \
    printf 'deb http://snapshot.debian.org/archive/debian/%s bookworm main\n' \
        "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        "libreoffice-writer-nogui=$LIBREOFFICE_VERSION" \
        "libreoffice-core-nogui=$LIBREOFFICE_VERSION" \
        fonts-liberation \
        fonts-noto-cjk \
        ca-certificates \
        curl; \
    rm -rf /var/lib/apt/lists/*; \
    soffice --version

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY verify ./verify

# Pinned static docker CLI, used only by the one-shot "verify" service to kill
# and restart the worker container through the mounted Docker socket.
ARG DOCKER_CLI_VERSION=26.1.4
# TARGETARCH is supplied automatically by BuildKit (amd64 / arm64); fall back
# to dpkg architecture for non-BuildKit builds.
ARG TARGETARCH
RUN set -eux; \
    arch_name="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch_name" in \
      amd64) arch=x86_64;  sum=a9cede81aa3337f310132c2c920dba2edc8d29b7d97065b63ba41cf47ae1ca4f ;; \
      arm64) arch=aarch64; sum=6f1a5fb161aef875d305ee4f79e65492b3c13e90dbe0a339df2ad6515e4f6849 ;; \
      *) echo "unsupported architecture=$arch_name" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/docker.tgz \
      "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_CLI_VERSION}.tgz"; \
    echo "${sum}  /tmp/docker.tgz" | sha256sum -c -; \
    tar -xzf /tmp/docker.tgz --strip-components=1 -C /usr/local/bin docker/docker; \
    rm -f /tmp/docker.tgz; \
    docker --version

RUN mkdir -p /data/storage/source /data/storage/tmp /data/storage/pdf
ENV DATABASE_URL=sqlite:////data/docx2pdf.db \
    STORAGE_DIR=/data/storage \
    SOFFICE_BIN=/usr/lib/libreoffice/program/soffice

EXPOSE 8000

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
