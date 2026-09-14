# Single image for API, worker and the one-shot verifier.
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# --- Pinned, checksum-verified LibreOffice ------------------------------------
# The converter is an EXACT upstream build (LibreOffice 26.2.6.3) fetched from
# The Document Foundation and verified by a per-architecture SHA-256, so the
# binary is concrete and immutable. Shared-system dependencies are resolved
# from the standard bookworm repo in a single apt transaction.
ARG LO_UPSTREAM=26.2.6
ARG LO_BUILD=26.2.6.3
ARG TARGETARCH
RUN set -eux; \
    arch_name="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch_name" in \
      amd64) \
        url_arch=x86_64; file_arch=x86-64; \
        lo_sum=fd0e8f8f2408dd2e5b90286e60f3f97cf566ba441cd48cfc5bcc68067303e0bc ;; \
      arm64) \
        url_arch=aarch64; file_arch=aarch64; \
        lo_sum=f8e8b1d30abde0d530d727ce1b26909ccbedc3d76bcf75d93b3dd5fcd5b8d278 ;; \
      *) echo "unsupported architecture=$arch_name" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    tarball="LibreOffice_${LO_UPSTREAM}_Linux_${file_arch}_deb.tar.gz"; \
    archive_tarball="LibreOffice_${LO_BUILD}_Linux_${file_arch}_deb.tar.gz"; \
    # Primary mirror, then the immutable official archive as fallback.
    curl -fsSL -o /tmp/lo.tar.gz \
        "https://download.documentfoundation.org/libreoffice/stable/${LO_UPSTREAM}/deb/${url_arch}/${tarball}" \
     || curl -fsSL -o /tmp/lo.tar.gz \
        "https://downloadarchive.documentfoundation.org/libreoffice/old/${LO_BUILD}/deb/${url_arch}/${archive_tarball}"; \
    echo "${lo_sum}  /tmp/lo.tar.gz" | sha256sum -c -; \
    mkdir -p /tmp/lo; \
    tar -xzf /tmp/lo.tar.gz -C /tmp/lo; \
    deb_dir="/tmp/lo/LibreOffice_${LO_BUILD}_Linux_${file_arch}_deb/DEBS"; \
    test -d "$deb_dir"; \
    # Single apt transaction installs the bundled, pinned debs and resolves
    # their shared-library dependencies from bookworm (fails loudly if not).
    apt-get install -y --no-install-recommends "$deb_dir"/*.deb; \
    apt-get install -y --no-install-recommends \
        fontconfig fonts-liberation fonts-noto-cjk; \
    ln -sf /opt/libreoffice26.2/program/soffice /usr/local/bin/soffice; \
    # Hard build-time check: fail early if a runtime library is missing.
    /opt/libreoffice26.2/program/soffice --version; \
    rm -rf /var/lib/apt/lists/* /tmp/lo /tmp/lo.tar.gz

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY verify ./verify

# Pinned static docker CLI, used only by the one-shot "verify" service to kill
# and restart the worker container through the mounted Docker socket.
ARG DOCKER_CLI_VERSION=26.1.4
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
    SOFFICE_BIN=/opt/libreoffice26.2/program/soffice

EXPOSE 8000

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
