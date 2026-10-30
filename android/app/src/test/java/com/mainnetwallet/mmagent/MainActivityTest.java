package com.mainnetwallet.mmagent;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

import org.junit.Test;

public class MainActivityTest {

    @Test
    public void acceptsHttpsRenderUrl() {
        assertEquals(
                "https://multi-market-agent.onrender.com",
                MainActivity.normalizeBaseUrl("https://multi-market-agent.onrender.com/"));
    }

    @Test
    public void addsHttpsScheme() {
        assertEquals(
                "https://example.onrender.com",
                MainActivity.normalizeBaseUrl("example.onrender.com"));
    }

    @Test
    public void keepsPort() {
        assertEquals(
                "https://example.com:8443",
                MainActivity.normalizeBaseUrl("https://example.com:8443/"));
    }

    @Test
    public void rejectsCleartextAndPaths() {
        assertNull(MainActivity.normalizeBaseUrl("http://example.com"));
        assertNull(MainActivity.normalizeBaseUrl("https://example.com/app"));
        assertNull(MainActivity.normalizeBaseUrl("https://example.com?x=1"));
        assertNull(MainActivity.normalizeBaseUrl("https://user:pass@example.com"));
    }
}
