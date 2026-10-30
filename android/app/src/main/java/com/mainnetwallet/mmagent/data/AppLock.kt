package com.mainnetwallet.mmagent.data

import android.content.Context
import android.content.SharedPreferences
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat

/**
 * The app lock: the app re-locks after a chosen time in the background and needs
 * a biometric to open again.
 *
 * The lock only turns on when the device actually has a biometric enrolled. A
 * lock that cannot be opened would strand the operator, so without one the
 * feature stays off and Settings says so.
 */
class AppLock(context: Context) {

    private val prefs: SharedPreferences =
        context.getSharedPreferences("mm_agent_lock", Context.MODE_PRIVATE)

    /** Minutes in the background before the app re-locks. 0 means immediately. */
    var timeoutMinutes: Int
        get() = prefs.getInt(KEY_TIMEOUT, DEFAULT_TIMEOUT)
        set(value) = prefs.edit().putInt(KEY_TIMEOUT, value.coerceIn(0, 60)).apply()

    /** On by default; the operator can turn it off. */
    var enabled: Boolean
        get() = prefs.getBoolean(KEY_ENABLED, true)
        set(value) = prefs.edit().putBoolean(KEY_ENABLED, value).apply()

    /** True when this device can actually unlock the app. */
    fun available(context: Context): Boolean {
        val manager = BiometricManager.from(context)
        return manager.canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_STRONG) ==
            BiometricManager.BIOMETRIC_SUCCESS
    }

    fun onBackground() {
        prefs.edit().putLong(KEY_BACKGROUND_AT, System.currentTimeMillis()).apply()
    }

    /** True when the app must be unlocked before it shows anything. */
    fun isLocked(): Boolean {
        if (!enabled) return false
        val backgroundAt = prefs.getLong(KEY_BACKGROUND_AT, 0L)
        if (backgroundAt == 0L) return false
        val away = System.currentTimeMillis() - backgroundAt
        return away > timeoutMinutes * 60_000L
    }

    fun unlock() {
        prefs.edit().remove(KEY_BACKGROUND_AT).apply()
    }

    /** Ask for a biometric. Returns false when the device has none enrolled. */
    fun prompt(activity: androidx.fragment.app.FragmentActivity, onSuccess: () -> Unit) {
        val executor = ContextCompat.getMainExecutor(activity)
        val prompt = BiometricPrompt(
            activity,
            executor,
            object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                    unlock()
                    onSuccess()
                }
            },
        )
        prompt.authenticate(
            BiometricPrompt.PromptInfo.Builder()
                .setTitle("Unlock MM Agent")
                .setSubtitle("Confirm it is you to see your balances and positions")
                .setNegativeButtonText("Cancel")
                .build()
        )
    }

    companion object {
        const val DEFAULT_TIMEOUT = 5
        private const val KEY_TIMEOUT = "timeout_minutes"
        private const val KEY_ENABLED = "enabled"
        private const val KEY_BACKGROUND_AT = "background_at"
    }
}
