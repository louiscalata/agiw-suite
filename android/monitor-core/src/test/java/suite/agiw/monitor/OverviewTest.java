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
        check(!Surface.motion(0f), "reduced motion");
        check(Surface.motion(1f), "motion allowed");
        System.out.println("android monitor-core: 32 checks passed");
    }

    private static void check(boolean value, String name) {
        if (!value) throw new AssertionError(name);
    }
}
