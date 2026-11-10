package suite.agiw.monitor;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
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
        if (base == null) return Result.refused("No address");
        if (base.getScheme() == null) return Result.refused("Type an https address");
        if (!"https".equalsIgnoreCase(base.getScheme())) return Result.refused("Cleartext is not used");
        if (base.getUserInfo() != null) return Result.refused("Address has credentials");
        String host = bracketedHost(base.getHost());
        if (host == null || host.isBlank() || "[]".equals(host)) return Result.refused("Address has no host");
        int port = base.getPort();
        URI target;
        try {
            target = URI.create(base.getScheme() + "://" + host + (port >= 0 ? ":" + port : "") + "/api/snapshot");
        } catch (IllegalArgumentException ex) {
            return Result.refused("Address was not read");
        }
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
            String body = readLimited(stream);
            if (status == 404 || status == 410) return new Result(false, status, "", "Endpoint was not found");
            if (status != 200) return new Result(false, status, "", "HTTP " + status);
            if (body == null) return new Result(false, status, "", "Payload was not read");
            return new Result(true, status, body, "");
        } catch (Exception ex) {
            return new Result(false, 0, "", "Observer did not answer");
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    /** IPv6 hosts from URI.getHost() have no brackets. The snapshot URI needs them. */
    static String bracketedHost(String host) {
        if (host == null) return null;
        if (host.indexOf(':') >= 0 && !host.startsWith("[")) return "[" + host + "]";
        return host;
    }

    /** Null means the body was larger than the cap. The caller does not keep it. */
    private static String readLimited(InputStream stream) throws IOException {
        if (stream == null) return "";
        byte[] buf = new byte[8192];
        int total = 0;
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        int read;
        while ((read = stream.read(buf)) >= 0) {
            if (total + read > 1_000_000) return null;
            out.write(buf, 0, read);
            total += read;
        }
        return new String(out.toByteArray(), StandardCharsets.UTF_8);
    }

    public record Result(boolean ok, int status, String body, String detail) {
        static Result refused(String detail) {
            return new Result(false, 0, "", detail);
        }
    }
}
