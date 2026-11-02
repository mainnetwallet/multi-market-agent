package com.mainnetwallet.mmagent

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager

/**
 * Keeps the app's process alive while the operator is signed in, so Android does not kill the chat
 * (or anything else the app is doing) when the app goes to the background.
 *
 * It is a foreground service with a quiet, always-visible notification. While a chat answer is running
 * it also holds a CPU wake lock, so the stream is not cut when the screen turns off; the lock is
 * released as soon as the answer ends, and it times out on its own after [WAKE_LOCK_MS].
 */
class KeepAliveService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Agent connection", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Keeps the chat and the connection to the server alive in the background."
                setShowBadge(false)
            },
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val busy = intent?.getBooleanExtra(EXTRA_BUSY, false) == true
        val notification = buildNotification(busy)
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        if (busy) acquireWakeLock() else releaseWakeLock()
        return START_STICKY
    }

    override fun onDestroy() {
        releaseWakeLock()
        super.onDestroy()
    }

    private fun buildNotification(busy: Boolean): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("Multi-market agent")
            .setContentText(if (busy) "The assistant is answering in the background" else "Connected in the background")
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(Notification.CATEGORY_SERVICE)
            .build()
    }

    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        val power = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "mmagent:chat").apply {
            setReferenceCounted(false)
            acquire(WAKE_LOCK_MS)
        }
    }

    private fun releaseWakeLock() {
        wakeLock?.takeIf { it.isHeld }?.release()
        wakeLock = null
    }

    companion object {
        private const val CHANNEL_ID = "keep_alive"
        private const val NOTIFICATION_ID = 4101
        private const val EXTRA_BUSY = "busy"
        private const val WAKE_LOCK_MS = 15 * 60 * 1000L

        /** Start the service (or update it): [busy] is true while a chat answer is running. */
        fun sync(context: Context, busy: Boolean) {
            val intent = Intent(context, KeepAliveService::class.java).putExtra(EXTRA_BUSY, busy)
            try {
                context.startForegroundService(intent)
            } catch (e: Exception) {
                // Android refused a start from the background; the next change while the app is open retries.
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, KeepAliveService::class.java))
        }
    }
}
