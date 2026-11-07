package suite.agiw.monitor;

/** One read at a time. An older reply must not replace a newer one. */
public final class ReadGate {
    private int generation;

    public synchronized int begin() {
        return ++generation;
    }

    public synchronized void cancel() {
        generation++;
    }

    public synchronized boolean accepts(int token) {
        return token > 0 && token == generation;
    }
}
