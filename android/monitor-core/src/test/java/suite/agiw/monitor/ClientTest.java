package suite.agiw.monitor;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;
import com.sun.net.httpserver.HttpsConfigurator;
import com.sun.net.httpserver.HttpsServer;

public final class ClientTest {
    public static void main(String[] args) throws Exception {
        ReadGate gate = new ReadGate();
        int first = gate.begin();
        int second = gate.begin();
        check(!gate.accepts(first), "older reply dropped");
        check(gate.accepts(second), "newer reply kept");
        gate.cancel();
        check(!gate.accepts(second), "cancelled reply dropped");
        check(!gate.accepts(0), "zero token");

        ObserverClient client = new ObserverClient();
        ObserverClient.Result clear = client.getSnapshot(URI.create("http://127.0.0.1:9/api/snapshot"), 500);
        check(!clear.ok() && clear.detail().equals("Cleartext is not used"), "cleartext");
        Board clearBoard = Board.from(clear, Instant.now());
        check(clearBoard.connect.equals("REFUSED") && clearBoard.acceptance.contains("ROLLBACK OPEN"), "cleartext stays refused");
        ObserverClient.Result bare = client.getSnapshot(URI.create("example.com"), 500);
        check(!bare.ok() && bare.detail().equals("Type an https address"), "bare host");
        check(Board.from(bare, Instant.now()).connect.equals("REFUSED"), "bare host is refused");
        check(Board.from(null, Instant.now()).detail.equals("No address"), "null result");
        ObserverClient.Result v6 = client.getSnapshot(URI.create("https://[::1]:9/"), 800);
        check(!v6.ok() && v6.detail().equals("Observer did not answer"), "ipv6 unreachable " + v6.detail());
        check(ObserverClient.bracketedHost("::1").equals("[::1]"), "ipv6 host is bracketed");
        check(ObserverClient.bracketedHost("127.0.0.1").equals("127.0.0.1"), "ipv4 host is unchanged");
        ObserverClient.Result upper = client.getSnapshot(URI.create("HTTP://127.0.0.1:9/api/snapshot"), 500);
        check(!upper.ok() && upper.detail().equals("Cleartext is not used"), "uppercase cleartext");
        ObserverClient.Result file = client.getSnapshot(URI.create("file:///etc/passwd"), 500);
        check(!file.ok() && file.detail().equals("Cleartext is not used"), "file uri");
        ObserverClient.Result opaque = client.getSnapshot(URI.create("https:example.com"), 500);
        check(!opaque.ok() && opaque.detail().equals("Address has no host"), "https without a host");
        ObserverClient.Result secret = client.getSnapshot(URI.create("https://user:secret@127.0.0.1/api/snapshot"), 500);
        check(!secret.ok() && secret.detail().equals("Address has credentials"), "typed credentials");
        check(!secret.detail().contains("secret"), "refusal does not echo the secret");

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
            Board downBoard = Board.from(down, Instant.now());
            check(downBoard.connect.equals("DISCONNECTED") && downBoard.process.equals("UNKNOWN"), "unreachable stays unknown");
            check(downBoard.acceptance.contains("ROLLBACK OPEN"), "unreachable keeps the acceptance line");
            check(Board.from(new ObserverClient.Result(false, 500, "", "HTTP 500"), Instant.now()).connect.equals("DISCONNECTED"), "http 500 is disconnected");
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

            fixture.body = "{";
            ObserverClient.Result malformed = trusted.getSnapshot(fixture.base, 2000);
            check(malformed.ok(), "malformed transport");
            check(Board.read(malformed.body(), Instant.now()).process.equals("UNKNOWN"), "malformed is unknown");

            fixture.body = "{\"schemaVersion\":1,\"observedAt\":\"" + Instant.now()
                    + "\",\"pipeline\":{\"status\":\"idle\"}}";
            Board partialFetch = Board.read(trusted.getSnapshot(fixture.base, 2000).body(), Instant.now());
            check(partialFetch.process.equals("UNKNOWN"), "partial fetch " + partialFetch.process);

            fixture.mode = "missing";
            ObserverClient.Result missing = trusted.getSnapshot(fixture.base, 2000);
            check(!missing.ok() && missing.status() == 404 && missing.detail().equals("Endpoint was not found"), "missing endpoint " + missing.detail());
            Board missingBoard = Board.from(missing, Instant.now());
            check(missingBoard.connect.equals("DISCONNECTED") && missingBoard.process.equals("UNKNOWN"), "missing endpoint is not a sample");
            check(missingBoard.acceptance.contains("ROLLBACK OPEN"), "missing endpoint keeps acceptance");

            fixture.mode = "huge";
            ObserverClient.Result huge = trusted.getSnapshot(fixture.base, 2000);
            check(!huge.ok() && huge.detail().equals("Payload was not read"), "huge payload " + huge.detail());
            check(Board.from(huge, Instant.now()).connect.equals("DISCONNECTED"), "huge payload is disconnected");

            fixture.mode = "redirect";
            ObserverClient.Result redirect = trusted.getSnapshot(fixture.base, 2000);
            check(!redirect.ok() && redirect.status() == 302, "redirect is not followed " + redirect.detail());
            check(!redirect.detail().contains("http://"), "redirect target is not fetched");

            fixture.mode = "drop";
            ObserverClient.Result dropped = trusted.getSnapshot(fixture.base, 1500);
            check(!dropped.ok() && dropped.body().isEmpty(), "fixture disconnect " + dropped.detail());
            check(!dropped.detail().equals("Cleartext is not used"), "disconnect is not a policy refusal");

            Fixture wrongHost = Fixture.start(false);
            try {
                ObserverClient named = new ObserverClient(wrongHost.trust);
                ObserverClient.Result host = named.getSnapshot(wrongHost.base, 2000);
                check(!host.ok(), "trusted certificate still checks the host");
            } finally {
                wrongHost.server.stop(0);
            }
        } finally {
            fixture.server.stop(0);
        }
        System.out.println("android client: " + checks + " checks passed");
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

    private static int checks;

    private static void check(boolean value, String name) {
        checks++;
        if (!value) throw new AssertionError(name);
    }

    private static final class Fixture {
        HttpsServer server;
        URI base;
        SSLContext trust;
        volatile String body = "{}";
        volatile String mode = "body";

        static Fixture start() throws Exception {
            return start(true);
        }

        static Fixture start(boolean san) throws Exception {
            Path dir = Files.createTempDirectory("agiw-tls");
            Path store = dir.resolve("test.p12");
            List<String> command = new ArrayList<>();
            command.add("keytool");
            command.add("-genkeypair");
            command.add("-alias");
            command.add("t");
            command.add("-keyalg");
            command.add("RSA");
            command.add("-keysize");
            command.add("2048");
            command.add("-storetype");
            command.add("PKCS12");
            command.add("-keystore");
            command.add(store.toString());
            command.add("-storepass");
            command.add("changeit");
            command.add("-keypass");
            command.add("changeit");
            command.add("-dname");
            command.add(san ? "CN=localhost" : "CN=example.invalid");
            command.add("-validity");
            command.add("1");
            if (san) {
                command.add("-ext");
                command.add("SAN=dns:localhost,ip:127.0.0.1");
            }
            Process tool = new ProcessBuilder(command).redirectErrorStream(true).start();
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
                if ("drop".equals(fixture.mode)) {
                    exchange.close();
                    return;
                }
                if ("missing".equals(fixture.mode)) {
                    exchange.sendResponseHeaders(404, -1);
                    exchange.close();
                    return;
                }
                if ("huge".equals(fixture.mode)) {
                    byte[] bytes = new byte[1_000_001];
                    exchange.sendResponseHeaders(200, bytes.length);
                    try (OutputStream out = exchange.getResponseBody()) { out.write(bytes); }
                    return;
                }
                if ("redirect".equals(fixture.mode)) {
                    exchange.getResponseHeaders().set("Location", "http://127.0.0.1/api/snapshot");
                    exchange.sendResponseHeaders(302, -1);
                    exchange.close();
                    return;
                }
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
