# Machine setup — agent guide (todoist-pomodoro)

Goal: take a fresh machine (codespace / Linux) from zero to ready, with slow downloads done upfront so future work is fast. No secrets in git.

## 0. What to have on hand

- Repo URL: `https://github.com/ece818/todoist-pomodoro`, branch `main`
- Todoist API token from the operator (Todoist → Settings → Integrations → Developer). Never commit it. Accepted names: `TODOIST_API_KEY` or `TODOIST_API_TOKEN` (`.env` first, env fallback).
- Expect ~10GB free for Android toolchain on first build (measured ~8.5GB: ~/.buildozer + android/.buildozer + ~/.gradle). Check: `df -h .`

## 1. Clone + Python deps

```bash
git clone https://github.com/ece818/todoist-pomodoro && cd todoist-pomodoro
python3 --version  # any 3.10+
pip install -r requirements.txt   # requests + pytest only
pip install buildozer             # only if building the APK on this machine
```

## 2. System deps

Desktop app only (skip on headless build machines):

```bash
sudo apt install -y python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```

Android builds require JDK 17 (not newer) plus python-for-android build tools
(`aclocal` missing breaks the `libffi` recipe):

```bash
sudo apt install -y openjdk-17-jdk autoconf automake libtool libtool-bin pkg-config cmake ninja-build zip unzip ccache
ls /usr/lib/jvm/  # want java-17-openjdk-amd64
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
export PATH=$JAVA_HOME/bin:$PATH
java -version  # must say 17
```

Buildozer auto-downloads the Android SDK/NDK + python-for-android bootstrap on first run. That is the slow step — do it in step 5, not during real work.

## 3. Secret (.env)

Operator pastes the token; agent writes it, never prints it:

```bash
echo 'TODOIST_API_KEY=<token-from-operator>' > .env
chmod 600 .env
```

Verify only the key name, never the value: `cut -d= -f1 .env`.

## 4. Verify (fast, no network to device needed)

```bash
python3 -m pytest tests/ -q
python3 web/server.py --port 8000 & sleep 2; curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000; kill %1
sh android/sync_backend.sh && git status --short android/backend/  # must be clean if backend unchanged
```

Desktop smoke (only where GTK is installed): `python3 -c "import gi; print('gtk ok')"`.

## 5. Pre-warm for speed (do once, when nobody is waiting)

This is what makes the next session fast:

```bash
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
export PATH=$JAVA_HOME/bin:$PATH
cd android && buildozer android debug  # 15-30 min cold; downloads SDK/NDK/p4a/Gradle. Subsequent builds ~5-10 min.
```

Keep the caches on the machine: `~/.buildozer/`, `android/.buildozer/`, `~/.gradle/`, pip wheel cache. Do not delete them to "save space" — that forces another cold build.

Signing identity: debug APKs are signed with `~/.android/debug.keystore`, auto-created on first build. Android refuses to update an installed app when the signer differs, so a new machine's key breaks updates — carry this single file between machines (copy it directly like the APK, never commit it) and place it at `~/.android/debug.keystore` before the first build there.

## 6. Notes / gotchas

- `android/bin/*.apk` and `.buildozer/` are gitignored — APKs never transfer via git. Rebuild or copy the single APK file directly.
- Spec is pinned: single arch `arm64-v8a`, API 34, min API 26 (`android/buildozer.spec`). Debug APK is sideloadable, no signing needed.
- Backend is shared: after changing `config.py`, `todoist.py`, `sync.py`, `sessions.py`, or `actions.py`, run `sh android/sync_backend.sh`.
- Machine sizing: 2-core/8GB is enough (repo is ~4MB + git). 4-core/16GB gives ~30-50% faster clean builds via cores, not RAM — and its first build is still slow (cold cache). Only move machines between tasks, never mid-build.
