package au.com.gardensprinklers.launcher;

import static org.junit.Assert.*;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.net.UnknownHostException;
import java.nio.charset.StandardCharsets;
import java.security.cert.Certificate;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLException;
import org.junit.Test;

public class PageProbeTest {
    private static final String ADDRESS = "https://sprinklers.example/";

    private static class Connection extends HttpsURLConnection {
        int response = 200;
        String html = "<!doctype html><title>Garden Sprinklers</title>";
        boolean disconnected;

        Connection() throws IOException {
            super(new URL(ADDRESS));
        }

        @Override public int getResponseCode() { return response; }
        @Override public InputStream getInputStream() {
            return new ByteArrayInputStream(html.getBytes(StandardCharsets.UTF_8));
        }
        @Override public void connect() {}
        @Override public void disconnect() { disconnected = true; }
        @Override public boolean usingProxy() { return false; }
        @Override public String getCipherSuite() { return "test"; }
        @Override public Certificate[] getLocalCertificates() { return null; }
        @Override public Certificate[] getServerCertificates() { return new Certificate[0]; }
    }

    @Test public void acceptsOnlyTheExpectedPageAndUsesSafeReadOnlyRequest() throws Exception {
        Connection connection = new Connection();
        PageProbe.Result result = new PageProbe(url -> connection).check(ADDRESS);
        assertTrue(result.reachable);
        assertEquals("GET", connection.getRequestMethod());
        assertFalse(connection.getInstanceFollowRedirects());
        assertFalse(connection.getUseCaches());
        assertEquals(3000, connection.getConnectTimeout());
        assertEquals(3000, connection.getReadTimeout());
        assertTrue(connection.disconnected);
    }

    @Test public void refusesRedirectErrorsAndUnexpectedPages() throws Exception {
        for (int code : new int[]{301, 302, 401, 403, 404, 500, 503}) {
            Connection connection = new Connection();
            connection.response = code;
            PageProbe.Result result = new PageProbe(url -> connection).check(ADDRESS);
            assertFalse(result.reachable);
            assertTrue(result.detail.contains("HTTP " + code));
            assertTrue(connection.disconnected);
        }
        Connection wrongPage = new Connection();
        wrongPage.html = "<title>Sign in to Wi-Fi</title>";
        assertFalse(new PageProbe(url -> wrongPage).check(ADDRESS).reachable);
    }

    @Test public void reportsNetworkAndCertificateFailuresWithoutDisablingTls() {
        IOException[] errors = {new UnknownHostException(), new SocketTimeoutException(),
                new SSLException("certificate error"), new IOException("private details")};
        String[] messages = {"resolved", "timed out", "verified", "unavailable"};
        for (int index = 0; index < errors.length; index++) {
            IOException error = errors[index];
            PageProbe.Result result = new PageProbe(url -> { throw error; }).check(ADDRESS);
            assertFalse(result.reachable);
            assertTrue(result.detail.contains(messages[index]));
            assertFalse(result.detail.contains("private details"));
        }
    }

    @Test public void rejectsPlainHttpBeforeConnecting() {
        PageProbe probe = new PageProbe(url -> { throw new AssertionError("Must not connect"); });
        assertFalse(probe.check("http://sprinklers.example/").reachable);
        assertFalse(probe.check("not a URL").reachable);
    }

    @Test public void boundsPageReads() throws Exception {
        Connection connection = new Connection();
        connection.html = " ".repeat(4096) + "<title>Garden Sprinklers</title>";
        assertFalse(new PageProbe(url -> connection).check(ADDRESS).reachable);
        assertTrue(connection.disconnected);
    }
}
