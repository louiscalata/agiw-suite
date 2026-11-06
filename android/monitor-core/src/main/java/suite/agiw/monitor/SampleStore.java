package suite.agiw.monitor;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/** The last reading the person made. The widget does not contact an observer. */
public final class SampleStore {
    private SampleStore() {}

    public static void write(Path file, Board board, long nowMillis) throws IOException {
        String text = "version=1\n"
                + "savedAt=" + nowMillis + "\n"
                + line("process", board.process)
                + line("lane", board.lane)
                + line("attachment", board.attachment)
                + line("runId", board.runId)
                + line("detail", board.detail)
                + line("connect", board.connect)
                + line("observed", board.observed);
        Files.writeString(file, text, StandardCharsets.UTF_8);
    }

    public static Board present(Path file, long nowMillis) {
        Map<String, String> fields;
        try {
            fields = read(file);
        } catch (IOException ex) {
            return Board.none("No sample yet");
        }
        if (!"1".equals(fields.get("version"))) return Board.none("No sample yet");
        long savedAt;
        try {
            savedAt = Long.parseLong(fields.getOrDefault("savedAt", ""));
        } catch (NumberFormatException ex) {
            return Board.none("No sample yet");
        }
        Board stored = Board.stored(
                fields.getOrDefault("process", "UNKNOWN"),
                fields.getOrDefault("lane", "NOT MEASURED"),
                fields.getOrDefault("attachment", "NO RUN"),
                fields.getOrDefault("runId", "not reported"),
                fields.getOrDefault("detail", ""),
                fields.getOrDefault("connect", "NONE"),
                fields.getOrDefault("observed", "not reported"));
        return present(stored, savedAt, nowMillis);
    }

    static Board present(Board stored, long savedAtMillis, long nowMillis) {
        if ("DISCONNECTED".equals(stored.connect) || "REFUSED".equals(stored.connect) || "NONE".equals(stored.connect)) {
            return stored;
        }
        long age = nowMillis - savedAtMillis;
        if (age < 0 || age > 30_000L) {
            return stored.aged("STALE", "Last sample is old. This widget did not read again.");
        }
        return stored;
    }

    private static Map<String, String> read(Path file) throws IOException {
        Map<String, String> fields = new LinkedHashMap<>();
        for (String row : Files.readString(file).split("\n")) {
            int cut = row.indexOf('=');
            if (cut <= 0) continue;
            fields.put(row.substring(0, cut), row.substring(cut + 1).replace("\\n", "\n"));
        }
        return fields;
    }

    private static String line(String key, String value) {
        String safe = value == null ? "" : value.replace("\n", " ").replace("\r", " ");
        return key + "=" + safe + "\n";
    }
}
