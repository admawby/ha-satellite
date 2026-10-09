ARG BUILD_FROM
FROM ${BUILD_FROM}

ARG XTERM_VERSION=5.5.0
ARG XTERM_FIT_VERSION=0.10.0

# Compiled dependencies come from Alpine; pure-Python ones from PyPI.
RUN apk add --no-cache \
        python3 \
        py3-pip \
        py3-aiohttp \
        py3-cryptography \
        curl \
    && pip3 install --no-cache-dir --break-system-packages \
        "asyncssh>=2.14,<3" \
        "paho-mqtt>=2.0,<3"

COPY rootfs /

# Vendor xterm.js so the web terminal works without internet access in the browser.
RUN set -eux; \
    vendor=/opt/hasat/server/hasat/web/vendor; \
    mkdir -p "$vendor"; \
    cdn=https://cdn.jsdelivr.net/npm; \
    curl -fsSL -o "$vendor/xterm.js"  "$cdn/@xterm/xterm@${XTERM_VERSION}/lib/xterm.js"; \
    curl -fsSL -o "$vendor/xterm.css" "$cdn/@xterm/xterm@${XTERM_VERSION}/css/xterm.css"; \
    curl -fsSL -o "$vendor/addon-fit.js" "$cdn/@xterm/addon-fit@${XTERM_FIT_VERSION}/lib/addon-fit.js"; \
    chmod a+x /etc/s6-overlay/s6-rc.d/hasat/run /etc/s6-overlay/s6-rc.d/hasat/finish
