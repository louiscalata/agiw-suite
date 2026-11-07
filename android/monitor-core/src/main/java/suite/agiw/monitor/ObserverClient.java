package suite.agiw.monitor;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;

/** HTTPS only. Cleartext is refused before a socket is opened. */
public final class ObserverClient {
    private final SSLContext ssl;

    public ObserverClient() {
        this(null);
    }

    /** Test builds pass a context that trusts only their fixture certificate. */
    public ObserverClient(SSLContext ssl) {
        this.ssl = ssl;
    }

    public Result getSnapshot(URI base, int timeoutMs) {
        if (base == null || base.getScheme() == null) return Result.refused("No address");
        if (!"https".equalsIgnoreCase(base.getScheme())) return Result.refused("Cleartext is not used");
        if (base.getUserInfo() != null) return Result.refused("Address has credentials");
        if (base.getHost() == null || base.getHost().isBlank()) return Result.refused("Address has no host");
        int port = base.getPort();
        URI target = URI.create(base.getScheme() + "://" + base.getHost() + (port >= 0 ? ":" + port : "") + "/api/snapshot");
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) target.toURL().openConnection();
            if (connection instanceof HttpsURLConnection https && ssl != null) https.setSSLSocketFactory(ssl.getSocketFactory());
            connection.setConnectTimeout(timeoutMs);
            connection.setReadTimeout(timeoutMs);
            connection.setInstanceFollowRedirects(false);
            connection.setRequestMethod("GET");
            int status = connection.getResponseCode();
            InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
            String body = stream == null ? "" : new String(stream.readAllBytes(), StandardCharsets.UTF_8);
            if (status != 200) return new Result(false, status, "", "HTTP " + status);
            return new Result(true, status, body, "");
        } catch (Exception ex) {
            return new Result(false, 0, "", "Observer did not answer");
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    public record Result(boolean ok, int status, String body, String detail) {
        static Result refused(String detail) {
            return new Result(false, 0, "", detail);
        }
    }
}
