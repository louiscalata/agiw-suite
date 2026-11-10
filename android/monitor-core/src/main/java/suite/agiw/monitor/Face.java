package suite.agiw.monitor;

/**
 * Words for one board on the tall window or the short window.
 * The short window prefixes LANE, ATTACH, and CONNECT. The stored board
 * keeps the fields, so a later tall window does not repeat those prefixes
 * and does not drop the run id.
 */
public final class Face {
    private Face() {}

    public static String lane(boolean cover, Board board) {
        return cover ? "LANE  " + board.lane : board.lane;
    }

    public static String attachment(boolean cover, Board board) {
        return cover ? "ATTACH  " + board.attachment : board.attachment + "  " + board.runId;
    }

    public static String connect(boolean cover, Board board) {
        return cover ? "CONNECT  " + board.connect : board.connect;
    }
}
