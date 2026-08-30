package com.miru.companion;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotEquals;

import org.junit.Test;

public class MiruProfileStoreTest {
    @Test
    public void canonicalOriginNormalizesHostAndDefaultPort() {
        assertEquals("https://example.com",
                MiruProfileStore.canonicalOrigin("HTTPS://Example.COM:443/path/ignored"));
        assertEquals("http://example.com:5002",
                MiruProfileStore.canonicalOrigin("http://Example.COM:5002/app"));
    }

    @Test
    public void profileKeySeparatesModeServerAndUser() {
        String local = MiruProfileStore.profileKey("local", "http://127.0.0.1:5001", "u_1");
        String remote = MiruProfileStore.profileKey("remote", "http://127.0.0.1:5001", "u_1");
        String otherPort = MiruProfileStore.profileKey("remote", "http://127.0.0.1:5002", "u_1");
        String otherUser = MiruProfileStore.profileKey("remote", "http://127.0.0.1:5001", "u_2");

        assertNotEquals(local, remote);
        assertNotEquals(remote, otherPort);
        assertNotEquals(remote, otherUser);
        assertEquals(remote,
                MiruProfileStore.profileKey("REMOTE", "http://127.0.0.1:5001/", "u_1"));
    }
}
