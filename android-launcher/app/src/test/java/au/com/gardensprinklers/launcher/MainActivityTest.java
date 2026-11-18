package au.com.gardensprinklers.launcher;

import static org.junit.Assert.*;
import static org.robolectric.Shadows.shadowOf;

import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.content.pm.ApplicationInfo;
import android.content.pm.ResolveInfo;
import android.os.Looper;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.android.controller.ActivityController;
import org.robolectric.annotation.Config;
import org.robolectric.annotation.LooperMode;

@RunWith(RobolectricTestRunner.class)
@Config(sdk = {26, 35})
@LooperMode(LooperMode.Mode.PAUSED)
public class MainActivityTest {
    public static class TestActivity extends MainActivity {
        static volatile boolean reachable;
        static final AtomicInteger probes = new AtomicInteger();

        @Override PageProbe.Result probePage() {
            probes.incrementAndGet();
            return new PageProbe.Result(reachable, reachable ? "" : "Test connection unavailable.");
        }
    }

    private ActivityController<TestActivity> controller;
    private TestActivity activity;

    @Before public void create() {
        TestActivity.reachable = false;
        TestActivity.probes.set(0);
        controller = Robolectric.buildActivity(TestActivity.class).create().start().resume().visible();
        activity = controller.get();
        awaitCheck();
    }

    @After public void destroy() {
        controller.pause().stop().destroy();
    }

    private String status() {
        return ((TextView) activity.findViewById(R.id.status)).getText().toString();
    }

    private void awaitCheck() {
        long deadline = System.nanoTime() + 3_000_000_000L;
        do {
            shadowOf(Looper.getMainLooper()).idle();
            if (((Button) activity.findViewById(R.id.check)).isEnabled()) {
                return;
            }
            try {
                Thread.sleep(5);
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                throw new AssertionError(error);
            }
        } while (System.nanoTime() < deadline);
        fail("Connection check did not finish");
    }

    @Test public void unreachablePageShowsHelpWithoutOpeningControls() {
        assertTrue(status().contains("Could not reach Sprinklers"));
        assertTrue(status().contains("controller computer"));
        assertNull(shadowOf(activity).getNextStartedActivity());
    }

    @Test public void reachablePageOpensBrowserAndReturningDoesNotReopenIt() {
        TestActivity.reachable = true;
        activity.findViewById(R.id.check).performClick();
        awaitCheck();
        Intent launch = shadowOf(activity).getNextStartedActivity();
        assertNotNull(launch);
        assertEquals(Intent.ACTION_VIEW, launch.getAction());
        assertEquals(activity.getString(R.string.sprinkler_url), launch.getDataString());
        int before = TestActivity.probes.get();
        controller.pause().resume();
        shadowOf(Looper.getMainLooper()).idle();
        assertNull(shadowOf(activity).getNextStartedActivity());
        assertEquals(before, TestActivity.probes.get());
        assertTrue(status().contains("opened in your browser"));
    }

    @Test public void missingTailscaleShowsInstallButtonAndDoesNotPretendToConnect() {
        activity.findViewById(R.id.connect).performClick();
        assertTrue(status().contains("not installed or cannot be opened"));
        assertEquals(View.VISIBLE, activity.findViewById(R.id.install).getVisibility());
        assertNull(shadowOf(activity).getNextStartedActivity());
    }

    @Test public void doesNotOpenBrowserFromABackgroundCheck() {
        TestActivity.reachable = true;
        activity.findViewById(R.id.check).performClick();
        controller.pause();
        shadowOf(Looper.getMainLooper()).idle();
        assertNull(shadowOf(activity).getNextStartedActivity());
        controller.resume();
        awaitCheck();
        assertNotNull(shadowOf(activity).getNextStartedActivity());
    }

    private void installTailscale() {
        Intent intent = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
                .setPackage("com.tailscale.ipn");
        ResolveInfo info = new ResolveInfo();
        info.activityInfo = new ActivityInfo();
        info.activityInfo.name = "com.tailscale.ipn.MainActivity";
        info.activityInfo.packageName = "com.tailscale.ipn";
        info.activityInfo.applicationInfo = new ApplicationInfo();
        info.activityInfo.applicationInfo.packageName = "com.tailscale.ipn";
        shadowOf(activity.getPackageManager()).addResolveInfoForIntent(intent, info);
    }

    @Test public void opensTailscaleThenAutomaticallyOpensPageOnReturn() {
        installTailscale();
        activity.findViewById(R.id.connect).performClick();
        Intent tailscale = shadowOf(activity).getNextStartedActivity();
        assertEquals("com.tailscale.ipn", tailscale.getComponent().getPackageName());
        controller.pause();
        TestActivity.reachable = true;
        controller.resume();
        awaitCheck();
        assertEquals(activity.getString(R.string.sprinkler_url),
                shadowOf(activity).getNextStartedActivity().getDataString());
    }

    @Test public void retriesAreBoundedIfTailscaleOrComputerRemainsUnavailable() {
        installTailscale();
        activity.findViewById(R.id.connect).performClick();
        shadowOf(activity).getNextStartedActivity();
        controller.pause().resume();
        awaitCheck();
        for (int retry = 0; retry < 5; retry++) {
            shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(2));
            awaitCheck();
        }
        assertTrue(status().contains("Could not reach Sprinklers"));
        int completed = TestActivity.probes.get();
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMinutes(2));
        assertEquals(completed, TestActivity.probes.get());
        assertNull(shadowOf(activity).getNextStartedActivity());
    }
}
