package com.anlandnext;

import android.app.Activity;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.ShortcutInfo;
import android.content.pm.ShortcutManager;
import android.graphics.Bitmap;
import android.graphics.drawable.Icon;
import android.os.Bundle;

/** Pin a container app shortcut from this package, so the shortcut belongs to
 *  the window host: uninstalling the launcher app does not remove the icon, and
 *  the pinned intent targets {@link AppLaunchActivity} directly.
 *
 *  Exported because the launcher app requests it, but it only forwards data to
 *  ShortcutManager — no root, no daemon call, no privileged state. All extras
 *  are length-bounded before they reach the launcher. */
public final class PinShortcutActivity extends Activity {
    public static final String EXTRA_CONTAINER = "container";
    public static final String EXTRA_USER = "user";
    public static final String EXTRA_APP_ID = "app_id";
    public static final String EXTRA_APP_NAME = "app_name";
    public static final String EXTRA_SHORTCUT_ID = "shortcut_id";
    public static final String EXTRA_ICON = "icon";

    private static final int MAX_FIELD = 128;

    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        try {
            Intent src = getIntent();
            String container = text(src, EXTRA_CONTAINER);
            String user = text(src, EXTRA_USER);
            String appId = text(src, EXTRA_APP_ID);
            String name = text(src, EXTRA_APP_NAME);
            String id = text(src, EXTRA_SHORTCUT_ID);
            Bitmap icon = src.getParcelableExtra(EXTRA_ICON);

            if (appId == null || id == null) {
                finish();
                return;
            }
            if (container == null) container = "";
            if (user == null) user = "";
            if (name == null) name = appId;

            Intent launch = new Intent();
            launch.setComponent(new ComponentName(this, AppLaunchActivity.class));
            launch.setAction(Intent.ACTION_VIEW);
            launch.putExtra(AppLaunchActivity.EXTRA_CONTAINER, container);
            launch.putExtra(AppLaunchActivity.EXTRA_USER, user);
            launch.putExtra(AppLaunchActivity.EXTRA_APP_ID, appId);
            launch.putExtra(AppLaunchActivity.EXTRA_APP_NAME, name);
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);

            ShortcutInfo.Builder b = new ShortcutInfo.Builder(this, id)
                    .setShortLabel(name)
                    .setIntent(launch);
            if (icon != null)
                b.setIcon(Icon.createWithBitmap(icon));

            ShortcutManager sm = getSystemService(ShortcutManager.class);
            if (sm != null && sm.isRequestPinShortcutSupported())
                sm.requestPinShortcut(b.build(), null);
        } catch (RuntimeException ignored) {
            /* A malformed request from any app must not crash the host. */
        } finally {
            finish();
        }
    }

    /** Null for missing or overlong values; callers decide the defaults. */
    private static String text(Intent src, String key) {
        String v = src.getStringExtra(key);
        return v == null || v.length() > MAX_FIELD ? null : v;
    }
}
