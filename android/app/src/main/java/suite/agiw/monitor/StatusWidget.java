package suite.agiw.monitor;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.appwidget.AppWidgetProviderInfo;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.res.Configuration;
import android.os.Bundle;
import android.view.View;
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

    @Override
    public void onAppWidgetOptionsChanged(Context context, AppWidgetManager manager, int id, Bundle options) {
        render(context, manager, id);
    }

    private static void render(Context context, AppWidgetManager manager, int id) {
        Board board = SampleStore.present(file(context), System.currentTimeMillis());
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
        if (keyguard(manager, id)) {
            String locked = context.getString(R.string.widget_locked);
            views.setTextViewText(R.id.widgetProcess, locked);
            views.setTextViewText(R.id.widgetConnect, "");
            views.setTextViewText(R.id.widgetDetail, "");
            views.setTextViewText(R.id.widgetAcceptance, "");
            views.setViewVisibility(R.id.widgetConnect, View.GONE);
            views.setViewVisibility(R.id.widgetDetail, View.GONE);
            views.setViewVisibility(R.id.widgetAcceptance, View.GONE);
            views.setContentDescription(R.id.widgetProcess, context.getString(R.string.widget_locked_description));
            views.setContentDescription(R.id.widgetConnect, "");
            views.setContentDescription(R.id.widgetDetail, "");
            views.setContentDescription(R.id.widgetAcceptance, "");
        } else {
            views.setTextViewText(R.id.widgetProcess, board.process);
            views.setTextViewText(R.id.widgetConnect, context.getString(R.string.cover_connect, board.connect));
            views.setTextViewText(R.id.widgetDetail, board.detail);
            views.setTextViewText(R.id.widgetAcceptance, board.acceptance);
            views.setContentDescription(R.id.widgetProcess, context.getString(R.string.desc_process, board.process));
            views.setContentDescription(R.id.widgetConnect, context.getString(R.string.desc_connect, board.connect));
            views.setContentDescription(R.id.widgetDetail, context.getString(R.string.desc_detail,
                    board.detail.isEmpty() ? context.getString(R.string.widget_no_detail) : board.detail));
            views.setContentDescription(R.id.widgetAcceptance, context.getString(R.string.desc_acceptance, board.acceptance));
        }
        // The root stays unlabeled. A summary there plus these rows is spoken twice.
        // Opens the inner activity on the normal display. The intent does not name a display.
        // One UI does not host this activity on the cover from this tap. Cover placement
        // is the widget metadata, after the person enables it under Cover screen Widgets.
        Intent open = new Intent(context, MonitorActivity.class);
        PendingIntent pending = PendingIntent.getActivity(context, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        views.setOnClickPendingIntent(R.id.widgetRoot, pending);
        manager.updateAppWidget(id, views);
    }

    /** Home and keyguard share one provider. The lock screen does not show the pipeline. */
    private static boolean keyguard(AppWidgetManager manager, int id) {
        Bundle options = manager.getAppWidgetOptions(id);
        int category = options.getInt(AppWidgetManager.OPTION_APPWIDGET_HOST_CATEGORY, -1);
        return category == AppWidgetProviderInfo.WIDGET_CATEGORY_KEYGUARD;
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
