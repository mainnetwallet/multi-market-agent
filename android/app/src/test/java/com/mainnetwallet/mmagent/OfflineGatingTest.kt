package com.mainnetwallet.mmagent

import com.mainnetwallet.mmagent.data.AppUiState
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * When the server cannot be reached, the price and the balance on screen are
 * unknown, so every money-adding and money-out action must be disabled. Cancel
 * and Pause stay available, because they reduce risk.
 */
class OfflineGatingTest {

    @Test
    fun aReachableServerAllowsTrading() {
        val state = AppUiState(loggedIn = true)
        assertTrue(state.online)
        assertFalse(state.unreachable)
    }

    @Test
    fun anUnreachableServerBlocksTrading() {
        val state = AppUiState(loggedIn = true, error = "Cannot reach the server.")
        assertFalse(state.online)
        assertTrue(state.unreachable)
    }

    @Test
    fun loadingAlsoBlocksTrading() {
        // A refresh in flight means the numbers on screen are not yet known good.
        val state = AppUiState(loggedIn = true, loading = true)
        assertFalse(state.online)
    }
}
