package suite.agiw.monitor;

public final class OverviewTest {
    public static void main(String[] args) {
        check(Overview.word(false, true, false, false, false, false, false) == Overview.Word.UNKNOWN, "no snapshot");
        check(Overview.word(true, true, false, false, false, false, true) == Overview.Word.UNKNOWN, "unsettled is not idle");
        check(Overview.word(true, true, false, false, false, true, false) == Overview.Word.BLOCKED, "blocked");
        check(Overview.word(true, true, false, false, false, false, false) == Overview.Word.IDLE, "fresh quiet");
        check(Overview.word(true, false, false, false, true, false, false) == Overview.Word.STALE, "stale feed");
        check(Overview.word(true, true, false, false, true, true, true) == Overview.Word.RUNNING, "live wins");
        check(!Acceptance.passClaimed(), "no pass");
        check(Acceptance.LINE.contains("ROLLBACK OPEN"), "rollback open");
        check(Acceptance.MAC_ASTRA.contains("1") || Acceptance.MAC_ASTRA.startsWith("astra1103-mac"), "mac receipt");
        check(Surface.compact(720, 748), "flip6 cover shape");
        check(!Surface.compact(1080, 2640), "flip6 inner shape");
        check(!Surface.motion(0f), "reduced motion");
        check(Surface.motion(1f), "motion allowed");
        System.out.println("android monitor-core: 13 checks passed");
    }

    private static void check(boolean value, String name) {
        if (!value) throw new AssertionError(name);
    }
}
