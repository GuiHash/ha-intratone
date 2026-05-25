# Changelog

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
