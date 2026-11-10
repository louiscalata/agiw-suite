package suite.agiw.monitor;

public final class FoldTest {
    public static void main(String[] args) {
        check(Fold.posture(false, true, true) == Fold.Posture.FLAT, "missing hinge is flat");
        check(Fold.posture(true, false, true) == Fold.Posture.FLAT, "open hinge is flat");
        check(Fold.posture(true, true, true) == Fold.Posture.TABLETOP, "horizontal half-open is tabletop");
        check(Fold.posture(true, true, false) == Fold.Posture.BOOK, "vertical half-open is book");

        int[] table = Fold.avoid(1080, 2640, 0, 1200, 1080, 1260, Fold.Posture.TABLETOP);
        check(table[0] == 0 && table[1] == 0 && table[2] == 0, "tabletop has no side pad");
        check(table[3] == 1440, "tabletop ends the viewport at the hinge");
        int[] high = Fold.avoid(1080, 2640, 0, 100, 1080, 160, Fold.Posture.TABLETOP);
        check(high[3] == 0, "a hinge near the top is ignored");
        int[] flat = Fold.avoid(1080, 2640, 0, 1200, 1080, 1260, Fold.Posture.FLAT);
        check(flat[3] == 0, "flat adds no pad");
        check(Fold.avoid(0, 2640, 0, 10, 10, 20, Fold.Posture.TABLETOP)[3] == 0, "empty window");

        int[] book = Fold.avoid(2000, 1000, 900, 0, 1100, 1000, Fold.Posture.BOOK);
        check(book[0] == 1100 && book[2] == 0, "equal panes keep the page on the right");
        int[] bookLeft = Fold.avoid(2000, 1000, 1200, 0, 1400, 1000, Fold.Posture.BOOK);
        check(bookLeft[0] == 0 && bookLeft[2] == 800, "the wider left pane is kept");
        int[] edge = Fold.avoid(2000, 1000, 10, 0, 40, 1000, Fold.Posture.BOOK);
        check(edge[0] == 0 && edge[2] == 0, "a hinge on the edge is ignored");

        int half = Integer.MAX_VALUE / 2;
        int[] huge = Fold.avoid(Integer.MAX_VALUE, Integer.MAX_VALUE, 0, half, Integer.MAX_VALUE, half + 8, Fold.Posture.TABLETOP);
        check(huge[3] == Integer.MAX_VALUE - half, "a huge window does not overflow the hinge pad");

        check(!Surface.stackRows(1f), "default type keeps the ledger");
        check(!Surface.stackRows(1.29f), "a modest scale keeps the ledger");
        check(Surface.stackRows(1.3f), "large type stacks the rows");
        check(Surface.stackRows(2f), "largest type stacks the rows");
        System.out.println("android fold: " + checks + " checks passed");
    }

    private static int checks;

    private static void check(boolean value, String name) {
        checks++;
        if (!value) throw new AssertionError(name);
    }
}
