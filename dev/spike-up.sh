#!/usr/bin/env bash
# Phase 2 HomeKit one-way audio spike — relaunch script.
#
# Idempotent: downloads go2rtc on first run, sets up dev_config symlinks
# (bluetooth stub for macOS TCC + intratone integration), copies the spike
# configuration.yaml from the example if missing, and starts go2rtc + HA.
#
# Once iPhone is paired with "HA Spike Bridge", the pairing is preserved in
# dev_config/.storage/ across reruns so future tests don't need re-pairing.
#
# Validates the path RTSP(audio+video) → HA camera.ffmpeg → HomeKit Bridge
# (support_audio: true) → iPhone Home.app audio output.

set -e
cd "$(dirname "$0")/.."

GO2RTC_VERSION=v1.9.14
ARCH=$(uname -m | sed 's/x86_64/amd64/')
GO2RTC_BIN=.context/go2rtc

# 1. Download go2rtc binary if missing (architecture-specific, gitignored)
if [ ! -x "$GO2RTC_BIN" ]; then
  mkdir -p .context
  echo "Downloading go2rtc $GO2RTC_VERSION ($ARCH)..."
  curl -sL -o /tmp/go2rtc.zip "https://github.com/AlexxIT/go2rtc/releases/download/$GO2RTC_VERSION/go2rtc_mac_$ARCH.zip"
  unzip -oq /tmp/go2rtc.zip -d .context/
  chmod +x "$GO2RTC_BIN"
fi

# 2. Set up dev_config (preserves .storage/ pairing data across reruns)
mkdir -p dev_config/custom_components
# Generate manifest.json from template (gitignored — hacs/default's hassfest script
# requires exactly one manifest.json per repo; this file must not be committed)
cp dev/ha_overrides/bluetooth/manifest.json.template dev/ha_overrides/bluetooth/manifest.json
ln -sfn ../../dev/ha_overrides/bluetooth dev_config/custom_components/bluetooth
ln -sfn ../../custom_components/intratone dev_config/custom_components/intratone

if [ ! -f dev_config/configuration.yaml ]; then
  cp dev/configuration-spike.example.yaml dev_config/configuration.yaml
  echo "Copied dev/configuration-spike.example.yaml → dev_config/configuration.yaml"
fi

# 3. Verify venv exists
if [ ! -x .venv/bin/hass ]; then
  echo "ERROR: .venv/bin/hass missing. Run: python3.13 -m venv .venv && .venv/bin/pip install homeassistant ha-ffmpeg PyTurboJPEG"
  exit 1
fi

# 4. Kill any previous instances
pkill -f "go2rtc -config" 2>/dev/null || true
pkill -f "hass -c dev_config" 2>/dev/null || true
sleep 1

# 5. Start go2rtc + HA detached
mkdir -p .context/logs
nohup "$GO2RTC_BIN" -config dev/go2rtc.yaml > .context/logs/go2rtc.log 2>&1 &
echo "go2rtc pid: $!"

nohup .venv/bin/hass -c dev_config > .context/logs/ha.log 2>&1 &
echo "hass    pid: $!"

echo ""
echo "HA UI:           http://localhost:8123"
echo "HomeKit Bridge:  advertised as 'HA Spike Bridge' on _hap._tcp.local"
echo "Logs:            tail -f .context/logs/{ha,go2rtc}.log"
