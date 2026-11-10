package suite.agiw.monitor;

import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

/**
 * Words for one board on the tall window or the short window.
 * The short window prefixes LANE, ATTACH, and CONNECT. The stored board
 * keeps the fields, so a later tall window does not repeat those prefixes
 * and does not drop the run id.
 */
public final class Face {
    private Face() {}

    /** Raw fields. Not the painted cover line. */
    public static Map<String, String> capture(Board board) {
        Map<String, String> out = new LinkedHashMap<>();
        out.put("process", board.process);
        out.put("lane", board.lane);
        out.put("attachment", board.attachment);
        out.put("runId", board.runId);
        out.put("detail", board.detail);
        out.put("connect", board.connect);
        out.put("observed", board.observed);
        return out;
    }

    public static Board restore(Map<String, String> saved) {
        if (saved == null) return Board.none("No address");
        return Board.stored(
                field(saved, "process", "UNKNOWN"),
                field(saved, "lane", "NOT MEASURED"),
                field(saved, "attachment", "NO RUN"),
                field(saved, "runId", "not reported"),
                field(saved, "detail", ""),
                field(saved, "connect", "NONE"),
                field(saved, "observed", "not reported"));
    }

    private static String field(Map<String, String> saved, String key, String fallback) {
        String value = saved.get(key);
        return value == null ? fallback : value;
    }

    public static String lane(boolean cover, Board board, String coverPattern) {
        return cover ? format(coverPattern, board.lane) : board.lane;
    }

    public static String attachment(boolean cover, Board board, String coverPattern, String innerPattern) {
        if (cover) return format(coverPattern, board.attachment);
        return String.format(Locale.ROOT, innerPattern, text(board.attachment), text(board.runId));
    }

    public static String connect(boolean cover, Board board, String coverPattern) {
        return cover ? format(coverPattern, board.connect) : board.connect;
    }

    private static String format(String pattern, String value) {
        return String.format(Locale.ROOT, pattern, text(value));
    }

    private static String text(String value) {
        return value == null ? "" : value;
    }
}
