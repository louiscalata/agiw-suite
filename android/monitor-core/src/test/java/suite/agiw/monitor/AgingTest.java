package suite.agiw.monitor;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;

public final class AgingTest {
    public static void main(String[] args) throws Exception {
        long observed = Instant.parse("2026-11-06T20:00:00Z").toEpochMilli();
        Board sample = Board.read("{\"schemaVersion\":1,\"observedAt\":\"2026-11-06T20:00:00Z\",\"pipeline\":{\"status\":\"idle\",\"pipelines\":[]}}", Instant.ofEpochMilli(observed));
        String payload = "{\"schemaVersion\":1,\"observedAt\":\"2026-11-06T20:00:00Z\",\"pipeline\":{\"status\":\"idle\",\"pipelines\":[]}}";
        check(Board.read(payload, Instant.ofEpochMilli(observed + 30_000L)).process.equals("IDLE"), "parse freshness boundary");
        Board parsedStale = Board.read(payload, Instant.ofEpochMilli(observed + 30_001L));
        check(parsedStale.process.equals("STALE") && parsedStale.detail.equals("Sample is 30s from the clock used by this client. Stale is not idle and not unknown."), "parse stale words");
        check(SampleStore.present(parsedStale, observed + 30_001L, observed + 30_001L).detail.equals(parsedStale.detail), "recently saved stale detail retained");
        check(SampleStore.present(parsedStale, observed + 30_001L, observed + 60_002L).detail.equals("Last sample is old. This widget did not read again."), "old saved stale detail retained");
        Path file = Files.createTempFile("agiw-aging", ".txt");
        SampleStore.write(file, sample, observed + 29_000L);
        check(SampleStore.present(file, observed + 30_000L).process.equals("IDLE"), "widget fresh at boundary");
        Board restored = Board.stored(sample.process, sample.lane, sample.attachment, sample.runId, sample.detail, sample.connect, sample.observed);
        if (args.length == 0 || args[0].equals("widget")) {
            check(SampleStore.present(file, observed + 30_001L).process.equals("STALE"), "widget expires from observation, not save");
        }
        if (args.length == 0 || args[0].equals("restore")) {
            check(SampleStore.present(restored, observed + 30_001L, observed + 30_001L).process.equals("STALE"), "restoration cannot renew an observation");
        }
        check(DisplayedSample.state(sample, observed) == DisplayedSample.State.FRESH, "fresh state");
        check(DisplayedSample.expiresAt(sample, observed) == observed + 30_001L, "expiry deadline");
        check(DisplayedSample.state(sample, observed - 1L) == DisplayedSample.State.STALE, "future observation");
        Board stale = DisplayedSample.present(sample, observed + 30_001L, "expired", false);
        check(stale.process.equals("STALE") && stale.detail.equals("expired"), "expiry transition");
        check(DisplayedSample.present(stale, observed, "new", false).detail.equals("expired"), "stale does not revive on clock rollback");
        check(DisplayedSample.expiresAt(stale, observed) == -1L, "no repeated expiry");
        Board disconnected = Board.stored("RUNNING", sample.lane, sample.attachment, sample.runId, "old", "DISCONNECTED", sample.observed);
        check(DisplayedSample.present(disconnected, observed, "expired", false).process.equals("UNKNOWN"), "closed restoration cannot be running");
        Board invalid = Board.stored("IDLE", sample.lane, sample.attachment, sample.runId, "old", "SAMPLE", "not reported");
        check(DisplayedSample.present(invalid, observed, "expired", false).process.equals("STALE"), "missing observation time is not fresh");
        check(DisplayedSample.state(Board.disconnected("Observer did not answer"), observed + 100_000L) == DisplayedSample.State.CLOSED, "closed state does not expire");
        Files.deleteIfExists(file);
        System.out.println("android aging: " + checks + " checks passed");
    }

    private static int checks;

    private static void check(boolean condition, String message) {
        checks++;
        if (!condition) throw new AssertionError(message);
    }
}
