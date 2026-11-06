package suite.agiw.monitor;

import android.app.PendingIntent;
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
    static Path file(Context context) {
        return context.getFilesDir().toPath().resolve("last-sample.txt");
    }

    static void publish(Context context) {
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        ComponentName provider = new ComponentName(context, StatusWidget.class);
        int[] ids = manager.getAppWidgetIds(provider);
        for (int id : ids) render(context, manager, id);
    }

    @Override
    public void onUpdate(Context context, AppWidgetManager manager, int[] ids) {
        for (int id : ids) render(context, manager, id);
    }

    private static void render(Context context, AppWidgetManager manager, int id) {
        Board board = SampleStore.present(file(context), System.currentTimeMillis());
        boolean night = (context.getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK)
                == Configuration.UI_MODE_NIGHT_YES;
        int ink = night ? 0xFFFFFFFF : 0xFF000000;
        int bg = night ? 0xFF000000 : 0xFFFFFFFF;
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
        views.setTextViewText(R.id.widgetConnect, "CONNECT  " + board.connect);
        views.setTextViewText(R.id.widgetDetail, board.detail);
        views.setTextViewText(R.id.widgetAcceptance, board.acceptance);
        Intent open = new Intent(context, MonitorActivity.class);
        PendingIntent pending = PendingIntent.getActivity(context, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        views.setOnClickPendingIntent(R.id.widgetRoot, pending);
        manager.updateAppWidget(id, views);
    }
}
