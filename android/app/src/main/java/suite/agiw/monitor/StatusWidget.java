package suite.agiw.monitor;

import android.app.PendingIntent;
import android.app.AlarmManager;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.res.Configuration;
import android.widget.RemoteViews;
import java.nio.file.Path;

/** Cover-capable status. It shows the last sample and does not open a connection. */
public final class StatusWidget extends AppWidgetProvider {
    private static final String EXPIRE = "suite.agiw.monitor.DISPLAY_EXPIRE";
    static Path file(Context context) {
        return context.getFilesDir().toPath().resolve("last-sample.txt");
    }

    static void publish(Context context) {
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        ComponentName provider = new ComponentName(context, StatusWidget.class);
        int[] ids = manager.getAppWidgetIds(provider);
        long now = System.currentTimeMillis();
        Board board = SampleStore.present(file(context), now);
        for (int id : ids) manager.updateAppWidget(id, views(context, board));
        schedule(context, ids.length == 0 ? -1L : DisplayedSample.expiresAt(board, now));
    }

    @Override
    public void onUpdate(Context context, AppWidgetManager manager, int[] ids) {
        publish(context);
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        super.onReceive(context, intent);
        if (EXPIRE.equals(intent.getAction())) publish(context);
    }

    @Override
    public void onDisabled(Context context) {
        schedule(context, -1L);
    }

    private static void schedule(Context context, long expires) {
        AlarmManager alarms = (AlarmManager) context.getSystemService(Context.ALARM_SERVICE);
        Intent intent = new Intent(context, StatusWidget.class).setAction(EXPIRE);
        PendingIntent pending = PendingIntent.getBroadcast(context, 0, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        alarms.cancel(pending);
        // Android may defer this inexact alarm, especially in Doze.
        if (expires >= 0) alarms.set(AlarmManager.RTC_WAKEUP, expires, pending);
    }

    static RemoteViews views(Context context, Board board) {
        int ink = themedColor(context, R.color.agiw_ink);
        int bg = themedColor(context, R.color.agiw_bg);
        RemoteViews views = new RemoteViews(context.getPackageName(), R.layout.widget_status);
        views.setInt(R.id.widgetRoot, "setBackgroundColor", bg);
        views.setTextColor(R.id.widgetName, ink);
        views.setTextColor(R.id.widgetRule, ink);
        views.setInt(R.id.widgetRule, "setBackgroundColor", ink);
        views.setTextColor(R.id.widgetProcess, ink);
        views.setTextColor(R.id.widgetConnect, ink);
        views.setTextColor(R.id.widgetDetail, ink);
        views.setTextColor(R.id.widgetAcceptance, ink);
        views.setTextViewText(R.id.widgetProcess, board.process);
        views.setTextViewText(R.id.widgetConnect, context.getString(R.string.cover_connect, board.connect));
        views.setContentDescription(R.id.widgetRoot,
                context.getString(R.string.widget_summary, board.process, board.connect, board.acceptance));
        views.setTextViewText(R.id.widgetDetail, board.detail);
        views.setTextViewText(R.id.widgetAcceptance, board.acceptance);
        views.setContentDescription(R.id.widgetProcess, board.process);
        views.setContentDescription(R.id.widgetConnect, "Connection " + board.connect);
        views.setContentDescription(R.id.widgetDetail, board.detail.isEmpty() ? "No detail" : board.detail);
        views.setContentDescription(R.id.widgetAcceptance, board.acceptance);
        // Opens the inner activity on the normal display. The intent does not name a display.
        // One UI does not host this activity on the cover from this tap. Cover placement
        // is the widget metadata, after the person enables it under Cover screen Widgets.
        Intent open = new Intent(context, MonitorActivity.class);
        PendingIntent pending = PendingIntent.getActivity(context, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        views.setOnClickPendingIntent(R.id.widgetRoot, pending);
        return views;
    }

    /** Night and day stay the two Jaryo inks, even when the host context is stale. */
    private static int themedColor(Context context, int id) {
        boolean night = (context.getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK)
                == Configuration.UI_MODE_NIGHT_YES;
        Configuration config = new Configuration(context.getResources().getConfiguration());
        config.uiMode = (config.uiMode & ~Configuration.UI_MODE_NIGHT_MASK)
                | (night ? Configuration.UI_MODE_NIGHT_YES : Configuration.UI_MODE_NIGHT_NO);
        return context.createConfigurationContext(config).getColor(id);
    }
}
