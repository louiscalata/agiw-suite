package suite.agiw.monitor;

import android.app.Activity;
import android.content.res.Configuration;
import android.os.Bundle;
import android.provider.Settings;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import java.net.URI;
import java.time.Instant;

/**
 * Read-only monitor. The address is typed on the tall window. The square window
 * keeps that address and shows Read without the form. There is no default host,
 * no stored credential, and no repair control. A square window is not the Flip 6 cover.
 */
public final class MonitorActivity extends Activity {
    private static final String KEY_ADDRESS = "address";
    private static final String KEY_PROCESS = "process";
    private static final String KEY_LANE = "lane";
    private static final String KEY_ATTACH = "attach";
    private static final String KEY_CONNECT = "connect";
    private static final String KEY_DETAIL = "detail";
    private static final String KEY_ACCEPT = "accept";
    private static final String KEY_OBSERVED = "observed";

    private final ObserverClient client = new ObserverClient();
    private boolean cover;
    private String addressText = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        bind(savedInstanceState);
    }

    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        Bundle kept = new Bundle();
        capture(kept);
        bind(kept);
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        capture(outState);
    }

    private void bind(Bundle state) {
        cover = compactNow();
        setContentView(cover ? R.layout.activity_cover : R.layout.activity_inner);
        float scale = Settings.Global.getFloat(getContentResolver(), Settings.Global.ANIMATOR_DURATION_SCALE, 1f);
        View rule = findViewById(R.id.schematic);
        if (rule != null) rule.setVisibility(Surface.motion(scale) ? View.VISIBLE : View.INVISIBLE);
        if (state == null) show(Board.none("No address"));
        else {
            set(R.id.process, state.getString(KEY_PROCESS, ""));
            set(R.id.lane, state.getString(KEY_LANE, ""));
            set(R.id.attachment, state.getString(KEY_ATTACH, ""));
            set(R.id.connect, state.getString(KEY_CONNECT, ""));
            set(R.id.detail, state.getString(KEY_DETAIL, ""));
            set(R.id.acceptance, state.getString(KEY_ACCEPT, Acceptance.LINE));
            set(R.id.observed, state.getString(KEY_OBSERVED, ""));
            addressText = state.getString(KEY_ADDRESS, "");
            EditText address = findViewById(R.id.address);
            if (address != null) address.setText(addressText);
        }
        Button read = findViewById(R.id.read);
        if (read != null) read.setOnClickListener(view -> readOnce());
    }

    private boolean compactNow() {
        int width = getResources().getDisplayMetrics().widthPixels;
        int height = getResources().getDisplayMetrics().heightPixels;
        return Surface.compact(width, height);
    }

    private void capture(Bundle outState) {
        EditText address = findViewById(R.id.address);
        if (address != null) addressText = address.getText().toString();
        outState.putString(KEY_ADDRESS, addressText);
        outState.putString(KEY_PROCESS, text(R.id.process));
        outState.putString(KEY_LANE, text(R.id.lane));
        outState.putString(KEY_ATTACH, text(R.id.attachment));
        outState.putString(KEY_CONNECT, text(R.id.connect));
        outState.putString(KEY_DETAIL, text(R.id.detail));
        outState.putString(KEY_ACCEPT, text(R.id.acceptance));
        outState.putString(KEY_OBSERVED, text(R.id.observed));
    }

    private void readOnce() {
        EditText address = findViewById(R.id.address);
        if (address != null) addressText = address.getText().toString().trim();
        Button read = findViewById(R.id.read);
        String raw = addressText.trim();
        if (raw.isEmpty()) {
            show(Board.none("No address"));
            return;
        }
        URI uri;
        try {
            uri = URI.create(raw);
        } catch (IllegalArgumentException ex) {
            show(Board.refused("Address was not read"));
            return;
        }
        if (read != null) read.setEnabled(false);
        new Thread(() -> {
            ObserverClient.Result result = client.getSnapshot(uri, 4000);
            Board board;
            if (result.detail().equals("Cleartext is not used") || result.detail().equals("No address")) {
                board = Board.refused(result.detail());
            } else if (!result.ok() && result.detail().equals("Observer did not answer")) {
                board = Board.disconnected(result.detail());
            } else if (!result.ok()) {
                board = Board.none(result.detail());
            } else {
                board = Board.read(result.body(), Instant.now());
            }
            runOnUiThread(() -> {
                if (isDestroyed()) return;
                show(board);
                Button again = findViewById(R.id.read);
                if (again != null) again.setEnabled(true);
            });
        }, "agiw-observer").start();
    }

    private void show(Board board) {
        set(R.id.process, "PROCESS  " + board.process);
        set(R.id.lane, "LANE  " + board.lane);
        set(R.id.attachment, "ATTACH  " + board.attachment + "  " + board.runId);
        set(R.id.connect, "CONNECT  " + board.connect);
        set(R.id.observed, "TIME  " + board.observed);
        set(R.id.detail, board.detail);
        set(R.id.acceptance, board.acceptance);
    }

    private String text(int id) {
        TextView view = findViewById(id);
        return view == null || view.getText() == null ? "" : view.getText().toString();
    }

    private void set(int id, String value) {
        TextView view = findViewById(id);
        if (view != null) view.setText(value);
    }
}
