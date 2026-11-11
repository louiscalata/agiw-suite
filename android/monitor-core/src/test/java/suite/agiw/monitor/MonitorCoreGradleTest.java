package suite.agiw.monitor;

import org.junit.Test;

/**
 * Gradle discovers {@code @Test} methods. The checks themselves stay in the
 * {@code main} entry points so {@code check.sh} can run the same assertions.
 */
public final class MonitorCoreGradleTest {
    @Test
    public void overview() throws Exception {
        OverviewTest.main(new String[0]);
    }

    @Test
    public void aging() throws Exception {
        AgingTest.main(new String[0]);
    }

    @Test
    public void client() throws Exception {
        ClientTest.main(new String[0]);
    }

    @Test
    public void fold() throws Exception {
        FoldTest.main(new String[0]);
    }
}
