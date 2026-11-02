package com.mainnetwallet.mmagent.data

import android.content.Context
import com.mainnetwallet.mmagent.BuildConfig

/**
 * One [AppViewModel] for the whole process. A ViewModel normally dies with its Activity, and Android
 * destroys the Activity of a backgrounded app whenever it likes; a running chat answer would die with it.
 * Held here, the chat keeps going for as long as the process lives (KeepAliveService keeps the process alive).
 */
object AppHolder {
    private var api: ApiClient? = null
    private var model: AppViewModel? = null

    @Synchronized
    fun get(context: Context): Pair<ApiClient, AppViewModel> {
        val app = context.applicationContext
        val existingApi = api
        val existingModel = model
        if (existingApi != null && existingModel != null) return existingApi to existingModel
        val prefs = app.getSharedPreferences("mm_agent_prefs", Context.MODE_PRIVATE)
        val savedUrl = prefs.getString(AppViewModel.KEY_BASE_URL, null)?.takeIf { it.isNotBlank() }
        val client = ApiClient(savedUrl ?: BuildConfig.DEFAULT_BASE_URL) {
            prefs.getString(AppViewModel.KEY_TOKEN, null)
        }
        val created = AppViewModel(app, client)
        api = client
        model = created
        return client to created
    }
}
