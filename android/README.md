# Beeldbuis for Android TV and phones

The TV interface from [`tv/app`](../tv/app) in a WebView, on top of a native Media3 (ExoPlayer) player.
Works with a TV remote (Android TV, Google TV, Fire TV) and with touch on a phone (landscape).

- `MainActivity.kt`: WebView + player; the page calls `BeeldbuisAndroid.play/stop/toggle/seekBy/exit`,
  the app reports back through `BBPlayerEvent(...)` and passes remote keys the WebView ignores (CH▲▼,
  media and colour keys, Back) to `BBKey(...)`.
- Provider requests from the page go through `https://appassets.androidplatform.net/proxy`, handled
  natively, because Xtream servers send no CORS headers.
- English subtitles are preferred when a stream has them.

## Build

GitHub Actions builds `Beeldbuis.apk` on every push (`.github/workflows/build.yml`); a new number in `VERSION`
on main publishes a release with the APK, the Samsung `.wgt` and the Mac update package. Locally: `gradle -p android assembleRelease`
with the Android SDK installed.

Signing: with the repository secrets `BB_KEYSTORE_BASE64` and `BB_KEYSTORE_PASSWORD` every build is signed with
the same key, so a new APK installs over the old one. Without them the build uses a throwaway debug key and an
update needs the old app uninstalled first.

## Install on Android TV

Sideload: install the *Downloader* app on the TV, allow it to install unknown apps, and open the APK's
download link from the GitHub release. On a phone: open the link and allow the browser to install apps.
