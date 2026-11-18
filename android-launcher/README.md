# Garden Sprinklers Android launcher

A small, personal Android launcher for the existing private sprinkler website.
It does not replace Tailscale or the phone web interface, and never sends
sprinkler-control commands.

## Using it

1. Install the signed `Garden-Sprinklers-Launcher-1.0.0.apk` on your phone.
   Android may ask you to allow installation from the app opening the APK.
   Allow only that source, and turn the permission off again afterwards.
2. Open **Garden Sprinklers**. If the private page is reachable, it opens in
   your normal browser immediately.
3. Otherwise, tap **Connect Tailscale**. In the actual Tailscale app, connect
   or complete sign-in as needed.
4. Return to Garden Sprinklers using Android Back or Recent apps. The launcher
   automatically checks again, with up to six attempts over approximately
   one minute, and opens the page when reachable.
5. If it still cannot connect, check Tailscale, the controller computer and
   the phone web service. Tap **Check connection / Open Sprinklers** to retry.

If Tailscale is missing, **Get Tailscale** opens its Google Play listing.
The launcher cannot silently sign in, turn on another app's VPN, or distinguish
a disconnected VPN from a stopped computer using a failed page request alone.
Returning from the browser does not immediately reopen it in a loop.

The existing sprinkler web password is still required. It is entered only in
your browser; the launcher stores no passwords or VPN keys and uses no WebView,
analytics, cloud relay, or background service. It checks only the HTTPS home
page, validates the normal TLS certificate and expected page title, refuses
redirects, and opens controls only after a successful check.

## Address and compatibility

The personal build opens `https://ubuntu120gb.tail6afeba.ts.net/`.
The address is set by `sprinkler_url` in [app/build.gradle](app/build.gradle).
For another installation, change it to that installation's private HTTPS
Tailscale Serve address and rebuild. The expected home-page title is
`Garden Sprinklers`.

Requires Android 8.0 or later, Tailscale installed and signed into the correct
tailnet, and an enabled web browser. It works over Wi-Fi or mobile data when
Tailscale can reach the controller. No public router ports or Tailscale Funnel
are required. The only Android permission requested is Internet access.

## Building and tests

Use Java 17 and the Android SDK, with platform 35 and build tools 35.0.0.
Review and accept Google's SDK licences through the official SDK manager.
There are no third-party runtime libraries.

```sh
export ANDROID_HOME="$HOME/Android/Sdk"
./gradlew --no-daemon testDebugUnitTest lintRelease assembleRelease
```

The Gradle wrapper is pinned to 8.11.1 with a distribution checksum.
Tests cover HTTPS page identity, redirects, failures, bounded reads,
opening Tailscale, returning and opening the browser, retry limits,
and avoiding browser reopen loops. Android UI tests use Robolectric;
they do not require or control the real phone.

The release build produces an unsigned APK under
`app/build/outputs/apk/release/`. Align and sign it before distribution using
the Android SDK's `zipalign` and `apksigner` tools. Keep the signing keystore and
its password outside the repository. Use the same key for future updates:
Android will not update an installed package with a differently signed APK.
Never commit a keystore, password, or generated APK.

The personal release key is kept on the controller computer in the private
`~/.config/garden-sprinkler-launcher/` directory, not in this project. Losing
it means a future differently signed build requires uninstalling the old app
first. This directory is not included in the app's portable `.sprinkler-backup`
exports. Refreshed complete local encrypted `.tar.gz.gpg` recovery archives
include it and the signed APK when present; neither is published to GitHub.

Building or using this launcher does not restart or modify the sprinkler
controller, schedules, Tailscale account, or phone web service.
