package au.com.gardensprinklers.launcher;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private static final String TAILSCALE_PACKAGE = "com.tailscale.ipn";
    private static final int MAX_ATTEMPTS = 6;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private TextView status;
    private Button check;
    private boolean resumed;
    private boolean openedBrowser;
    private boolean waitingForTailscale;
    private int generation;
    private int attempts;

    @Override
    public void onCreate(Bundle savedState) {
        super.onCreate(savedState);
        setContentView(R.layout.activity_main);
        View content = findViewById(R.id.content);
        int padding = Math.round(24 * getResources().getDisplayMetrics().density);
        content.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(padding + insets.getSystemWindowInsetLeft(),
                    padding + insets.getSystemWindowInsetTop(),
                    padding + insets.getSystemWindowInsetRight(),
                    padding + insets.getSystemWindowInsetBottom());
            return insets;
        });
        content.requestApplyInsets();
        if (Build.VERSION.SDK_INT >= 28) {
            findViewById(R.id.heading).setAccessibilityHeading(true);
        }
        status = findViewById(R.id.status);
        check = findViewById(R.id.check);
        ((TextView) findViewById(R.id.address)).setText(
                getString(R.string.page_address, getString(R.string.sprinkler_url)));
        if (savedState != null) {
            openedBrowser = savedState.getBoolean("openedBrowser");
            waitingForTailscale = savedState.getBoolean("waitingForTailscale");
        }
        findViewById(R.id.connect).setOnClickListener(view -> connectTailscale());
        check.setOnClickListener(view -> {
            handler.removeCallbacksAndMessages(null);
            openedBrowser = false;
            attempts = 0;
            checkPage();
        });
        findViewById(R.id.install).setOnClickListener(view -> openBrowser(
                "https://play.google.com/store/apps/details?id=" + TAILSCALE_PACKAGE));
    }

    @Override
    protected void onResume() {
        super.onResume();
        resumed = true;
        attempts = 0;
        if (openedBrowser) {
            status.setText(R.string.opened);
            check.setEnabled(true);
        } else {
            checkPage();
        }
    }

    @Override
    protected void onPause() {
        resumed = false;
        generation++;
        handler.removeCallbacksAndMessages(null);
        super.onPause();
    }

    @Override
    protected void onSaveInstanceState(Bundle state) {
        state.putBoolean("openedBrowser", openedBrowser);
        state.putBoolean("waitingForTailscale", waitingForTailscale);
        super.onSaveInstanceState(state);
    }

    @Override
    protected void onDestroy() {
        worker.shutdownNow();
        super.onDestroy();
    }

    private void connectTailscale() {
        Intent launch = getPackageManager().getLaunchIntentForPackage(TAILSCALE_PACKAGE);
        if (launch == null) {
            showTailscaleUnavailable();
            return;
        }
        generation++;
        handler.removeCallbacksAndMessages(null);
        waitingForTailscale = true;
        openedBrowser = false;
        status.setText(R.string.waiting);
        try {
            startActivity(launch);
        } catch (ActivityNotFoundException | SecurityException error) {
            waitingForTailscale = false;
            showTailscaleUnavailable();
        }
    }

    private void showTailscaleUnavailable() {
        waitingForTailscale = false;
        generation++;
        handler.removeCallbacksAndMessages(null);
        status.setText(R.string.not_installed);
        findViewById(R.id.install).setVisibility(View.VISIBLE);
        check.setEnabled(true);
    }

    private void checkPage() {
        if (!resumed) {
            return;
        }
        int request = ++generation;
        attempts++;
        check.setEnabled(false);
        status.setText(R.string.checking);
        worker.execute(() -> {
            PageProbe.Result result = probePage();
            handler.post(() -> {
                if (!resumed || request != generation) {
                    return;
                }
                check.setEnabled(true);
                if (result.reachable) {
                    waitingForTailscale = false;
                    openedBrowser = true;
                    status.setText(R.string.opened);
                    openBrowser(getString(R.string.sprinkler_url));
                } else if (waitingForTailscale && attempts < MAX_ATTEMPTS) {
                    status.setText(getString(R.string.retrying, attempts, MAX_ATTEMPTS, result.detail));
                    handler.postDelayed(this::checkPage, 2000);
                } else {
                    waitingForTailscale = false;
                    status.setText(getString(R.string.check_failed, result.detail));
                }
            });
        });
    }

    PageProbe.Result probePage() {
        return new PageProbe().check(getString(R.string.sprinkler_url));
    }

    private void openBrowser(String address) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(address))
                    .addCategory(Intent.CATEGORY_BROWSABLE));
        } catch (ActivityNotFoundException | SecurityException error) {
            openedBrowser = false;
            status.setText(R.string.no_browser);
        }
    }
}
