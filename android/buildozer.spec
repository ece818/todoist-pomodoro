[app]
title = Todoist Pomodoro
package.name = pomodoro
package.domain = org.todoistpomodoro
source.dir = .
source.include_exts = py
version = 0.11
android.numeric_version = 11
requirements = python3,kivy,requests,certifi
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.archs = arm64-v8a
android.api = 34
android.minapi = 26
android.accept_sdk_license = True
android.backup_rules =
# S24 Ultra is arm64; single-arch keeps the build fast and the APK small.
# Debug build is sideloadable as-is; no Play signing needed.

[buildozer]
log_level = 2
warn_on_root = 0
