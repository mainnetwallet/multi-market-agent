package com.mainnetwallet.mmagent

import com.mainnetwallet.mmagent.data.AllowlistEntry
import com.mainnetwallet.mmagent.data.WithdrawStep
import com.mainnetwallet.mmagent.data.WithdrawViewModel
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class WithdrawViewModelTest {

    private val dispatcher = StandardTestDispatcher()
    private lateinit var vm: WithdrawViewModel

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
        vm = WithdrawViewModel(com.mainnetwallet.mmagent.data.ApiClient("https://example.invalid") { null })
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun theFlowStartsOnTheForm() {
        assertEquals(WithdrawStep.FORM, vm.state.value.step)
    }

    @Test
    fun aNewWithdrawalGetsANewIdempotencyKey() {
        val first = vm.state.value.idempotencyKey
        vm.reset()
        assertTrue(first != vm.state.value.idempotencyKey)
    }

    @Test
    fun changingTheFormGetsANewIdempotencyKey() {
        val first = vm.state.value.idempotencyKey
        vm.setAmount("10")
        val second = vm.state.value.idempotencyKey
        vm.setRecipient("0x1234567890abcdef1234567890abcdef12345678")
        assertTrue(first != second)
        assertTrue(second != vm.state.value.idempotencyKey)
    }

    @Test
    fun theVenueIsCarriedIntoTheFlow() {
        vm.setVenue("polymarket")
        assertEquals("polymarket", vm.state.value.venue)
    }

    @Test
    fun theAmountKeepsOnlyDigitsAndOneDecimalPoint() {
        vm.setAmount("1,2a3.4.5")
        assertEquals("123.45", vm.state.value.amount)
    }

    @Test
    fun maxIsTheAvailableBalanceRoundedDownToCents() {
        vm.setMax("12.3499")
        assertEquals("12.34", vm.state.value.amount)
        vm.setMax("0")
        assertEquals("12.34", vm.state.value.amount)
    }

    @Test
    fun theReceiverAddressLosesItsWhitespace() {
        vm.setRecipient(" 0x1234 5678\n")
        assertEquals("0x12345678", vm.state.value.recipient)
    }

    @Test
    fun onlyAnEvmAddressShapeIsAccepted() {
        assertTrue(WithdrawViewModel.isEvmAddress("0x1234567890abcdef1234567890abcdef12345678"))
        assertTrue(!WithdrawViewModel.isEvmAddress("0x1234"))
        assertTrue(!WithdrawViewModel.isEvmAddress("1234567890abcdef1234567890abcdef1234567800"))
        assertTrue(!WithdrawViewModel.isEvmAddress("0xZZ34567890abcdef1234567890abcdef12345678"))
    }

    @Test
    fun aFeeQuotedForOtherInputsIsNotReady() {
        // Nothing has been quoted, so the page must not let the withdrawal through.
        vm.setVenue("polymarket")
        vm.setAmount("10")
        assertTrue(!vm.state.value.quoteReady)
    }

    @Test
    fun withdrawingWithoutAFeeIsRefused() {
        vm.setVenue("polymarket")
        vm.setAmount("10")
        var result: Boolean? = null
        vm.submit { result = it }
        assertEquals(false, result)
        assertTrue(!vm.state.value.submitting)
    }

    @Test
    fun anAddressInItsCoolingPeriodCannotBeUsedYet() {
        val later = java.time.Instant.now().plusSeconds(3_600).toString()
        val earlier = java.time.Instant.now().minusSeconds(3_600).toString()
        fun entry(usableAt: String?) = AllowlistEntry("1", "cold", "0xabc", "Base", "USDC", usableAt)
        assertTrue(!entry(later).isUsable())
        assertTrue(entry(earlier).isUsable())
        assertTrue(entry(null).isUsable())
    }
}
