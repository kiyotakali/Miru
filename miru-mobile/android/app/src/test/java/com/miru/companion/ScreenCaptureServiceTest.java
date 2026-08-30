package com.miru.companion;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class ScreenCaptureServiceTest {

    @Test
    public void responseTimeoutAfterCompleteBodyMustNotRetrySameFrame() {
        assertTrue(ScreenCaptureService.timeoutMeansServerAccepted(true));
    }

    @Test
    public void timeoutBeforeCompleteBodyRemainsRetryable() {
        assertFalse(ScreenCaptureService.timeoutMeansServerAccepted(false));
    }
}
