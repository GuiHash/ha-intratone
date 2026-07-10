# Intratone API — Reverse Engineering Notes

Reverse-engineered from APK `com.cogelec.notificationpush` (v4.6.3, plus the
`mobipass` transfer flow in §4.7 from v4.6.4) and live traffic analysis. Goal:
build a virtual device (e.g. Home Assistant integration) that receives doorbell
push notifications, answers SIP calls, and opens doors without a physical phone.

---

## 1. Architecture

```mermaid
flowchart TD
    phone[Intratone<br/>phone app]
    rest[Intratone REST<br/>sip.intratone.info]
    sched[Intratone push<br/>scheduler]
    bell[Cogelec doorbell<br/>in building]
    fcm[Google FCM/MCS<br/>mtalk.google.com]
    client[Your client<br/>firebase-msg +<br/>SIP UAC]
    pbx[SIP PBX<br/>Asterisk]
    door([door unlocked])

    phone -- "invite code<br/>register device" --> rest
    rest -- "POST /registercodes<br/>(id_fcm = FCM token)" --> sched
    bell -- "ring event" --> sched
    sched -- "FCM data push<br/>via Firebase" --> fcm
    fcm -- "MCS push (TCP 5228)" --> client
    client -- "SIP INVITE" --> pbx
    pbx -- "200 OK + SDP" --> client
    client <-. "RTP audio" .-> pbx
    client -- "POST /calls/{id}/answer" --> door
```

Three protocols are involved:

| Protocol     | Purpose                                       | Endpoint                       |
| ------------ | --------------------------------------------- | ------------------------------ |
| HTTPS REST   | Device registration, JWT auth, door commands  | `sip.intratone.info`           |
| FCM / MCS    | Doorbell push notification                    | `mtalk.google.com:5228` (TLS)  |
| SIP/UDP+RTP  | Voice (intercom audio)                        | `178.32.84.135:5060`           |

---

## 2. Firebase / FCM configuration

Extracted from `strings.xml` and `google-services.json` in the APK:

| Key                | Value                                              |
| ------------------ | -------------------------------------------------- |
| `project_id`       | `android-ipvideo-studio`                           |
| `app_id`           | `1:676502914290:android:5393f05ec7f22bd6`          |
| `api_key`          | `AIzaSyB7RtCyt6LZWMruWKj7Z_9Ii7_VAIVdSKU`          |
| `messaging_sender_id` | `676502914290`                                  |
| `package_name`     | `com.cogelec.notificationpush`                     |
| `cert_sha1`        | `353F1762E3AE0B2DD83DC74D282BA77EC6A934D4`         |

**Important**: the `bundle_id` passed to FCM registration MUST be
`com.cogelec.notificationpush`. Using anything else (e.g. `org.chromium.linux`
or the default `receiver.push.com`) yields a token Intratone won't push to.

---

## 3. Receiving FCM pushes without an Android device

Standard approach: use Google's MCS protocol (the same TCP socket Google Play
Services maintains on real devices) to receive FCM data messages bound to a
specific `(android_id, FCM token)` pair.

**Don't roll your own MCS client.** Use [`firebase-messaging`][fbmsg] (Python)
or [`push-receiver`][pr] (Node). They handle:

- Firebase Installation Service (FIS) registration
- GCM check-in (`android.clients.google.com/checkin`) — gets `android_id` + `security_token`
- FCM token registration (`fcmregistrations.googleapis.com` or legacy `iid/register`)
- MCS LoginRequest with the correct proto field numbers (#1 `id`, #2 `domain`,
  #3 `user`, #4 `resource`, #5 `auth_token`, #6 `device_id="android-{hex}"`,
  #13 `use_rmq2=true`, #15 `auth_service=ANDROID_ID`)
- Heartbeats (client-initiated every 5 min by default)
- `persistent_id` tracking + selective ACKs (without this Google stops sending
  new pushes after a few queued messages)

[fbmsg]: https://pypi.org/project/firebase-messaging/
[pr]: https://github.com/MatthieuLemoine/push-receiver

### Minimal Python example

```python
from firebase_messaging import FcmPushClient, FcmRegisterConfig

config = FcmRegisterConfig(
    project_id="android-ipvideo-studio",
    app_id="1:676502914290:android:5393f05ec7f22bd6",
    api_key="AIzaSyB7RtCyt6LZWMruWKj7Z_9Ii7_VAIVdSKU",
    messaging_sender_id="676502914290",
    bundle_id="com.cogelec.notificationpush",
)

def on_push(notification, persistent_id, ctx):
    data = notification["data"]          # <- payload is nested here
    print("LOGIN_TO_CALL =", data["LOGIN_TO_CALL"])
    print("call_id       =", data["call_id"])

client = FcmPushClient(on_push, config,
                       credentials=saved_creds_or_None,
                       credentials_updated_callback=persist_creds)
fcm_token = await client.checkin_or_register()   # pass to Intratone /registercodes
await client.start()
```

### Credentials persistence

`firebase-messaging` returns a dict with this shape; persist it as JSON and
pass it back on next run to avoid re-registering:

```jsonc
{
  "keys":  { "private": "...", "public": "...", "secret": "...", "authSecret": "..." },
  "gcm":   { "android_id": 5042706006057121824, "security_token": ..., "token": "AC..." },
  "fcm":   { "installation": { ... }, "registration": { "token": "cCuan7vQLHKC-..." } }
}
```

The FCM token (`fcm.registration.token`) is what Intratone needs.

---

## 4. Intratone REST API

Base URL: `https://sip.intratone.info/`

All POST endpoints use `application/x-www-form-urlencoded` (NOT JSON).
Authenticated endpoints expect `Authorization: Bearer <JWT>`.

### App credentials (hardcoded in APK)

```
app_id    = app_apisip_android
app_token = >KompY95?oijeIKR8049?OLysIekjpceKejLAHhh
```

### 4.1 — `POST /api/auth/registercodes`  (Invite-code registration)

Register a new virtual device on an existing apartment using an invite code
generated in the official app (Mes infos → Ajouter un appareil → format
`448789 - 1206`).

Body fields:

| field                  | value                                |
| ---------------------- | ------------------------------------ |
| `app_id`               | `app_apisip_android`                 |
| `app_token`            | (constant above)                     |
| `code`                 | first part of invite code (`448789`) |
| `codepass`             | second part (`1206`)                 |
| `os`                   | `android`                            |
| `osv`                  | `29`                                 |
| `model`                | any string (`HA-Bridge`)             |
| `manufacturer`         | any string                           |
| `device_id`            | your stable client UUID              |
| `description`          | any string                           |
| `appversion`           | `4.6.4`                              |
| `id_fcm`               | your FCM token                       |
| `id_wonderpush`        | empty                                |
| `pushkit_id`           | empty (iOS only)                     |
| `bundleid`             | `com.cogelec.notificationpush`       |
| `device_country`       | `FRA`                                |
| `device_language`      | `fr`                                 |
| `carrier_name`         | `Orange`                             |
| `carrier_countrycode`  | `FR`                                 |
| `carrier_countryiso`   | `fr`                                 |
| `carrier_networkcode`  | `20801`                              |

Response:
```json
{ "v": "1.5.0", "error": 0, "state": "ok",
  "data": { "id": "3844428", "tel": "0671124546" } }
```

- `data.id` is the **numeric_id** (used as `smartphone_id` later)
- `data.tel` is the phone number tied to the apartment

### 4.2 — `POST /api/auth/device`  (Authenticate + JWT refresh)

**This is also the JWT refresh endpoint.** Call it whenever your JWT is near
expiry. No SMS or invite code needed because the `device_id` is persistent.

Body:

| field         | value                       |
| ------------- | --------------------------- |
| `app_id`      | `app_apisip_android`        |
| `app_token`   | (constant)                  |
| `tel`         | phone from registercodes    |
| `device_id`   | same as registration        |
| `appversion`  | `4.6.4`                     |

Response:
```json
{ "v": "1.5.0", "error": 0, "state": "ok",
  "data": {
    "indicatif": "33",
    "id": "3844428",
    "tel": "0671124546",
    "code": "-1",
    "device_id": "ha-intratone-bridge-v1",
    "device": "HA-Bridge",
    "lift": 0, "floor": 0,
    "jwt": "eyJhbGciOiJIUzUxMiJ9...",
    "expire_at": "2026-05-21 10:34:00",
    "expire_gmt_at": "2026-05-21 08:34:00",
    "openingaccess": "0",
    "access_refresh": "0",
    "mobipass_compatible": "1",
    "mobipass": "0"
  } }
```

#### CléMobil / Mobipass flags (`data.*`, each `1`/`0` — sent as int or string)

Confirmed by decompiling the Android `AuthDevice` parser in v4.6.4
(`AuthRepositoryImpl` reads them into four booleans):

| field                 | meaning                                                                 |
| --------------------- | ----------------------------------------------------------------------- |
| `openingaccess`       | remote-open (CléMobil) currently enabled on this device                 |
| `access_refresh`      | client should refresh its access list                                   |
| `mobipass_compatible` | the account/number is eligible for the single-owner **Mobipass** scheme |
| `mobipass`            | the Mobipass key is currently **active on this device**                 |

Since ~June 2026 only one device per phone number may hold the key. The app
treats the key as usable when `mobipass_compatible && mobipass`.

> **These flags are NOT a reliable eligibility signal for a third-party client.**
> The official app is also fed `mobipassCompatible`/`mobipass` out-of-band via
> FCM push (`AccessEvent.MobipassCompatible`), so an account that *is* eligible
> can still report `mobipass_compatible == "0"` on `auth/device` for our client
> (observed in issue #61). Don't gate the transfer on them — attempt the flow in
> §4.7 and let the server answer (`MOBIPASS_NOT_AVAILABLE` when it truly can't).

### 4.3 — `POST /api/calls/{call_id}/answer`  (Open door)

Triggers the relay that unlocks the door. Send this when you decide to "answer"
an incoming call. Works while the call is active (typically 30s after the FCM
push).

Body:

| field           | value                              |
| --------------- | ---------------------------------- |
| `smartphone_id` | the `numeric_id` from registration |

Header: `Authorization: Bearer <JWT>`

Response: `{ "error": 0, "state": "ok", ... }` → door opened.

### 4.4 — `GET /api/access`  (List remote-openable accesses — "Clé mobile" / mobipass)

Lists the doors/gates the account can open **without anyone ringing** — what the
app shows as "Clé mobile" tiles, and what the **mobipass** service drives.

Header: `Authorization: Bearer <JWT>`

Response envelope (the app parses `response["data"]["list"]` — confirmed by
disassembling `APIManager.getAccessList` in the decompiled iOS app v4.4.10):

```json
{ "v": "...", "error": 0, "state": "ok",
  "data": {
    "list": [
      { "id": 11, "residence": "Résidence A", "name": "Portail véhicule",
        "phonenumber": "0612345678", "openmode": "data" }
    ]
  } }
```

The per-item JSON keys — `id`, `residence`, `name`, `phonenumber`, `openmode` —
were confirmed by disassembling the iOS `AccessItem` decoder (the keys are read
literally there). Note the response key is **`openmode`** (lowercase, singular)
even though the in-memory struct field is `openModes: [OpenMode]`; the value may
arrive as a single string or a list.

| field         | type             | notes                                          |
| ------------- | ---------------- | ---------------------------------------------- |
| `id`          | Int              | the **access_id** (stable key; sent to open)   |
| `residence`   | String           | building/residence name (accesses group by it) |
| `name`        | String           | door/gate label                                |
| `phonenumber` | String           | number the legacy 2G flow used to call         |
| `openmode`    | String / [String]| which open mode(s) this access supports        |

`OpenMode` values (Android enum `CLEMOBIL`, `DATA`, `BLE`; iOS also has
`unknown`). **How the access is opened depends on its first mode** — the app
dispatches on `firstOrNull(openModes)` (Android `AccessViewModel.openAccess`):

| primary mode | how the app opens it                                    | usable from a 3rd-party client? |
| ------------ | ------------------------------------------------------- | ------------------------------- |
| `data`       | REST API (§4.5) — **mobipass**, 4G                      | ✅ yes                          |
| `ble`        | REST API (§4.5) — `openAccessByApiUseCase`              | ✅ yes                          |
| `clemobil`   | **places a real GSM phone call** to `phonenumber` (`openAccessByPhoneUseCase` → `LaunchPhoneCall`) — legacy 2G FlashCall | ❌ no (needs a dialer) |

So a non-phone client can only open `data`/`ble` accesses via §4.5; `clemobil`
accesses require actually dialling the access's `phonenumber`.

### 4.5 — `POST /api/access/open/clemobil`  (Open an access — mobipass & Clémobil)

Opens a door/gate on demand, no incoming call required. Despite the legacy
`clemobil` in its name, this is the **API open path the app uses for `data`
(mobipass) and `ble` accesses** — `openAccessByApiUseCase` →
`AccessApiService.openApnAccessOnAPI`. The body does **not** carry the mode; it
holds only `phonenumber` + `access_id`. Confirmed on both platforms (Android
Retrofit `@FormUrlEncoded @POST("/api/access/open/clemobil") @Field` and iOS
`APIManager.openAccessWithCleMobil`).

> **Not used for `clemobil`-mode accesses.** An access whose first mode is
> `clemobil` is opened by the app via a **real GSM phone call** to its
> `phonenumber`, not via this endpoint (see §4.4). So this endpoint only opens
> `data`/`ble` accesses.

Header: `Authorization: Bearer <JWT>`
Body (`application/x-www-form-urlencoded`):

| field         | value                      |
| ------------- | -------------------------- |
| `phonenumber` | the access's `phonenumber` |
| `access_id`   | the access's `id`          |

Response: `{ "error": 0, "state": "ok", ... }` → access opened.

> **mobipass vs Clémobil.** Clémobil used a 2G *FlashCall* (the registered
> phone dialled a number to open). With 2G being shut down (May 2026), mobipass
> replaces it with a 4G-data flow (`openmode=data`) opened through the REST API
> above. The iOS v4.4.10 binary had no "mobipass" string (only
> `clemobil`/`data`/`ble`), but the Android v4.6.4 app added a whole `mobipass`
> package — the ownership-transfer feature documented in §4.7.

### 4.6 — Other endpoints

| Method | Path                             | Purpose                                 |
| ------ | -------------------------------- | --------------------------------------- |
| GET    | `/api/calls?page=1&limit=20`     | Recent call history                     |
| POST   | `/api/access/bookmark`           | Bookmark favourite accesses (body: `BookmarkRequest[]`) |
| POST   | `/api/access/refresh`            | Refresh the access list (body: favourite id list) |
| POST   | `/api/elevator/gohome`           | KONE elevator "go home" call            |
| POST   | `/api/auth/verify`               | Phone pre-check: `verifyUser(tel, tel_ind)` → returns what the backend knows about a number (`total`, `visioactivate`, `openingaccess`, …) before onboarding |
| POST   | `/api/auth/register`             | SMS flow: register device (triggers the SMS) |
| POST   | `/api/auth/validate`             | SMS flow: validate the received code    |
| POST   | `/api/auth/vdwelling`            | Registration via an **ALLOHA** card (`registerDeviceAlloha`) |
| POST   | `/api/auth/register/idealys`     | Registration for **Idealys** hardware (`registerDeviceIdelays`) |

### 4.7 — CléMobil / Mobipass transfer (single-owner key handover)

Since ~June 2026 the remote-open key ("CléMobil", internally **Mobipass**) can
be held by only one device per phone number. Moving it to a new device needs a
one-time code (OTP) sent by SMS. Reverse-engineered from the Android
`MobipassRemoteDataSource` (Retrofit) in v4.6.4. Both calls are JWT-authed and
target `sip.intratone.info`.

Detect the need first via the `mobipass_compatible`/`mobipass` flags on
`/api/auth/device` (§4.2): eligible + not-here ⇒ transfer required.

**Step 1 — request the code (sends the SMS):**

```
POST /api/mobipass/activate
Authorization: Bearer <JWT>
(empty body — the device/number is identified by the JWT)
```

**Step 2 — verify the code (performs the transfer):**

```
POST /api/mobipass/otp/verify
Authorization: Bearer <JWT>
Content-Type: application/x-www-form-urlencoded

otp=<CODE FROM SMS>
```

Success (`error == 0`) hands the key to this device and revokes it on the
previous one; `GET /api/access` then returns the `data`/`ble` accesses again.
Failures come back as `error != 0` with a `code`:

| `code`                 | meaning                                              |
| ---------------------- | ---------------------------------------------------- |
| `MOBIPASS_OTP_INVALID` | wrong/expired code (verify step)                     |
| `MOBIPASS_CODE_BLOCKED`| too many attempts — temporarily blocked              |
| `MOBIPASS_NOT_AVAILABLE`| Mobipass not available for this account             |
| `MOBIPASS_CODE_SMS_SENT`| a code was already sent (activate step)             |

---

## 5. JWT structure

`HS512`-signed, 7-day lifetime. Claims:

```json
{
  "indicatif": "33",
  "id": "3844428",
  "tel": "0671124546",
  "code": "-1",
  "device_id": "ha-intratone-bridge-v1",
  "device": "HA-Bridge",
  "exp": 1779352440,
  "iat": 1778747640,
  "jti": "6a0588f813cf34.94237504",
  "nbf": 1778747640
}
```

### Refresh strategy

Re-call `POST /api/auth/device` with `(app_id, app_token, tel, device_id, appversion)`
whenever the JWT has < ~24h left. The endpoint always returns a fresh JWT
without requiring the user to do anything. A daily cron-style refresh is safe.

---

## 6. FCM push payload (doorbell ring)

What firebase-messaging delivers when the bell rings:

```python
{
  "fcmMessageId": "0d434d98-bb44-4e42-8a1b-87401cfcffdb",
  "from":         "676502914290",            # = messaging_sender_id
  "priority":     "normal",
  "data": {
    "ip_adress":        "178.32.84.135",      # SIP server
    "NOTIFICATION_UUID":"6a0593365af09",
    "LOGIN_TO_CALL":    "2DO77UAO49XTGJ5Y93TFIZ8YLPIMXN36",  # SIP To: URI user part
    "LOGIN":            "cogelecTest",        # SIP digest username
    "PASS":             "CogeleC",            # SIP digest password
    "TYPE":             "24",
    "message":          "PORTE RUE",          # human-readable door name
    "sound":            "fireman.mp3",
    "nbcodes":          "1",
    "DATE":             "1778750262000",      # epoch ms
    "call_id":          "300705065",          # pass to /calls/{id}/answer
    "lv":               "1",
    "NBPORTE":          "2",
    "codes":            "*"
  }
}
```

The interesting fields:

| Field            | Use                                                          |
| ---------------- | ------------------------------------------------------------ |
| `LOGIN_TO_CALL`  | SIP target: `INVITE sip:<LOGIN_TO_CALL>@178.32.84.135`       |
| `LOGIN`, `PASS`  | SIP Digest credentials (same for all users globally!)        |
| `ip_adress`      | SIP proxy IP                                                 |
| `call_id`        | Pass to `/api/calls/{id}/answer` to open the door            |
| `message`        | Door name to show in the UI                                  |
| `NBPORTE`        | Door index / position                                        |

---

## 7. SIP audio bridge

The push only notifies; voice goes via SIP/RTP. The doorbell unit registers
on `178.32.84.135:5060` as a SIP UA and is reachable by the `LOGIN_TO_CALL`
URI user part. To get a two-way audio path, your client must send an INVITE.

### SIP credentials

**Global, hardcoded in the APK**, same for every Intratone customer:

```
username = cogelecTest
password = CogeleC
```

Proxy-Authorization uses HTTP Digest (`MD5(HA1:nonce:HA2)`).

### Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant P as SIP PBX<br/>178.32.84.135:5060

    C->>P: INVITE sip:LOGIN_TO_CALL@server
    P-->>C: 407 Proxy-Auth-Required (realm, nonce)
    C->>P: ACK
    C->>P: INVITE (Proxy-Authorization: Digest ...)
    P-->>C: 100 Trying / 180 Ringing
    P-->>C: 200 OK + SDP (server RTP IP/port)
    C->>P: ACK
    Note over C,P: RTP G.711 (PCMU) bidirectional audio
```

SDP offer codecs: `PCMU/8000` (payload type 0) and `PCMA/8000` (payload type 8).
The server's 200 OK contains the `c=IN IP4 <ip>` and `m=audio <port>` of its
RTP endpoint — you receive incoming audio there and send your microphone
audio back to that endpoint on the symmetric port.

The local RTP port advertised in the offer SDP must actually be open and
listening on the client side.

---

## 8. End-to-end flow

### When the doorbell rings

1. **FCM push arrives** with `LOGIN_TO_CALL`, `call_id`, `LOGIN`, `PASS`.
2. **(Optional) SIP INVITE** to `sip:<LOGIN_TO_CALL>@178.32.84.135` with
   Digest auth `cogelecTest:CogeleC`. Bridge the resulting RTP stream
   (e.g. into go2rtc / Home Assistant camera).
3. **To open the door**: `POST /api/calls/{call_id}/answer` with
   `smartphone_id=<numeric_id>` and the current JWT.

### Maintenance

| Task                            | Trigger                                                |
| ------------------------------- | ------------------------------------------------------ |
| Refresh JWT                     | Daily, or on first 401 response                        |
| Re-register FCM token w/ Intratone | Only if you regenerate FCM credentials (re-run `/registercodes` with a new invite code) |
| Re-checkin GCM (refresh `security_token`) | Handled automatically by firebase-messaging  |

---

## 9. Gotchas & lessons learned

1. **Don't write your own MCS client.** It works for the LoginResponse but
   Google silently stops delivering pushes if you miss any of: correct proto
   field numbers (especially `device_id` as string `"android-{hex}"` in field 6,
   not the int64 in field 9 which is `compress`), `use_rmq2=true`,
   `auth_service=ANDROID_ID`, or selective ACKs of `persistent_id`s.

2. **FCM token must be bound to `bundle_id=com.cogelec.notificationpush`.**
   Tokens registered as Chrome (`org.chromium.linux`) won't receive Intratone
   pushes even if `messaging_sender_id` is correct, because Intratone targets
   the app's specific token.

3. **Re-running `/registercodes` creates a NEW device** on the apartment. The
   old one stays registered (and keeps receiving pushes) until removed in the
   official app. Each invite code is single-use.

4. **FCM payload data is nested.** `notification["data"]["call_id"]`, not
   `notification["call_id"]`.

5. **SIP digest creds are global** (`cogelecTest`/`CogeleC`). Authentication
   to a *specific* doorbell unit is done via the per-call `LOGIN_TO_CALL`
   identifier in the request URI, not via per-user SIP creds.

6. **JWT refresh = re-auth.** There's no dedicated `/refresh` endpoint;
   `/api/auth/device` returns a fresh JWT every time as long as
   `(tel, device_id)` is still registered.

7. **`openingaccess: 0` in the JWT** doesn't prevent opening the door via
   `/calls/{id}/answer`. It controls only the "Clé mobile" badge-style open
   (the `/api/access/open/clemobil` endpoint).

8. **The CléMobil key is single-owner since ~June 2026.** If `GET /api/access`
   suddenly returns nothing and `/api/auth/device` reports
   `mobipass_compatible: "1"`, `mobipass: "0"`, the key was claimed by another
   device (usually the user's phone). Recover it with the transfer flow in §4.7
   — this revokes remote opening in the official app on that other device.
