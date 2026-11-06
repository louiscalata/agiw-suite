package suite.agiw.monitor;

/** Recorded review evidence. Not a pass, and not live telemetry. */
public final class Acceptance {
    public static final String LINE = "BLOCKED · Acceptance · PC POST ROLLBACK OPEN";
    public static final String MAC_ASTRA = "astra1103-mac-A-20261106-110643";
    public static final String PC_ASTRA = "astra1103-pc-A2-20261106-111209";

    private Acceptance() {}

    public static boolean passClaimed() {
        return false;
    }
}
