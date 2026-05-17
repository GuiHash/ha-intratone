# Intratone Doorbell — Home Assistant integration

Native Home Assistant integration for the **Intratone** intercom system (manufactured by Cogelec, widely deployed in French apartment buildings). Exposes your apartment intercom as native HA entities and as a HomeKit accessory so calls ring directly on your iPhone, with one-way audio + video and a door-unlock button.

## Features

After pairing, the integration creates three entities under one device:

| Entity | What you can do |
|---|---|
| `event.intratone_<ID>_sonnette` | Fires every time a visitor rings the intercom. Usable as automation trigger. |
| `camera.intratone_<ID>_interphone` | Placeholder image when idle. Live audio + video stream during a call. |
| `lock.intratone_<ID>_porte` | Tap *Unlock* → opens the door (sends `opendoor:*` SIP MESSAGE, same backend as the official Intratone app). |

Exposed to HomeKit via HA's HomeKit Bridge, the camera tile on iPhone Home.app delivers:

- Doorbell push notification < 2 s after a visitor rings (FCM)
- One-way audio (visitor → iPhone): G.711 µ-law transcoded to Opus on the fly
- Video (VP8 → H.264 baseline transcoding) — **opt-in** via the env var `INTRATONE_VIDEO_ENABLED=1`. Enable only if your Intratone subscription includes the visiophone (intercom with actual camera).
- Lock tile to open the door

### Important: door unlock requires an active call

The unlock action sends a SIP MESSAGE inside the active call dialog. It only works in the **~25-second window** starting when the visitor presses the button and ending when the intercom hangs up. There's currently no way to open the door without a visitor ringing — see [Not yet implemented](#not-yet-implemented).

## Not yet implemented

- **Talkback** (iPhone microphone → visitor). The "Talk" button shows in HomeKit but does nothing. **Blocked on upstream**: Home Assistant's HomeKit Bridge and HAP-python don't expose camera microphone input at all today. We'll add it when one of them does.
- **Remote door unlock without a visitor ringing** (Intratone's *Clé mobile* feature). The API supports it (`POST /api/access/open/clemobil`), the integration just doesn't wire it up yet.
- **Call history**, **multiple intercoms in one config entry**, **HomeKit Secure Video**.

## Known quality caveats

- **Audio**: bounded by G.711 µ-law @ 8 kHz (Intratone's wire codec). Some accounts receive only comfort-noise µ-law from the server during a call rather than the real microphone stream — same behaviour observed on the official Intratone iOS app for those accounts, only the GSM fallback carries real audio. Not a bug in this integration; it's a server / account-side condition.
- **Video**: bitrate is set by the intercom hardware (~5-10 kbps observed). Equivalent quality to the official app — there's no client-side knob to request higher quality.
- **France only**: tested against `sip.intratone.info`. Other Cogelec deployments untested.

## Prerequisites

1. **Home Assistant Core 2026.5.x** (the only version actually tested — older versions may work but YMMV).
2. **`ffmpeg`** binary available to HA, built with `libx264` and `libopus`. Pre-installed on HA OS and HA Container.
3. **HA's built-in HomeKit Bridge integration** configured (used to expose entities to iPhone Home.app).
4. **go2rtc add-on** running on `127.0.0.1:8554` (RTSP) and `127.0.0.1:1984` (API) with a pre-declared empty stream named `intratone`. On HA OS install via Add-on Store; on other deployments install separately. Minimal config:
   ```yaml
   streams:
     intratone: ""
   ```
5. **Access to the official Intratone account** that's already paired to your apartment, on a phone where the Intratone app is logged in. You'll use that app to generate a one-time invite code for the HA pairing (see below).

## Installation

### Via HACS (custom repository)

1. HACS → ⋮ menu → **Custom repositories**
2. Add `https://github.com/GuiHash/ha-intratone` with category **Integration**
3. Search **Intratone Doorbell** → Install
4. Restart Home Assistant

### Manual

```bash
cd <your HA config>/custom_components
git clone https://github.com/GuiHash/ha-intratone.git intratone
# Or download a release archive and extract to custom_components/intratone/
```

## Pairing

1. On your existing phone with the official Intratone app: **Mes infos → Ajouter un appareil** → the app generates an invite code in the format `448789 - 1206`.
2. In Home Assistant: **Settings → Devices & Services → Add Integration → Intratone Doorbell**
3. Paste the invite code. HA calls `/api/auth/registercodes` with it, registers an FCM push subscription, and obtains a long-lived device JWT (7-day rolling token).
4. The three entities appear under a new device. Both the existing phone and HA will receive ring notifications in parallel; both can open the door.

If you need to re-pair (e.g. after FCM token rotation), generate a new invite code from the official app and run the integration's re-authentication flow.

## HomeKit Bridge configuration

Add to your `configuration.yaml`:

```yaml
homekit:
  - name: HA Bridge
    filter:
      include_entities:
        - camera.intratone_<ID>_interphone
        - event.intratone_<ID>_sonnette
        - lock.intratone_<ID>_porte
    entity_config:
      camera.intratone_<ID>_interphone:
        support_audio: true
        linked_doorbell_sensor: event.intratone_<ID>_sonnette
        # HomeKit ffmpeg encodes Opus at 24 kbps with frame_duration 60 ms
        # → 180-byte frames. Default RTP packet size 188 (= 176 max payload)
        # rejects them, killing the audio output. Bump to fit + margin.
        audio_packet_size: 384
```

Replace `<ID>` by the suffix HA uses in your entity IDs (your apartment's numeric ID, visible in any of the three entity names). Restart HA so the Bridge picks up the new config.

To opt into VP8 video, set `INTRATONE_VIDEO_ENABLED=1` in HA's environment before starting it.

## Troubleshooting

**Ring doesn't reach iPhone** — verify `event.intratone_<ID>_sonnette` fires in HA (Developer Tools → Events) when someone rings. Check the FCM listener heartbeat in logs (`firebase_messaging` lines every ~20 s). Make sure your HomeKit Bridge accessory is paired and the `linked_doorbell_sensor` is set.

**Tile opens but loading spinner forever** — go2rtc must be running with the `intratone` slot declared. Look for `FFMPEG_PUSH_READY: ... consumable` in HA logs (the marker confirming our ffmpeg pushed successfully); if absent, enable `custom_components.intratone: debug` and look for ffmpeg errors.

**Tile opens but no audio** — look for `Packet size 180 too large` in HA's homekit ffmpeg logs. If present, the `audio_packet_size: 384` setting is missing from the HomeKit `entity_config` above. During a call, the `AUDIO_RX_SUMMARY` log line shows how many audio packets the integration received and whether their content was real audio or comfort-noise.

**Door doesn't open** — the unlock action only works during the active call dialog (see [the constraint above](#important-door-unlock-requires-an-active-call)). Open the camera tile first to trigger the call, then tap the lock within ~25 s.

**JWT expired** — auto-refreshed once per day. If you see `401` errors in logs, reload the integration manually; it will fetch a fresh JWT.

## How it works

```
Visitor presses intercom
     ↓ (FCM push, ~1 s)
HA fires doorbell event → iPhone push notification
     ↓ (user taps tile)
HA opens SIP TCP connection → INVITE → 200 OK + SDP
     ↓
RTP audio (µ-law) + RTP video (VP8) from Intratone server
     ↓ (audio_bridge.py: STUN-respond, depacket RTP, feed ffmpeg)
ffmpeg transcode µ-law → Opus 16k mono / VP8 → H.264 baseline
     ↓
push RTSP → go2rtc → HomeKit Bridge → SRTP → iPhone Home.app
```

See [`INTRATONE_API.md`](INTRATONE_API.md) for the full REST + SIP API reverse-engineering notes.

## Credits

Reverse-engineered from the official Cogelec / Intratone Android app (APK 4.6.3). HTTP, SIP and RTP behaviour mirrors what the official app does so the integration cohabits cleanly alongside it on the same account.

## License

MIT.
