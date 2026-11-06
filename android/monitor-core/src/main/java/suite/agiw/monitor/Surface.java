package suite.agiw.monitor;

/**
 * A nearly square window uses the short layout. That is the window aspect,
 * not a hinge angle and not evidence the Flip 6 cover launched this activity.
 * Missing fold information stays an ordinary window choice.
 */
public final class Surface {
    private Surface() {}

    public static boolean compact(int widthPx, int heightPx) {
        if (widthPx <= 0 || heightPx <= 0) return false;
        long longSide = Math.max(widthPx, heightPx);
        long shortSide = Math.min(widthPx, heightPx);
        return longSide * 10L <= shortSide * 13L;
    }

    public static boolean motion(float animatorScale) {
        return animatorScale > 0f;
    }
}
