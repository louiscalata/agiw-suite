package suite.agiw.monitor;

import java.nio.file.Files;
import java.nio.file.Path;

public final class OverviewTest {
    public static void main(String[] args) throws Exception {
        check(Overview.word(false, true, false, false, false, false, false) == Overview.Word.UNKNOWN, "no snapshot");
        check(Overview.word(true, true, false, false, false, false, true) == Overview.Word.UNKNOWN, "unsettled is not idle");
        check(Overview.word(true, true, false, false, false, true, false) == Overview.Word.BLOCKED, "blocked");
        check(Overview.word(true, true, false, false, false, false, false) == Overview.Word.IDLE, "fresh quiet");
        check(Overview.word(true, false, false, false, true, false, false) == Overview.Word.STALE, "stale feed");
        check(Overview.word(true, true, false, false, true, true, true) == Overview.Word.RUNNING, "live wins");
        check(!Acceptance.passClaimed(), "no pass");
        check(Acceptance.LINE.contains("ROLLBACK OPEN"), "rollback open");
        // Reflection reads the class-file constant. A direct Acceptance.LINE
        // use is inlined, so a UTF-8 test compile would hide a Cp1252 miscompile.
        String rendered;
        try {
            rendered = (String) Acceptance.class.getField("LINE").get(null);
        } catch (ReflectiveOperationException ex) {
            throw new AssertionError("acceptance line", ex);
        }
        check(rendered.indexOf('\u00B7') >= 0, "rendered acceptance contains U+00B7");
        check(rendered.indexOf('\u00C2') < 0 && rendered.indexOf('\u00C3') < 0, "rendered acceptance is not mojibake");
        check(rendered.equals(Board.none("No address").acceptance), "board renders the acceptance line");
        check(Acceptance.MAC_ASTRA.startsWith("astra1103-mac-A"), "mac receipt");
        check(Acceptance.MAC_DESK.contains("DESK"), "desk receipt");
        check(Acceptance.MAC_SYNC.contains("SYNC"), "sync receipt");
        check(Acceptance.PC_AND.contains("AND"), "android contract receipt");
        check(Acceptance.PC_WIN.contains("WIN"), "windows receipt");
        check(Acceptance.MAC_UNSIGNED.contains("317") == false && Acceptance.MAC_UNSIGNED.contains("evening-unsigned"), "unsigned is not this head");
        check(Acceptance.MAC_UNSIGNED_JEV.contains("115509"), "unsigned jev");
        check(Acceptance.PC_NODE.contains("pc-win-node"), "pc node");
        check(Acceptance.MAC_ANDJAVA.contains("ANDJAVA"), "andjava");
        check(Surface.compact(720, 748), "square window");
        check(!Surface.compact(1080, 2640), "tall window");
        check(!Surface.compact(1, Integer.MAX_VALUE), "overflow is not square");
        check(Surface.compact(Integer.MAX_VALUE, Integer.MAX_VALUE), "max square");
        check(!Surface.compact(0, 748), "empty side");
        check(Overview.word(true, true, true, false, true, false, false) == Overview.Word.STALE, "paused beats live");
        check(Overview.word(true, true, false, false, false, true, true) == Overview.Word.BLOCKED, "blocked beats unsettled");
        check(Overview.word(false, true, false, false, true, true, false) == Overview.Word.UNKNOWN, "no snapshot");
        check(Overview.word(true, true, false, true, false, false, false) == Overview.Word.IDLE, "fictional quiet");
        check(!Surface.fullyVisible(700, 748, 748, 12, 24, 0), "edge line is clipped");
        check(Surface.fullyVisible(680, 710, 748, 12, 24, 0), "line above the pad");
        check(Surface.fullyVisible(700, 748, 748, 12, 24, 40), "scroll clears the edge");
        check(!Surface.fullyVisible(10, 10, 748, 0, 0, 0), "empty line");
        check(Acceptance.notes().contains("Not a pass"), "receipts are not a pass");
        check(Acceptance.notes().contains("ROLLBACK"), "receipts keep rollback");
        Path sample = Files.createTempFile("agiw-sample", ".txt");
        Board idle = Board.read("{\"schemaVersion\":1,\"observedAt\":\"2026-11-06T20:00:00Z\",\"pipeline\":{\"status\":\"idle\",\"pipelines\":[]}}", java.time.Instant.parse("2026-11-06T20:00:00Z"));
        SampleStore.write(sample, idle, 1_000L);
        check(Files.readString(sample).equals("version=1\nsavedAt=1000\nprocess=IDLE\nlane=NOT MEASURED\nattachment=NO RUN\nrunId=not reported\ndetail=IDLE\nconnect=SAMPLE\nobserved=2026-11-06T20:00:00Z\n"), "stored format and words");
        check(idle.process.equals("IDLE") && idle.connect.equals("SAMPLE") && idle.detail.equals("IDLE"), "fresh payload words");
        long observedAt = java.time.Instant.parse(idle.observed).toEpochMilli();
        SampleStore.write(sample, idle, observedAt);
        check(SampleStore.present(sample, observedAt + 30_000L).process.equals("IDLE"), "stored age 30 seconds remains fresh");
        Board expired = SampleStore.present(sample, observedAt + 30_001L);
        check(expired.process.equals("STALE") && expired.detail.equals("Last sample is old. This widget did not read again."), "stored expiry words");
        check(SampleStore.present(sample, observedAt).process.equals("IDLE"), "fresh stored sample");
        check(SampleStore.present(sample, observedAt + 40_000L).process.equals("STALE"), "old stored sample is not read again");
        SampleStore.write(sample, Board.disconnected("Observer did not answer"), 1_000L);
        Board down = SampleStore.present(sample, 90_000L);
        check(down.connect.equals("DISCONNECTED"), "stored disconnect");
        check(down.process.equals("UNKNOWN"), "stored disconnect is not idle");
        check(SampleStore.present(Board.stored("IDLE", "NOT MEASURED", "NO RUN", "not reported", "", "NONE", "not reported"), 1_000L, 90_000L).process.equals("UNKNOWN"), "closed idle is not idle");
        check(SampleStore.present(Board.stored("RUNNING", "INVENTORY", "NO RUN", "not reported", "", "DISCONNECTED", "not reported"), 1_000L, 1_000L).process.equals("UNKNOWN"), "disconnect is not running");
        check(SampleStore.present(Board.stored("IDLE", "NOT MEASURED", "NO RUN", "not reported", "", "REFUSED", "not reported"), 1_000L, 1_000L).process.equals("UNKNOWN"), "refused is not idle");
        Board ahead = SampleStore.present(idle, observedAt, observedAt - 1L);
        check(ahead.process.equals("STALE"), "future sample is not idle");
        check(!ahead.process.equals("IDLE"), "future sample word");
        SampleStore.write(sample, idle, observedAt);
        String partial = Files.readString(sample).lines().filter(line -> !line.startsWith("connect=")).reduce("", (left, right) -> left + right + "\n");
        Files.writeString(sample, partial);
        Board missingConnect = SampleStore.present(sample, 1_000L);
        check(missingConnect.connect.equals("NONE"), "missing connect");
        check(missingConnect.process.equals("UNKNOWN"), "missing connect is not idle");
        Files.deleteIfExists(sample);
        check(!Surface.motion(0f), "reduced motion");
        check(Surface.motion(1f), "motion allowed");
        Board face = Board.stored("RUNNING", "INVENTORY", "UNATTACHED", "run-1", "RUNNING", "SAMPLE", "2026-11-06T20:00:00Z");
        check(Face.lane(true, face).equals("LANE  INVENTORY"), "cover lane prefix");
        check(Face.lane(false, face).equals("INVENTORY"), "inner lane is the field");
        check(Face.attachment(true, face).equals("ATTACH  UNATTACHED"), "cover attach");
        check(Face.attachment(false, face).equals("UNATTACHED  run-1"), "inner attach keeps the run id");
        check(Face.connect(true, face).equals("CONNECT  SAMPLE"), "cover connect");
        check(Face.connect(false, face).equals("SAMPLE"), "inner connect");
        Board kept = Board.stored(face.process, face.lane, face.attachment, face.runId, face.detail, face.connect, face.observed);
        check(Face.lane(false, kept).equals("INVENTORY"), "inner after cover does not keep the prefix");
        check(Face.attachment(false, kept).equals("UNATTACHED  run-1"), "run id survives the short window");
        check(!Face.lane(true, kept).contains("LANE  LANE"), "cover prefix is not doubled");
        System.out.println("android monitor-core: " + checks + " checks passed");
    }

    private static int checks;

    private static void check(boolean value, String name) {
        checks++;
        if (!value) throw new AssertionError(name);
    }
}
