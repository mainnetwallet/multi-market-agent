# Android app

Native Android shell for the **multi-market-agent** PWA. It is deliberately
thin: the agent functionality, controls, charts, chat and safety confirmations
come from the deployed web app. The APK provides a launcher, persistent secure
cookies, JavaScript dialogs, back navigation and a server URL setting.

## Deploy backend to Render first

1. Push this repo to GitHub.
2. In Render, create a Blueprint from `render.yaml` (or the existing service).
3. Set `ADMIN_PASSWORD`, `SESSION_SECRET`, and optionally LLM/Tavily/Telegram/
   venue secrets.
4. Use the **HTTPS** Render URL, for example:
   `https://multi-market-agent.onrender.com`
5. Verify:
   - `HEAD https://<service>.onrender.com/health` returns 200.
   - Open the URL on your phone and log in.
6. Render free instances sleep. Use an always-on plan, otherwise trading loops
   and exit rules will not run while asleep.

The backend blueprint already uses `APP_ENV=production`, `HOST=0.0.0.0`,
`PORT=10000`, a persistent disk at `/app/data`, SQLite, and `/health`.

## Prebuilt APK

The first build is attached to the private repo release:

**Download:** https://github.com/mainnetwallet/multi-market-agent/releases/tag/v1.0.0-android  
**File:** `mm-agent-1.0.0-debug.apk`  
**SHA256:** `80FDF368D6AF7EDE121465539402A5EE5953B899B50C91FA8AF60A4D63964AA4`

This is a debug-signed sideload build, not a Play Store upload.

## Build the APK

### Option A — GitHub Actions (recommended)

`.github/workflows/android.yml` runs Android unit tests and builds a debug APK on
every `android/**` change. Open **Actions → Android APK → latest run → Artifacts**
and download `mm-agent-debug-apk`.

Optional: set a repository **variable** `ANDROID_BASE_URL` to your Render URL so
the app opens it by default. The app still shows a server settings screen if the
variable is empty.

### Option B — Android Studio / local Gradle

Requirements: JDK 17, Android SDK Platform 35, Build Tools 35.0.0.

```bash
cd android
./gradlew testDebugUnitTest assembleDebug
# Windows:
gradlew.bat testDebugUnitTest assembleDebug
```

APK: `android/app/build/outputs/apk/debug/app-debug.apk`

Build with a pre-filled Render URL:

```bash
./gradlew -PmmBaseUrl=https://your-service.onrender.com assembleDebug
```

## Install on Android

1. Copy `app-debug.apk` to the phone (USB, Drive, etc.).
2. Allow "Install unknown apps" for the file manager/browser.
3. Open the APK and install.
4. Launch **MM Agent**. On first run, enter the Render HTTPS URL.
5. Log in with the agent's `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

The app uses a normal Android WebView cookie jar and never stores venue API keys
or wallet keys. Clearing app data or using **Clear session** in the menu logs out.

## Security / production notes

- Only `https://` server URLs are accepted. Cleartext HTTP is disabled.
- SSL certificate errors cancel the load; there is no bypass.
- `target="_blank"` research links open in the external browser.
- Release builds are unsigned by default. For Play Store or a trusted sideload,
  create a keystore **outside the repo**, configure a `signingConfigs` block in
  `app/build.gradle`, build `assembleRelease`, and never commit the keystore or
  passwords.
- Debug APK is signed by the standard Android debug key and is suitable for
  sideload/testing, not Play Store.
- The APK is a shell: updating the web app on Render updates the UI immediately;
  only shell changes require a new APK.

## Known limitations

- No FCM push notifications; while the app is backgrounded, alerts are via
  Telegram/email/uptime monitoring.
- No offline trading (and there must never be offline order placement).
- Android WebView version must be reasonably current (Android 8+/WebView updated
  through Play Store). The app targets Android 15/API 35.
