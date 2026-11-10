package suite.agiw.monitor;

/**
 * Hinge padding for one scrolling page.
 * Tabletop (a horizontal half-open hinge, the Flip flex posture) ends the
 * viewport at the hinge. Book (a vertical half-open hinge) keeps the page in
 * the wider pane. A flat window adds nothing. The Flip cover is a separate
 * display and usually has no hinge; this class does not claim a cover host.
 */
public final class Fold {
    public enum Posture {
        FLAT,
        TABLETOP,
        BOOK
    }

    private Fold() {}

    public static Posture posture(boolean featurePresent, boolean halfOpened, boolean horizontal) {
        if (!featurePresent || !halfOpened) return Posture.FLAT;
        return horizontal ? Posture.TABLETOP : Posture.BOOK;
    }

    /**
     * Extra pixels beyond the page inset, in window coordinates: left, top, right, bottom.
     * A hinge outside the middle band is ignored so a bad rect cannot pad the page away.
     */
    public static int[] avoid(int width, int height, int left, int top, int right, int bottom, Posture posture) {
        int[] none = {0, 0, 0, 0};
        if (posture == null || width <= 0 || height <= 0 || right <= left || bottom <= top) return none;
        return switch (posture) {
            case FLAT -> none;
            case TABLETOP -> tabletop(height, top);
            case BOOK -> book(width, left, right);
        };
    }

    private static int[] tabletop(int height, int top) {
        int hingeTop = clamp(top, 0, height);
        if (outside(hingeTop, hingeTop, height)) return new int[] {0, 0, 0, 0};
        return new int[] {0, 0, 0, height - hingeTop};
    }

    private static int[] book(int width, int left, int right) {
        int hingeLeft = clamp(left, 0, width);
        int hingeRight = clamp(right, 0, width);
        if (outside(hingeLeft, hingeRight, width)) return new int[] {0, 0, 0, 0};
        int leftPane = hingeLeft;
        int rightPane = width - hingeRight;
        if (rightPane >= leftPane) return new int[] {hingeRight, 0, 0, 0};
        return new int[] {0, 0, width - hingeLeft, 0};
    }

    /** True when the band does not sit in the middle 60 percent of the span. */
    private static boolean outside(int start, int end, int span) {
        long low = (long) span * 2L / 10L;
        long high = (long) span * 8L / 10L;
        return start < low || end > high;
    }

    private static int clamp(int value, int min, int max) {
        return Math.max(min, Math.min(max, value));
    }
}
