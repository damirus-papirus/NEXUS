[app]
title = NEXUS Network
package.name = nexusnetwork
package.domain = org.damiruspapirus
source.dir = .
source.include_exts = py,kv,png,jpg,atlas
version = 1.0.0
requirements = python3,kivy==2.3.1,websockets==12.0,requests
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,ACCESS_NETWORK_STATE,ACCESS_WIFI_STATE,WAKE_LOCK
android.api = 35
android.minapi = 23
android.archs = arm64-v8a,armeabi-v7a
android.allow_backup = False
p4a.branch = v2024.01.21

[buildozer]
log_level = 2
warn_on_root = 1
