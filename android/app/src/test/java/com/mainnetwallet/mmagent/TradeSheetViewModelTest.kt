package com.mainnetwallet.mmagent

import com.mainnetwallet.mmagent.data.TradeSheetViewModel
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.math.BigDecimal

@OptIn(ExperimentalCoroutinesApi::class)
class TradeSheetViewModelTest {

    private val dispatcher = StandardTestDispatcher()
    private lateinit var vm: TradeSheetViewModel

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
        vm = TradeSheetViewModel(com.mainnetwallet.mmagent.data.ApiClient("https://example.invalid") { null })
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun orderTypeChipsFollowTheVenueCapabilities() {
        vm.setCapabilities(
            venue = "kalshi",
            orderTypes = listOf("GTC", "FAK", "FOK"),
            postOnly = true,
            tick = BigDecimal("0.01"),
            asset = "USD",
        )
        assertEquals(listOf("GTC", "FAK", "FOK"), vm.state.value.draft.orderTypeChips)
        assertFalse("GTD is not offered by Kalshi", "GTD" in vm.state.value.draft.orderTypeChips)
    }

    @Test
    fun anUnsupportedOrderTypeIsNotAccepted() {
        vm.setCapabilities("limitless", listOf("GTC", "FAK", "FOK"), false, BigDecimal("0.001"), "USDC")
        vm.setOrderType("GTD")
        assertEquals("GTC", vm.state.value.draft.orderType)
    }

    @Test
    fun postOnlyIsTurnedOffForKillOrderTypes() {
        vm.setCapabilities("kalshi", listOf("GTC", "FAK", "FOK"), true, BigDecimal("0.01"), "USD")
        vm.setPostOnly(true)
        assertTrue(vm.state.value.draft.postOnly)
        vm.setOrderType("FOK")
        assertFalse("post-only does not apply to fill-or-kill", vm.state.value.draft.postOnly)
    }

    @Test
    fun estimateShowsCostFeeMaxLossAndPayout() {
        vm.setCapabilities("kalshi", listOf("GTC"), true, BigDecimal("0.01"), "USD")
        vm.setPrice("0.57")
        vm.setShares("10")
        val estimate = vm.state.value.estimate
        assertTrue(estimate.valid)
        assertEquals("5.70", estimate.costUsd)
        assertEquals("5.70", estimate.maxLossUsd)
        assertEquals("10.00", estimate.payoutUsd)
    }

    @Test
    fun anAmountIsConvertedToShares() {
        vm.setCapabilities("kalshi", listOf("GTC"), true, BigDecimal("0.01"), "USD")
        vm.setPrice("0.50")
        vm.setAmount("5")
        assertEquals("10", vm.state.value.estimate.shares)
        assertEquals("5.00", vm.state.value.estimate.costUsd)
    }

    @Test
    fun anInvalidPriceIsRefusedWithAReason() {
        vm.setCapabilities("kalshi", listOf("GTC"), true, BigDecimal("0.01"), "USD")
        vm.setPrice("1.5")
        assertFalse(vm.state.value.estimate.valid)
        assertTrue(vm.state.value.estimate.problem!!.contains("between 0 and 1"))
    }

    @Test
    fun aNewIntentGetsANewIdempotencyKey() {
        val first = vm.state.value.idempotencyKey
        vm.newIntent()
        assertTrue(first != vm.state.value.idempotencyKey)
    }
}
