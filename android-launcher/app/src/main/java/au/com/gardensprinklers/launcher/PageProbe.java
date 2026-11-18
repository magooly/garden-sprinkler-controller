package au.com.gardensprinklers.launcher;

import java.io.IOException;
import java.io.InputStream;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.net.UnknownHostException;
import java.nio.charset.StandardCharsets;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLException;

final class PageProbe {
    interface ConnectionFactory {
        HttpsURLConnection open(URL url) throws IOException;
    }

    static final class Result {
        final boolean reachable;
        final String detail;

        Result(boolean reachable, String detail) {
            this.reachable = reachable;
            this.detail = detail;
        }
    }

    private final ConnectionFactory connections;

    PageProbe() {
        this(url -> (HttpsURLConnection) url.openConnection());
    }

    PageProbe(ConnectionFactory connections) {
        this.connections = connections;
    }

    Result check(String address) {
        HttpsURLConnection connection = null;
        try {
            URL url = new URL(address);
            if (!"https".equals(url.getProtocol())) {
                return new Result(false, "The page address must use HTTPS.");
            }
            connection = connections.open(url);
            connection.setConnectTimeout(3000);
            connection.setReadTimeout(3000);
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setRequestMethod("GET");
            if (connection.getResponseCode() != 200) {
                return new Result(false, "The web service returned HTTP " + connection.getResponseCode() + ".");
            }
            try (InputStream input = connection.getInputStream()) {
                byte[] buffer = new byte[4096];
                int count = 0;
                while (count < buffer.length) {
                    int read = input.read(buffer, count, buffer.length - count);
                    if (read == -1) {
                        break;
                    }
                    count += read;
                }
                String html = new String(buffer, 0, count, StandardCharsets.UTF_8);
                if (!html.contains("<title>Garden Sprinklers</title>")) {
                    return new Result(false, "The address did not return the expected sprinkler page.");
                }
            }
            return new Result(true, "");
        } catch (UnknownHostException error) {
            return new Result(false, "The private computer name could not be resolved.");
        } catch (SocketTimeoutException error) {
            return new Result(false, "The connection timed out.");
        } catch (SSLException error) {
            return new Result(false, "The secure connection could not be verified. Check the phone date and network.");
        } catch (IOException error) {
            return new Result(false, "The private connection is unavailable.");
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }
}
