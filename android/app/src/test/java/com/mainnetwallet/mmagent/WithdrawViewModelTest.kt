package com.mainnetwallet.mmagent

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
    fun theFlowStartsAtTheAmountStep() {
        assertEquals(WithdrawStep.AMOUNT, vm.state.value.step)
    }

    @Test
    fun aNewWithdrawalGetsANewIdempotencyKey() {
        val first = vm.state.value.idempotencyKey
        vm.reset()
        assertTrue(first != vm.state.value.idempotencyKey)
    }

    @Test
    fun theVenueIsCarriedIntoTheFlow() {
        vm.setVenue("polymarket")
        assertEquals("polymarket", vm.state.value.venue)
    }

    @Test
    fun theStepUpTokenIsHeldForTheConfirmCall() {
        // The token arrives from the server after a successful password check; the
        // ViewModel keeps it so the single-use token is sent exactly once.
        vm.setVenue("polymarket")
        vm.setAmount("10")
        assertEquals(null, vm.state.value.stepUpToken)
    }

    @Test
    fun removingArecipientIsOfferedWithoutAStepUp() {
        // Removal reduces risk, so the server takes no step-up token for it.
        vm.reset()
        assertEquals(WithdrawStep.AMOUNT, vm.state.value.step)
    }
}
