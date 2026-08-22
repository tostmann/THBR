# THBR — Thread Border Router on an ESP32-C6 stick, host side.
#
# One image, two ways to run it:
#   Home Assistant add-on — the Supervisor builds this file and passes BUILD_FROM
#   plain Docker         — docker build -t thbr addon/
#
# The image carries the stick firmware (firmware/, produced by scripts/dist.sh)
# and flashes it over the same USB port it later uses as the IP backbone.
# Python-only: the pump needs nothing but the standard library and iproute2;
# esptool does the flashing.
ARG BUILD_FROM=python:3-alpine
FROM ${BUILD_FROM}

# The Home Assistant base images are Alpine without Python; python:3-alpine
# already carries one, and installing a second would just double the image.
RUN apk add --no-cache iproute2 \
 && (command -v python3 >/dev/null || apk add --no-cache python3 py3-pip) \
 && pip install --no-cache-dir --break-system-packages "esptool>=5"

WORKDIR /opt/thbr
COPY tap_pump.py thbrctl.py webui.py logo.png logo-dark.png ./
COPY firmware/ ./firmware/

# The release this image was built for.  Baked in rather than read back from
# the Supervisor, so the number is also right under plain Docker, where there
# is no Supervisor to ask.  scripts/publish_images.sh passes the version from
# config.yaml, which is the same string the add-on is installed under.
ARG THBR_VERSION=dev
ENV THBR_VERSION=${THBR_VERSION}

ENV PYTHONUNBUFFERED=1
# start-period covers a first-boot flash (~1 min) plus Thread attach.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD ["python3", "/opt/thbr/thbrctl.py", "status"]

# Both paths land here: plain Docker runs it directly, and the add-on declares
# init: false so the Supervisor starts this entry point rather than an s6 tree.
# thbrctl handles SIGTERM itself, which is what running as PID 1 requires.
ENTRYPOINT ["python3", "/opt/thbr/thbrctl.py"]
CMD ["run"]
