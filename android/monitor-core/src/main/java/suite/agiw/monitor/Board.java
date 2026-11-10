package suite.agiw.monitor;

import java.time.Instant;
import java.util.List;
import java.util.Map;

/** Read-only view of one observer payload. Missing fields stay unknown. */
public final class Board {
    public final String process;
    public final String lane;
    public final String attachment;
    public final String runId;
    public final String detail;
    public final String acceptance;
    public final String connect;
    public final String observed;

    private Board(String process, String lane, String attachment, String runId, String detail, String connect, String observed) {
        this.process = process;
        this.lane = lane;
        this.attachment = attachment;
        this.runId = runId;
        this.detail = detail;
        this.acceptance = Acceptance.LINE;
        this.connect = connect;
        this.observed = observed;
    }

    public static Board stored(String process, String lane, String attachment, String runId, String detail, String connect, String observed) {
        return new Board(process, lane, attachment, runId, detail, connect, observed);
    }

    public Board aged(String process, String detail) {
        return new Board(process, lane, attachment, runId, detail, connect, observed);
    }

    public static Board none(String detail) {
        return new Board("UNKNOWN", "NOT MEASURED", "NO RUN", "not reported", detail, "NONE", "not reported");
    }

    public static Board refused(String detail) {
        return new Board("UNKNOWN", "NOT MEASURED", "NO RUN", "not reported", detail, "REFUSED", "not reported");
    }

    public static Board disconnected(String detail) {
        return new Board("UNKNOWN", "NOT MEASURED", "NO RUN", "not reported", detail, "DISCONNECTED", "not reported");
    }

    /** A refusal stays REFUSED. Anything else that is not a sample stays DISCONNECTED. */
    public static Board from(ObserverClient.Result result, Instant now) {
        if (result == null) return none("No address");
        String detail = result.detail() == null ? "" : result.detail();
        if (refusedDetail(detail)) return refused(detail);
        if (!result.ok()) return disconnected(detail.isEmpty() ? "Observer did not answer" : detail);
        return read(result.body(), now == null ? Instant.EPOCH : now);
    }

    private static boolean refusedDetail(String detail) {
        return "Cleartext is not used".equals(detail)
                || "No address".equals(detail)
                || "Type an https address".equals(detail)
                || "Address was not read".equals(detail)
                || "Address has credentials".equals(detail)
                || "Address has no host".equals(detail);
    }

    public static Board read(String json, Instant now) {
        Object parsed;
        try {
            parsed = Json.parse(json);
        } catch (RuntimeException ex) {
            return none("Payload was not read");
        }
        if (!(parsed instanceof Map<?, ?> root)) return none("Payload was not an object");
        Object version = root.get("schemaVersion");
        if (!(version instanceof Long) || ((Long) version).longValue() != 1L) return none("Schema was not 1");
        String observedText = root.get("observedAt") instanceof String text ? text : "";
        Instant observed = instant(observedText);
        if (observed == null) return none("Observation time was not reported");
        long age = now.toEpochMilli() - observed.toEpochMilli();
        if (age < 0 || age > 30_000L) {
            Hold hold = attachment(root);
            return new Board("STALE", lane(root), hold.word, hold.runId,
                    "Sample is " + (age / 1000L) + "s from the clock used by this client. Stale is not idle and not unknown.",
                    "SAMPLE", observedText);
        }
        boolean live = liveRoute(root) || liveJob(root);
        boolean blocked = needsAction(root);
        boolean unsettled = !live && !blocked && !quiet(root);
        Overview.Word word = Overview.word(true, true, false, false, live, blocked, unsettled);
        Hold hold = attachment(root);
        return new Board(word.name(), lane(root), hold.word, hold.runId, word.name(), "SAMPLE", observedText);
    }

    private static boolean liveRoute(Map<?, ?> root) {
        Object pipeline = root.get("pipeline");
        if (!(pipeline instanceof Map<?, ?> pipe)) return false;
        Object rows = pipe.get("pipelines");
        if (!(rows instanceof List<?> list)) return false;
        for (Object row : list) {
            if (!(row instanceof Map<?, ?> item)) continue;
            if (Boolean.TRUE.equals(item.get("live")) && "running".equals(item.get("status"))) return true;
        }
        return false;
    }

    private static boolean liveJob(Map<?, ?> root) {
        Object jobs = root.get("windowsJobs");
        if (!(jobs instanceof Map<?, ?> map)) return false;
        if (!Boolean.TRUE.equals(map.get("liveAvailable"))) return false;
        Object rows = map.get("inFlight");
        return rows instanceof List<?> list && !list.isEmpty();
    }

    private static boolean needsAction(Map<?, ?> root) {
        Object rows = root.get("components");
        if (!(rows instanceof List<?> list)) return false;
        for (Object row : list) {
            if (!(row instanceof Map<?, ?> item)) continue;
            if ("nisi".equals(item.get("id")) && "needs-action".equals(item.get("state"))) return true;
        }
        return false;
    }

    private static boolean quiet(Map<?, ?> root) {
        Object pipeline = root.get("pipeline");
        if (!(pipeline instanceof Map<?, ?> pipe)) return false;
        if (!"idle".equals(pipe.get("status"))) return false;
        if (needsAction(root)) return false;
        Object rows = pipe.get("pipelines");
        if (!(rows instanceof List<?> list)) return false;
        for (Object row : list) {
            if (!(row instanceof Map<?, ?> item)) return false;
            if (!"idle".equals(item.get("status"))) return false;
            if (Boolean.TRUE.equals(item.get("live"))) return false;
        }
        return true;
    }

    private static String lane(Map<?, ?> root) {
        Object worker = root.get("windowsWorker");
        if (worker instanceof Map<?, ?> map) {
            Object lanes = map.get("lanes");
            if (lanes instanceof Map<?, ?> laneMap) {
                for (String name : new String[] {"fast", "deep"}) {
                    Object lane = laneMap.get(name);
                    if (!(lane instanceof Map<?, ?> one)) continue;
                    String expected = text(one.get("model"));
                    String served = text(one.get("servedModel"));
                    if (expected != null && served != null && !expected.equals(served)) return "MISMATCH";
                    if (Boolean.TRUE.equals(one.get("up")) && served == null) return "NOT READY";
                    if (expected != null || served != null) return "INVENTORY";
                }
            }
        }
        Object models = root.get("models");
        if (models instanceof List<?> list && !list.isEmpty()) return "INVENTORY";
        return "NOT MEASURED";
    }

    private static Hold attachment(Map<?, ?> root) {
        String runId = null;
        Object pipeline = root.get("pipeline");
        if (pipeline instanceof Map<?, ?> pipe) {
            Object rows = pipe.get("pipelines");
            if (rows instanceof List<?> list) {
                for (Object row : list) {
                    if (row instanceof Map<?, ?> item && item.get("runId") instanceof String id && !id.isBlank()) {
                        runId = id;
                        break;
                    }
                }
            }
        }
        if (runId == null) return new Hold("NO RUN", "not reported");
        return new Hold("UNATTACHED", runId);
    }

    private static String text(Object value) {
        return value instanceof String s && !s.isBlank() ? s : null;
    }

    private static Instant instant(Object value) {
        if (!(value instanceof String text) || text.isBlank()) return null;
        try {
            return Instant.parse(text);
        } catch (RuntimeException ex) {
            return null;
        }
    }

    private record Hold(String word, String runId) {}
}
