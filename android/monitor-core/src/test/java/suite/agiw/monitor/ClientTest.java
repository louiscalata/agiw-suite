package suite.agiw.monitor;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.time.Instant;
import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;
import com.sun.net.httpserver.HttpsConfigurator;
import com.sun.net.httpserver.HttpsServer;

public final class ClientTest {
    public static void main(String[] args) throws Exception {
        ObserverClient client = new ObserverClient();
        ObserverClient.Result clear = client.getSnapshot(URI.create("http://127.0.0.1:9/api/snapshot"), 500);
        check(!clear.ok() && clear.detail().equals("Cleartext is not used"), "cleartext");

        Fixture fixture = Fixture.start();
        try {
            ObserverClient.Result untrusted = new ObserverClient().getSnapshot(fixture.base, 2000);
            check(!untrusted.ok(), "platform trust rejects the fixture cert");
            ObserverClient trusted = new ObserverClient(fixture.trust);
            fixture.body = "{\"schemaVersion\":1,\"observedAt\":\"" + Instant.now()
                    + "\",\"pipeline\":{\"status\":\"idle\",\"pipelines\":[]}}";
            ObserverClient.Result ok = trusted.getSnapshot(fixture.base, 2000);
            Board quiet = Board.read(ok.body(), Instant.now());
            check(ok.ok(), "https ok " + ok.detail());
            check(quiet.process.equals("IDLE"), "fresh idle " + quiet.process);
            check(quiet.lane.equals("NOT MEASURED"), "no lane");
            check(quiet.attachment.equals("NO RUN"), "no run");
            check(quiet.connect.equals("SAMPLE"), "sample");
            check(quiet.acceptance.contains("ROLLBACK OPEN"), "rollback");

            String old = Instant.now().minusSeconds(90).toString();
            fixture.body = payload(old, "idle", false);
            Board stale = Board.read(trusted.getSnapshot(fixture.base, 2000).body(), Instant.now());
            check(stale.process.equals("STALE"), "stale " + stale.process);
            check(stale.observed.equals(old), "time kept " + stale.observed);
            check(!stale.process.equals("UNKNOWN"), "stale is not unknown");

            fixture.body = payload(Instant.now().toString(), "running", true);
            Board live = Board.read(trusted.getSnapshot(fixture.base, 2000).body(), Instant.now());
            check(live.process.equals("RUNNING"), "running");
            check(live.runId.equals("run-1"), "run id");
            check(live.attachment.equals("UNATTACHED"), "unattached");

            fixture.body = "{\"schemaVersion\":1}";
            Board missingTime = Board.read(trusted.getSnapshot(fixture.base, 2000).body(), Instant.now());
            check(missingTime.process.equals("UNKNOWN"), "missing time");

            ObserverClient.Result down = trusted.getSnapshot(URI.create("https://127.0.0.1:9/"), 500);
            check(!down.ok() && down.detail().equals("Observer did not answer"), "down " + down.detail());
            Board blocked = Board.read(blockedPayload(Instant.now().toString()), Instant.now());
            check(blocked.process.equals("BLOCKED"), "blocked " + blocked.process);
            String mismatch = "{\"schemaVersion\":1,\"observedAt\":\"" + Instant.now()
                    + "\",\"pipeline\":{\"status\":\"idle\",\"pipelines\":[]},\"windowsWorker\":{\"lanes\":{\"fast\":{\"model\":\"a\",\"servedModel\":\"b\",\"up\":true}}}}";
            Board lane = Board.read(mismatch, Instant.now());
            check(lane.lane.equals("MISMATCH"), "mismatch " + lane.lane);
            check(lane.process.equals("IDLE"), "inventory is not live " + lane.process);
            check(Board.read("", Instant.now()).process.equals("UNKNOWN"), "empty");
            check(Board.read("{}", Instant.now()).process.equals("UNKNOWN"), "object");
            check(Board.read("[]", Instant.now()).process.equals("UNKNOWN"), "array");
            check(Board.read("null", Instant.now()).process.equals("UNKNOWN"), "null");
            String fresh = Instant.now().toString();
            Board partial = Board.read("{\"schemaVersion\":1,\"observedAt\":\"" + fresh
                    + "\",\"pipeline\":{\"status\":\"idle\"}}", Instant.now());
            check(partial.process.equals("UNKNOWN"), "partial idle " + partial.process);
            Board quietList = Board.read("{\"schemaVersion\":1,\"observedAt\":\"" + fresh
                    + "\",\"pipeline\":{\"status\":\"idle\",\"pipelines\":[]}}", Instant.now());
            check(quietList.process.equals("IDLE"), "explicit idle " + quietList.process);
            Board both = Board.read("{\"schemaVersion\":1,\"observedAt\":\"" + fresh
                    + "\",\"pipeline\":{\"status\":\"idle\",\"pipelines\":[{\"runId\":\"run-1\",\"status\":\"running\",\"live\":true}]},"
                    + "\"components\":[{\"id\":\"nisi\",\"state\":\"needs-action\"}]}", Instant.now());
            check(both.process.equals("RUNNING"), "live beats blocked " + both.process);
        } finally {
            fixture.server.stop(0);
        }
        System.out.println("android client: checks passed");
    }

    private static String payload(String observed, String status, boolean live) {
        return "{\"schemaVersion\":1,\"observedAt\":\"" + observed + "\",\"pipeline\":{\"status\":\""
                + status + "\",\"pipelines\":[{\"runId\":\"run-1\",\"status\":\""
                + (live ? "running" : "idle") + "\",\"live\":" + live + "}]}}";
    }

    private static String blockedPayload(String observed) {
        return "{\"schemaVersion\":1,\"observedAt\":\"" + observed
                + "\",\"pipeline\":{\"status\":\"idle\",\"pipelines\":[]},\"components\":[{\"id\":\"nisi\",\"state\":\"needs-action\"}]}";
    }

    private static void check(boolean value, String name) {
        if (!value) throw new AssertionError(name);
    }

    private static final class Fixture {
        HttpsServer server;
        URI base;
        SSLContext trust;
        volatile String body = "{}";

        static Fixture start() throws Exception {
            Path dir = Files.createTempDirectory("agiw-tls");
            Path store = dir.resolve("test.p12");
            Process tool = new ProcessBuilder("keytool", "-genkeypair", "-alias", "t", "-keyalg", "RSA", "-keysize", "2048",
                    "-storetype", "PKCS12", "-keystore", store.toString(), "-storepass", "changeit", "-keypass", "changeit",
                    "-dname", "CN=localhost", "-validity", "1", "-ext", "SAN=dns:localhost,ip:127.0.0.1")
                    .redirectErrorStream(true).start();
            String log = new String(tool.getInputStream().readAllBytes());
            if (tool.waitFor() != 0) throw new IllegalStateException(log);
            KeyStore keys = KeyStore.getInstance("PKCS12");
            try (var in = Files.newInputStream(store)) { keys.load(in, "changeit".toCharArray()); }
            KeyManagerFactory keysFactory = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
            keysFactory.init(keys, "changeit".toCharArray());
            SSLContext serverContext = SSLContext.getInstance("TLS");
            serverContext.init(keysFactory.getKeyManagers(), null, null);
            TrustManagerFactory trustFactory = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
            trustFactory.init(keys);
            SSLContext trust = SSLContext.getInstance("TLS");
            trust.init(null, trustFactory.getTrustManagers(), null);
            Fixture fixture = new Fixture();
            fixture.trust = trust;
            HttpsServer server = HttpsServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.setHttpsConfigurator(new HttpsConfigurator(serverContext));
            server.createContext("/api/snapshot", exchange -> {
                byte[] bytes = fixture.body.getBytes(java.nio.charset.StandardCharsets.UTF_8);
                exchange.getResponseHeaders().set("Content-Type", "application/json");
                exchange.sendResponseHeaders(200, bytes.length);
                try (OutputStream out = exchange.getResponseBody()) { out.write(bytes); }
            });
            server.start();
            fixture.server = server;
            fixture.base = URI.create("https://127.0.0.1:" + server.getAddress().getPort());
            return fixture;
        }
    }
}
