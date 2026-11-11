package suite.agiw.monitor;

import java.time.Instant;

/** Classifies and transitions a displayed observation without reading again. */
public final class DisplayedSample {
    public enum State { CLOSED, FRESH, STALE }

    private DisplayedSample() {}

    public static State state(Board board, long nowMillis) {
        if (!"SAMPLE".equals(board.connect)) return State.CLOSED;
        if ("STALE".equals(board.process)) return State.STALE;
        try {
            long observed = Instant.parse(board.observed).toEpochMilli();
            if (nowMillis < observed || Math.subtractExact(nowMillis, observed) > 30_000L) return State.STALE;
            return State.FRESH;
        } catch (RuntimeException ex) {
            return State.STALE;
        }
    }

    public static Board present(Board board, long nowMillis, String staleDetail, boolean updateStaleDetail) {
        switch (state(board, nowMillis)) {
            case CLOSED:
                if ("IDLE".equals(board.process) || "RUNNING".equals(board.process)) {
                    return board.aged("UNKNOWN", "A closed connection is not a live sample.");
                }
                return board;
            case STALE:
                return "STALE".equals(board.process) && !updateStaleDetail ? board : board.aged("STALE", staleDetail);
            default:
                return board;
        }
    }

    /** Returns the first millisecond at which a fresh sample becomes stale. */
    public static long expiresAt(Board board, long nowMillis) {
        if (state(board, nowMillis) != State.FRESH) return -1L;
        try {
            return Math.addExact(Instant.parse(board.observed).toEpochMilli(), 30_001L);
        } catch (RuntimeException ex) {
            return -1L;
        }
    }
}
