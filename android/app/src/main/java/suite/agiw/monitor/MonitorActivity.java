package suite.agiw.monitor;

import android.app.Activity;
import android.os.Bundle;
import android.provider.Settings;
import android.view.View;
import android.widget.TextView;

/**
 * Read-only monitor. No credentials, no network, and no invented telemetry.
 * A nearly square window uses the cover layout. Samsung does not launch
 * this activity on the Flip 6 cover unless the person allows it.
 */
public final class MonitorActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        int width = getResources().getDisplayMetrics().widthPixels;
        int height = getResources().getDisplayMetrics().heightPixels;
        setContentView(Surface.compact(width, height) ? R.layout.activity_cover : R.layout.activity_inner);
        float scale = Settings.Global.getFloat(getContentResolver(), Settings.Global.ANIMATOR_DURATION_SCALE, 1f);
        View rule = findViewById(R.id.schematic);
        if (rule != null) rule.setVisibility(Surface.motion(scale) ? View.VISIBLE : View.INVISIBLE);
        Overview.Word process = Overview.word(false, false, false, false, false, false, false);
        set(R.id.process, "PROCESS  " + process.name());
        set(R.id.lane, "LANE  NOT MEASURED");
        set(R.id.attachment, "ATTACH  NO RUN");
        set(R.id.acceptance, Acceptance.LINE);
    }

    private void set(int id, String value) {
        TextView view = findViewById(id);
        if (view != null) view.setText(value);
    }
}
