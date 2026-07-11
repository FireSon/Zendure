FROM mcr.microsoft.com/devcontainers/python:3.14

# Install system dependencies for HA
RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    ffmpeg \
    libturbojpeg0 \
    libpcap-dev \
    libudev-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Home Assistant 2026.4.3 with frontend
RUN pip install --no-cache-dir homeassistant==2026.4.3 home-assistant-frontend==20260325.7

# Core component deps required at startup by _base_components() import chain:
#   assist_satellite -> conversation (hassil) -> tts (mutagen, ffmpeg) -> camera -> stream (numpy, av)
#   assist_pipeline (pymicro-vad, pyspeex-noise)
# Plus default_config deps: usb, dhcp, ssdp
RUN pip install --no-cache-dir \
    hassil==3.5.0 \
    home-assistant-intents==2026.3.24 \
    mutagen==1.47.0 \
    ha-ffmpeg==3.2.2 \
    numpy==2.3.2 \
    av==16.0.1 \
    PyTurboJPEG==1.8.0 \
    pymicro-vad==1.0.1 \
    pyspeex-noise==1.0.2 \
    aiousbwatcher==1.1.1 \
    pyserial==3.5 \
    aiodhcpwatcher==1.2.1 \
    aiodiscover==2.7.1 \
    async-upnp-client==0.46.2

# default_config dep: go2rtc
RUN pip install --no-cache-dir go2rtc-client==0.4.0

# Deps for integrations configured in the production backup
RUN pip install --no-cache-dir \
    reolink-aio==0.19.1 \
    PyMetno==0.13.0 \
    getmac==0.9.5 \
    gTTS==2.5.3 \
    forecast-solar==5.0.0 \
    aioshelly==13.23.1 \
    aioesphomeapi==44.1.0 \
    esphome-dashboard-api==1.3.0 \
    bleak-esphome==3.6.0 \
    zha==1.0.2 \
    serialx==0.6.2 \
    PyChromecast==14.0.10 \
    stringcase==1.2.0 \
    aiogithubapi>=22.10.1

# Install debugpy for VS Code debugging
RUN pip install --no-cache-dir debugpy

# Install test dependencies
RUN pip install --no-cache-dir \
    pytest \
    pytest-cov \
    pytest-homeassistant-custom-component \
    pytest-asyncio
