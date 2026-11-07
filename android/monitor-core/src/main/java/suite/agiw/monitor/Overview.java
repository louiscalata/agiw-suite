package suite.agiw.monitor;

/** Same overview rule as the desktop tabs. Idle is only a fresh, quiet sample. */
public final class Overview {
    private Overview() {}

    public enum Word {
        RUNNING, IDLE, BLOCKED, UNKNOWN, STALE
    }

    public static Word word(boolean snapshot, boolean fresh, boolean paused, boolean fictional,
                            boolean anyLive, boolean anyBlocked, boolean anyUnsettled) {
        if (!snapshot) return Word.UNKNOWN;
        if (fictional) return anyLive ? Word.RUNNING : Word.IDLE;
        if (paused || !fresh) return Word.STALE;
        if (anyLive) return Word.RUNNING;
        if (anyBlocked) return Word.BLOCKED;
        if (anyUnsettled) return Word.UNKNOWN;
        return Word.IDLE;
    }
}
