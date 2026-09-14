# Single image for API, worker and the one-shot verifier.
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# --- Pinned, checksum-verified LibreOffice ------------------------------------
# The converter is an EXACT upstream build (LibreOffice 26.2.6.3) fetched from
# The Document Foundation and verified by a per-architecture SHA-256, so the
# binary is concrete and immutable.
#
# IMPORTANT: the TDF .debs ship with EMPTY Depends metadata, so apt will not
# pull any shared libraries for them. The RUNTIME_LIBS list below is the exact
# set of system libraries a headless writer_pdf_Export run needs; it was
# derived empirically (LD_DEBUG during a real --headless conversion) and proven
# on a fresh bookworm sysroot where soffice converts text and image documents
# with zero missing NEEDED libs. Keep the debs and these libs in ONE apt
# transaction so the build fails loudly if anything is unresolvable.
ARG LO_UPSTREAM=26.2.6
ARG LO_BUILD=26.2.6.3
ARG TARGETARCH
COPY docker/build_gate.py /tmp/build_gate.py
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
    # Install the pinned local debs together with the explicit headless
    # runtime libraries they fail to declare.
    apt-get install -y --no-install-recommends \
        "$deb_dir"/*.deb \
        fontconfig \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-noto-cjk \
        libavahi-client3 \
        libavahi-common3 \
        libblkid1 \
        libbrotli1 \
        libbsd0 \
        libc6 \
        libcairo2 \
        libcap2 \
        libcom-err2 \
        libcups2 \
        libdbus-1-3 \
        libelogind0 \
        libexpat1 \
        libffi8 \
        libfontconfig1 \
        libfreetype6 \
        libgcc-s1 \
        libgcrypt20 \
        libglib2.0-0 \
        libgmp10 \
        libgnutls30 \
        libgpg-error0 \
        libgssapi-krb5-2 \
        libhogweed6 \
        libidn2-0 \
        libk5crypto3 \
        libkeyutils1 \
        libkrb5-3 \
        libkrb5support0 \
        liblz4-1 \
        liblzma5 \
        libmd0 \
        libmount1 \
        libnettle8 \
        libnspr4 \
        libnss3 \
        libp11-kit0 \
        libpcre2-8-0 \
        libpixman-1-0 \
        libpng16-16 \
        libselinux1 \
        libsqlite3-0 \
        libstdc++6 \
        libtasn1-6 \
        libunistring2 \
        libx11-6 \
        libx11-xcb1 \
        libxau6 \
        libxcb-render0 \
        libxcb-shm0 \
        libxcb1 \
        libxdmcp6 \
        libxext6 \
        libxinerama1 \
        libxrender1 \
        libzadc4 \
        libzstd1 \
        ocl-icd-libopencl1; \
    ln -sf /opt/libreoffice26.2/program/soffice /usr/local/bin/soffice; \
    # Definitive build gate: actually run a headless DOCX->PDF conversion.
    # This loads the real filter/VCL libraries, so a missing ARM64 runtime
    # library fails the BUILD here rather than at worker startup. The docx is
    # generated on the fly (stdlib) and the PDF is removed; none is shipped.
    python3 /tmp/build_gate.py /tmp/gate.docx; \
    mkdir -p /tmp/gateout; \
    /opt/libreoffice26.2/program/soffice --headless --norestore --nolockcheck \
        --nodefault -env:UserInstallation=file:///tmp/gateprofile \
        --convert-to pdf --outdir /tmp/gateout /tmp/gate.docx; \
    test -f /tmp/gateout/gate.pdf; \
    head -c 5 /tmp/gateout/gate.pdf | grep -q '%PDF-'; \
    echo 'LibreOffice build gate: %PDF- OK'; \
    rm -rf /var/lib/apt/lists/* /tmp/lo /tmp/lo.tar.gz \
        /tmp/gate.docx /tmp/gateout /tmp/gateprofile /tmp/build_gate.py

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
