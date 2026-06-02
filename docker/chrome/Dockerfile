# Headless Chromium with a remote-controllable desktop, so Gemini's
# device-bound session credentials (DBSC) get generated and rotated by a
# real browser. Initial Google login is performed by the user through
# noVNC; afterwards Chromium maintains the session on its own.
#
# Layout:
#   - Xvfb            : virtual X display :99
#   - fluxbox         : minimal window manager
#   - x11vnc          : VNC server attached to :99
#   - websockify      : WebSocket-to-VNC bridge + serves noVNC web UI on :6080
#   - chromium        : foreground process; exit code drives container lifecycle
#
# Persisted state: /data/chrome-profile (cookies, DBSC keys, history).
# CDP is exposed on :9222 but should NOT be published publicly — only
# reachable from the docker network (the webai container).

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-sandbox \
        xvfb \
        fluxbox \
        x11vnc \
        novnc \
        websockify \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        ca-certificates \
        dumb-init \
        gosu \
        procps \
        socat \
        tini \
        tinyproxy \
        wget \
    && rm -rf /var/lib/apt/lists/*

# Pre-create the chrome user but DON'T switch to it. Entrypoint starts as
# root so it can fix permissions on the bind-mounted profile dir (host
# directory may be owned by anyone), then drops to the chrome user via
# gosu before launching Chromium.
RUN useradd -m -u 1000 -s /bin/bash chrome \
    && mkdir -p /data/chrome-profile \
    && chown -R chrome:chrome /data/chrome-profile

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /home/chrome

VOLUME ["/data/chrome-profile"]

# 6080 = noVNC web UI (publish this to access Chromium from your browser).
# 9222 = Chrome DevTools Protocol (DO NOT publish; webai connects internally).
EXPOSE 6080 9222

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/entrypoint.sh"]
