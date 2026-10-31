package com.mainnetwallet.mmagent

import com.mainnetwallet.mmagent.data.OutcomeBook
import com.mainnetwallet.mmagent.data.TradeSheetViewModel
import com.mainnetwallet.mmagent.data.heldShares
import com.mainnetwallet.mmagent.ui.BookLevel
import org.json.JSONObject
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

    private fun level(price: String, shares: String) = BookLevel(BigDecimal(price), BigDecimal(shares))

    private fun openPolymarketTicket() {
        vm.setCapabilities("polymarket", listOf("GTC", "GTD", "FAK", "FOK"), true, BigDecimal("0.01"), "pUSD")
        vm.startTicket("YES")
        vm.setBooks(
            mapOf(
                "YES" to OutcomeBook(
                    bids = listOf(level("0.58", "20"), level("0.55", "100")),
                    asks = listOf(level("0.60", "10"), level("0.70", "100")),
                ),
                "NO" to OutcomeBook(
                    bids = listOf(level("0.30", "100")),
                    asks = listOf(level("0.42", "100")),
                ),
            )
        )
    }

    @Test
    fun aNewTicketStartsAsAMarketBuyOnTheTappedSide() {
        openPolymarketTicket()
        val draft = vm.state.value.draft
        assertEquals("BUY", draft.side)
        assertEquals("FAK", draft.orderType)
        assertTrue(draft.isMarketOrder)
        assertEquals("YES", draft.outcome)
    }

    @Test
    fun aVenueWithoutMarketOrdersStartsOnLimit() {
        vm.setCapabilities("limitless-like", listOf("GTC"), false, BigDecimal("0.001"), "USDC")
        vm.startTicket("NO")
        assertEquals("GTC", vm.state.value.draft.orderType)
        assertFalse(vm.state.value.draft.supportsMarket)
    }

    @Test
    fun aMarketBuyWalksTheAsksAndShowsWhatItWins() {
        openPolymarketTicket()
        vm.setAmount("12")
        val estimate = vm.state.value.estimate
        assertTrue(estimate.valid)
        // $6 buys 10 shares at 60c, the other $6 buys 8.571428 at 70c.
        assertEquals("18.57", estimate.shares)
        assertEquals("12.00", estimate.costUsd)
        assertEquals("18.57", estimate.payoutUsd)
        assertEquals("6.57", estimate.returnUsd)
        assertEquals("0.6462", estimate.avgPrice)
    }

    @Test
    fun aMarketBuyAskedForMoreThanTheBookHoldsSaysSo() {
        openPolymarketTicket()
        vm.setAmount("1000")
        val estimate = vm.state.value.estimate
        assertTrue(estimate.valid)
        assertTrue(estimate.note!!.contains("can be filled"))
    }

    @Test
    fun aMarketBuyWithNothingTypedAsksForAnAmount() {
        openPolymarketTicket()
        assertFalse(vm.state.value.estimate.valid)
        assertEquals("Enter an amount.", vm.state.value.estimate.problem)
    }

    @Test
    fun aMarketSellWalksTheBids() {
        openPolymarketTicket()
        vm.setSide("SELL")
        vm.setShares("30")
        val estimate = vm.state.value.estimate
        assertTrue(estimate.valid)
        // 20 shares at 58c, then 10 at 55c.
        assertEquals("17.10", estimate.costUsd)
        assertEquals("30", estimate.shares)
    }

    @Test
    fun tappingNoSwitchesToTheNoBookWithoutANetworkCall() {
        openPolymarketTicket()
        vm.switchOutcome("NO")
        assertEquals("NO", vm.state.value.draft.outcome)
        assertEquals(BigDecimal("0.42"), vm.state.value.asks.first().price)
    }

    @Test
    fun aLimitOrderStartsAtThePriceYouWouldGetNow() {
        openPolymarketTicket()
        vm.setMarketMode(false)
        assertFalse(vm.state.value.draft.isMarketOrder)
        assertEquals("0.6", vm.state.value.draft.price)
    }

    @Test
    fun theStepperMovesOneTickAndStaysInsideZeroAndOne() {
        openPolymarketTicket()
        vm.setMarketMode(false)
        vm.stepPrice(1)
        assertEquals("0.61", vm.state.value.draft.price)
        vm.setPrice("0.99")
        vm.stepPrice(1)
        assertEquals("0.99", vm.state.value.draft.price)
        vm.setPrice("0.01")
        vm.stepPrice(-1)
        assertEquals("0.01", vm.state.value.draft.price)
    }

    @Test
    fun quickAddsAccumulateAndPercentChipsUseWhatIsHeld() {
        openPolymarketTicket()
        vm.addAmount(5)
        vm.addAmount(10)
        assertEquals("15", vm.state.value.draft.amountUsd)
        vm.setHeld(BigDecimal("40"), BigDecimal.ZERO)
        vm.setSide("SELL")
        vm.setPercentOfHeld(50)
        assertEquals("20", vm.state.value.draft.shares)
        vm.setPercentOfHeld(100)
        assertEquals("40", vm.state.value.draft.shares)
    }

    @Test
    fun switchingBuyAndSellStartsTheInputsClean() {
        openPolymarketTicket()
        vm.setAmount("25")
        vm.setSide("SELL")
        assertEquals("", vm.state.value.draft.amountUsd)
        assertEquals("", vm.state.value.draft.shares)
    }

    @Test
    fun heldSharesAddsUpThePositionRowsOfOneMarketAndOutcome() {
        val rows = listOf(
            JSONObject("""{"venue":"polymarket","market_id":"m1","outcome":"YES","shares":"10"}"""),
            JSONObject("""{"venue":"polymarket","market_id":"m1","outcome":"YES","shares":"2.5"}"""),
            JSONObject("""{"venue":"polymarket","market_id":"m1","outcome":"NO","shares":"7"}"""),
            JSONObject("""{"venue":"polymarket","market_id":"m2","outcome":"YES","shares":"99"}"""),
        )
        assertEquals(0, BigDecimal("12.5").compareTo(heldShares(rows, "polymarket", "m1", "YES")))
        assertEquals(0, BigDecimal("7").compareTo(heldShares(rows, "polymarket", "m1", "NO")))
    }
}
