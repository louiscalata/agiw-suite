package suite.agiw.monitor;

/** Recorded review evidence. Not a pass, and not live telemetry. */
public final class Acceptance {
    public static final String LINE = "BLOCKED · Acceptance · PC POST ROLLBACK OPEN";
    public static final String MAC_ASTRA = "astra1103-mac-A-20261106-110643";
    public static final String PC_ASTRA = "astra1103-pc-A2-20261106-111209";
    public static final String MAC_DESK = "astra1103-mac-DESK-20261106-114103";
    public static final String MAC_SYNC = "astra1103-mac-SYNC-20261106-114919";
    public static final String PC_AND = "astra1103-pc-AND-20261106-114125";
    public static final String PC_WIN = "astra1103-pc-WIN-20261106-114930";
    public static final String MAC_UNSIGNED = "evening-unsigned-20261106-115434";
    public static final String MAC_UNSIGNED_JEV = "mac-unsigned-build-20261106-115509";
    public static final String PC_NODE = "pc-win-node-20261106";
    public static final String MAC_ANDJAVA = "astra1103-mac-ANDJAVA-20261106-115608";

    private Acceptance() {}

    public static boolean passClaimed() {
        return false;
    }

    public static String notes() {
        return "Recorded receipts. Not a pass.\n"
                + "Mac unsigned " + MAC_UNSIGNED_JEV + " is 0/4.\n"
                + "PC POST ROLLBACK remains open.\n"
                + MAC_ASTRA + "\n"
                + PC_ASTRA + "\n"
                + MAC_DESK + "\n"
                + MAC_SYNC + "\n"
                + PC_AND + "\n"
                + PC_WIN + "\n"
                + PC_NODE + "\n"
                + MAC_ANDJAVA;
    }
}
