# Changelog

## [0.7.1](https://github.com/GuiHash/ha-intratone/compare/v0.7.0...v0.7.1) (2026-07-10)


### Bug Fixes

* **intratone:** call api/auth/verify at SMS onboarding to mirror the app ([#61](https://github.com/GuiHash/ha-intratone/issues/61)) ([fe686cd](https://github.com/GuiHash/ha-intratone/commit/fe686cd4f1374b7586ef13934849b3ba269b1bb8))
* **intratone:** send the official app's okhttp User-Agent on API requests ([#61](https://github.com/GuiHash/ha-intratone/issues/61)) ([21d426b](https://github.com/GuiHash/ha-intratone/commit/21d426bbf06d065caacbf30d3ace73a9af96f5cb))

## [0.7.0](https://github.com/GuiHash/ha-intratone/compare/v0.6.3...v0.7.0) (2026-07-10)


### Features

* **fcm:** detect push-token rotation and surface a re-pair repair ([#78](https://github.com/GuiHash/ha-intratone/issues/78)) ([b2c4157](https://github.com/GuiHash/ha-intratone/commit/b2c415795fc8652a4586afd022c26b6883873f32))


### Bug Fixes

* **mobipass:** use Android-style 16-hex device_id for CléMobil eligibility ([#81](https://github.com/GuiHash/ha-intratone/issues/81)) ([88cd50f](https://github.com/GuiHash/ha-intratone/commit/88cd50fcf71e7601f815b8ba1377c2e08af1f0f1))


### Performance Improvements

* **video:** cut stream startup latency and harden the VP8 keyframe path ([#80](https://github.com/GuiHash/ha-intratone/issues/80)) ([ba38e61](https://github.com/GuiHash/ha-intratone/commit/ba38e612bc90a935ba484e2c9b1d9dc2eff16031))

## [0.6.3](https://github.com/GuiHash/ha-intratone/compare/v0.6.2...v0.6.3) (2026-07-10)


### Bug Fixes

* **mobipass:** handle MOBIPASS_SMS_SENT and correct code constants ([#61](https://github.com/GuiHash/ha-intratone/issues/61)) ([ebb5749](https://github.com/GuiHash/ha-intratone/commit/ebb574929fe12655707bd1f1c43ef57e87b3dfaa))
* **mobipass:** register as a real phone profile to unlock CléMobil transfer ([#61](https://github.com/GuiHash/ha-intratone/issues/61)) ([95f000b](https://github.com/GuiHash/ha-intratone/commit/95f000b5ead5ef8108185be2dbcc59110b0c91d7))

## [0.6.2](https://github.com/GuiHash/ha-intratone/compare/v0.6.1...v0.6.2) (2026-07-10)


### Bug Fixes

* **mobipass:** report appversion 4.6.4 to unlock CléMobil transfer ([#61](https://github.com/GuiHash/ha-intratone/issues/61)) ([73418c9](https://github.com/GuiHash/ha-intratone/commit/73418c94c263267b8eb46b90216e9cf681628588))

## [0.6.1](https://github.com/GuiHash/ha-intratone/compare/v0.6.0...v0.6.1) (2026-07-10)


### Bug Fixes

* **mobipass:** read the access_refresh flag under its real key ([#61](https://github.com/GuiHash/ha-intratone/issues/61)) ([895de99](https://github.com/GuiHash/ha-intratone/commit/895de9955d68cc024c0bbd8624c5effc41e62183))

## [0.6.0](https://github.com/GuiHash/ha-intratone/compare/v0.5.0...v0.6.0) (2026-07-10)

### Bug Fixes

* **mobipass:** don't gate CléMobil transfer on unreliable auth flags ([#61](https://github.com/GuiHash/ha-intratone/issues/61)) ([#68](https://github.com/GuiHash/ha-intratone/issues/68)) ([5181fcc](https://github.com/GuiHash/ha-intratone/commit/5181fcc5edfa6b06322e71f59b206bbfdfae3b5c))


## 0.5.0 (2026-07-10)

Version v0.5.0 does not exist due to a versioning issue

## [0.4.1](https://github.com/GuiHash/ha-intratone/compare/v0.4.0...v0.4.1) (2026-07-10)


### Features

* **brand:** add intratone wordmark logo assets ([#65](https://github.com/GuiHash/ha-intratone/issues/65)) ([f932afd](https://github.com/GuiHash/ha-intratone/commit/f932afdd04b411b8c19c8b692c5a6ca4f7a8621c))

## [0.4.0](https://github.com/GuiHash/ha-intratone/compare/v0.3.2...v0.4.0) (2026-07-10)


### Features

* **mobipass:** CléMobil transfer flow + auto-detected repair ([#61](https://github.com/GuiHash/ha-intratone/issues/61)) ([#62](https://github.com/GuiHash/ha-intratone/issues/62)) ([82adaa4](https://github.com/GuiHash/ha-intratone/commit/82adaa48e44cf6b8a1fa5493aaddfe5684633db3))

## [0.3.2](https://github.com/GuiHash/ha-intratone/compare/v0.3.1...v0.3.2) (2026-06-28)


### Bug Fixes

* **translations:** add en.json and fix missing options labels in fr.json ([#54](https://github.com/GuiHash/ha-intratone/issues/54)) ([450cb36](https://github.com/GuiHash/ha-intratone/commit/450cb36f1621aa68193825d4a929fbbd400c01cd))

## [0.3.1](https://github.com/GuiHash/ha-intratone/compare/v0.3.0...v0.3.1) (2026-06-24)


### Bug Fixes

* **rest-api:** surface HTTP status and full body on API errors ([#52](https://github.com/GuiHash/ha-intratone/issues/52)) ([7b9114d](https://github.com/GuiHash/ha-intratone/commit/7b9114de51c7ef3b8459dd3e66e89e840b1ebe36))

## [0.3.0](https://github.com/GuiHash/ha-intratone/compare/v0.2.1...v0.3.0) (2026-06-06)


### Features

* expose mobipass remote-open accesses as lock entities ([#40](https://github.com/GuiHash/ha-intratone/issues/40)) ([0c5cb9e](https://github.com/GuiHash/ha-intratone/commit/0c5cb9e07d19054b23d03f84dda89c0c4aaefea7))


### Bug Fixes

* correct clemobil open docs and catch auth errors on access unlock ([#42](https://github.com/GuiHash/ha-intratone/issues/42)) ([a5d09df](https://github.com/GuiHash/ha-intratone/commit/a5d09dfa95f444e2b0815034dfa46525bbd369ea))

## [0.2.1](https://github.com/GuiHash/ha-intratone/compare/v0.2.0...v0.2.1) (2026-05-25)


### Bug Fixes

* **video:** drop pre-keyframe VP8 packets and add periodic PLI refresh ([#34](https://github.com/GuiHash/ha-intratone/issues/34)) ([1be8770](https://github.com/GuiHash/ha-intratone/commit/1be87701eabb795aa1bfa44ef69537831e7953e0))

## [0.2.0](https://github.com/GuiHash/ha-intratone/compare/v0.1.0...v0.2.0) (2026-05-25)


### Features

* audio-only re-INVITE on VP8 failure + richer push payload fields ([#20](https://github.com/GuiHash/ha-intratone/issues/20)) ([050a3a2](https://github.com/GuiHash/ha-intratone/commit/050a3a258bed5cb1bf19415d533257445c6dc13f))
* backlight switch + handle callCancel & unregister FCM pushes ([#19](https://github.com/GuiHash/ha-intratone/issues/19)) ([c313b97](https://github.com/GuiHash/ha-intratone/commit/c313b9735572cec5342ec156b61b28b961439377))
* expose video config in integration options UI ([#9](https://github.com/GuiHash/ha-intratone/issues/9)) ([13b1674](https://github.com/GuiHash/ha-intratone/commit/13b1674dadc2e519c443c825f0dec6b0ea937c72))
* Handle VP8 keyframe delays and improve stream timeout ([#10](https://github.com/GuiHash/ha-intratone/issues/10)) ([5acfd03](https://github.com/GuiHash/ha-intratone/commit/5acfd03aa449a69966e83dc3b41ad4b8e448fa6e))
* Implement RFC 4585 PLI feedback for faster VP8 ([#11](https://github.com/GuiHash/ha-intratone/issues/11)) ([8220a05](https://github.com/GuiHash/ha-intratone/commit/8220a050db3023d58ba9fe034fc8299bded558e1))
* Intratone integration — Phase 1 (push + open door) ([#2](https://github.com/GuiHash/ha-intratone/issues/2)) ([373e4c0](https://github.com/GuiHash/ha-intratone/commit/373e4c0310d15a4e86c95417406bd70d8bd9da28))
* phase 2 spike setup - HomeKit one-way audio dev env ([#3](https://github.com/GuiHash/ha-intratone/issues/3)) ([a3653fd](https://github.com/GuiHash/ha-intratone/commit/a3653fdd2a1d378ede0554df0a932f4716f0107a))
* Phase 3 Intratone features and refactoring ([#6](https://github.com/GuiHash/ha-intratone/issues/6)) ([3bb30c2](https://github.com/GuiHash/ha-intratone/commit/3bb30c2bc990b160265feccfffb1cbb5ecbd5191))
* send MUTE_OFF SIP MESSAGE on bridge-up to try to extend the call window ([#18](https://github.com/GuiHash/ha-intratone/issues/18)) ([30e5d36](https://github.com/GuiHash/ha-intratone/commit/30e5d36748ef62f845768cc949bdb5bec8013dd8))
* STUN responses + VP8 video forwarding for Phase 3 ([#4](https://github.com/GuiHash/ha-intratone/issues/4)) ([a6b4bba](https://github.com/GuiHash/ha-intratone/commit/a6b4bba2d0cf3e3f8db8a340dc2eadb929c21a74))


### Bug Fixes

* abort prior call on new FCM push so subsequent rings aren't dropped ([#17](https://github.com/GuiHash/ha-intratone/issues/17)) ([bfd1cae](https://github.com/GuiHash/ha-intratone/commit/bfd1cae68b139f04f2f137e1884c9e321d03b44b))
* add abort_call mock to test fixtures ([43a1c41](https://github.com/GuiHash/ha-intratone/commit/43a1c415eb52f5d7ec635876a13d35a191d7674a))
* Keep SIP call alive on stream timeout ([#12](https://github.com/GuiHash/ha-intratone/issues/12)) ([ecf02d8](https://github.com/GuiHash/ha-intratone/commit/ecf02d8137626529d38e53b786503ec9a0de87f4))


### Performance Improvements

* **ci:** switch to uv and cache apt packages ([#28](https://github.com/GuiHash/ha-intratone/issues/28)) ([f9d162b](https://github.com/GuiHash/ha-intratone/commit/f9d162b410ddd76430bbfb31f7166534aa835cab))
* optimize ffmpeg startup latency with aggressive low-latency flags ([#15](https://github.com/GuiHash/ha-intratone/issues/15)) ([ab341ab](https://github.com/GuiHash/ha-intratone/commit/ab341ab505a89381a55affafd4f74999d2970524))
