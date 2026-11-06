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

    /** The child sits fully inside the padded viewport. A line on the edge does not. */
    public static boolean fullyVisible(int childTop, int childBottom, int viewHeight, int padTop, int padBottom, int scrollY) {
        if (childBottom <= childTop || viewHeight <= 0) return false;
        int visibleTop = scrollY;
        int visibleBottom = scrollY + viewHeight - Math.max(0, padTop) - Math.max(0, padBottom);
        return childTop >= visibleTop && childBottom <= visibleBottom;
    }

    public static boolean motion(float animatorScale) {
        return animatorScale > 0f;
    }
}
