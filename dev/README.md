# Local dev environment (macOS)

This folder contains versioned artifacts for setting up a working Home
Assistant dev instance on macOS, and for reproducing the Phase 2 prereq
spikes. Files here are gitignored counterparts under `dev_config/` and
`.context/` — they stay local, this folder is the source of truth.

## What's here

| Path | Purpose |
|---|---|
| `custom_components/bluetooth/` | No-op stub that overrides HA's core `bluetooth` integration **only in dev_config/**. macOS' TCC SIGKILLs any Python process that touches Core Bluetooth without `NSBluetoothAlwaysUsageDescription` in its Info.plist, which `hass` from a venv doesn't have. The stub prevents the auto-load crash. **Never ship this** under the project-root `custom_components/` — that would break real Linux/HA OS installs. |
| `go2rtc.yaml` | Mock RTSP server config used by the HomeKit one-way audio spike. Generates `sine 440Hz + smptebars` at `rtsp://127.0.0.1:8554/fake_doorbell`. |
| `configuration-spike.example.yaml` | Minimal HA config for the spike (camera.ffmpeg + homekit bridge with `support_audio: true`). Copied to `dev_config/configuration.yaml` on first run if missing. |
| `spike-up.sh` | Idempotent launcher: downloads `go2rtc` binary if needed, sets up symlinks, starts go2rtc + HA detached. |

## First-time setup

```bash
# 1. Create venv with Python 3.13 (HA does not support 3.14 yet)
python3.13 -m venv .venv
.venv/bin/pip install homeassistant ha-ffmpeg PyTurboJPEG

# 2. Launch the spike
./dev/spike-up.sh

# 3. Pair from iPhone (Home.app → Add Accessory → "HA Spike Bridge"),
#    pairing code shown in .context/logs/ha.log:
grep "Pincode:" .context/logs/ha.log
```

Pairing is persisted in `dev_config/.storage/` (gitignored) so subsequent
runs of `spike-up.sh` reconnect to the iPhone without re-pairing.

## Why a separate `dev_config/`

HA's runtime data (`.storage/`, logs, registries) lives in `dev_config/`,
which is fully gitignored to avoid leaking per-dev tokens and apartment IDs.
The reusable bits (stubs, scripts, example configs) live here in `dev/` and
are symlinked into `dev_config/` by `spike-up.sh`.
