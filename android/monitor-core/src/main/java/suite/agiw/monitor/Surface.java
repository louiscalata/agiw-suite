package suite.agiw.monitor;

/**
 * Cover versus inner is a shape choice only.
 * The OS does not start this activity on the Flip 6 cover by itself.
 */
public final class Surface {
    private Surface() {}

    public static boolean compact(int widthPx, int heightPx) {
        if (widthPx <= 0 || heightPx <= 0) return false;
        int longSide = Math.max(widthPx, heightPx);
        int shortSide = Math.min(widthPx, heightPx);
        return longSide * 10 <= shortSide * 13;
    }

    public static boolean motion(float animatorScale) {
        return animatorScale > 0f;
    }
}
