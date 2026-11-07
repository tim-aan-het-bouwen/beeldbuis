package app.beeldbuis.tv

import android.annotation.SuppressLint
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import androidx.webkit.WebViewAssetLoader
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.net.HttpURLConnection
import java.net.URL

/**
 * Beeldbuis for Android TV and phones: the TV web app (tv/app) in a WebView on top of a Media3 player.
 * The page talks to the player through the `BeeldbuisAndroid` bridge (see js/player.js) and gets
 * events back through `BBPlayerEvent(...)`.
 */
class MainActivity : AppCompatActivity() {
    private lateinit var web: WebView
    private lateinit var playerView: PlayerView
    private var player: ExoPlayer? = null
    private val main = Handler(Looper.getMainLooper())
    private val ticker = object : Runnable {
        override fun run() {
            val p = player ?: return
            val d = if (p.duration == C.TIME_UNSET) 0.0 else p.duration / 1000.0
            js("BBPlayerEvent('time', ${p.currentPosition / 1000.0}, $d)")
            main.postDelayed(this, 1000)
        }
    }

    private val assets by lazy {
        WebViewAssetLoader.Builder().addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(this)).build()
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        hideSystemBars()

        val root = FrameLayout(this)
        playerView = PlayerView(this).apply {
            useController = false
            resizeMode = AspectRatioFrameLayout.RESIZE_MODE_FIT
            setShutterBackgroundColor(Color.BLACK)
            visibility = View.GONE
        }
        web = WebView(this).apply {
            setBackgroundColor(Color.TRANSPARENT)
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mediaPlaybackRequiresUserGesture = false
            settings.useWideViewPort = true
            settings.loadWithOverviewMode = true
            isFocusable = true
            isFocusableInTouchMode = true
            addJavascriptInterface(Bridge(), "BeeldbuisAndroid")
            webViewClient = object : WebViewClient() {
                override fun shouldInterceptRequest(view: WebView, req: WebResourceRequest): WebResourceResponse? {
                    val u = req.url
                    if (u.host == "appassets.androidplatform.net" && u.path == "/proxy") return proxy(u.getQueryParameter("u"))
                    return assets.shouldInterceptRequest(u)
                }
            }
        }
        root.addView(playerView, FrameLayout.LayoutParams(-1, -1))
        root.addView(web, FrameLayout.LayoutParams(-1, -1))
        setContentView(root)
        web.loadUrl("https://appassets.androidplatform.net/assets/web/index.html")
        web.requestFocus()

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() { js("BBKey('back')") }
        })
    }

    /** Provider requests from the page (Xtream servers send no CORS headers, so the page cannot call them itself). */
    private fun proxy(target: String?): WebResourceResponse {
        val headers = mapOf("Access-Control-Allow-Origin" to "*", "Cache-Control" to "no-store")
        if (target == null || !(target.startsWith("http://") || target.startsWith("https://")))
            return WebResourceResponse("text/plain", "utf-8", 400, "Bad Request", headers, ByteArrayInputStream(ByteArray(0)))
        return try {
            var url = URL(target)
            var conn: HttpURLConnection
            var hops = 0
            while (true) {   // follow redirects, also http <-> https
                conn = url.openConnection() as HttpURLConnection
                conn.instanceFollowRedirects = false
                conn.connectTimeout = 15000
                conn.readTimeout = 90000
                conn.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android) Beeldbuis/0.1")
                val code = conn.responseCode
                if (code in 300..399 && hops++ < 5) { url = URL(url, conn.getHeaderField("Location")); conn.disconnect(); continue }
                break
            }
            val code = conn.responseCode
            val body = (if (code >= 400) conn.errorStream else conn.inputStream)?.readBytes() ?: ByteArray(0)
            val type = (conn.contentType ?: "application/json").substringBefore(';').trim()
            WebResourceResponse(type, "utf-8", code, conn.responseMessage?.ifBlank { null } ?: "OK", headers, ByteArrayInputStream(body))
        } catch (e: Exception) {
            WebResourceResponse("text/plain", "utf-8", 502, "Bad Gateway", headers, ByteArrayInputStream((e.message ?: "error").toByteArray()))
        }
    }

    private fun js(code: String) = main.post { web.evaluateJavascript(code, null) }
    private fun quote(s: String) = JSONObject.quote(s)

    private fun hideSystemBars() {
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_LAYOUT_STABLE)
    }

    /** Remote buttons the WebView does not pass on as key codes. */
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        val action = when (event.keyCode) {
            KeyEvent.KEYCODE_CHANNEL_UP -> "chup"
            KeyEvent.KEYCODE_CHANNEL_DOWN -> "chdown"
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE -> "playpause"
            KeyEvent.KEYCODE_MEDIA_PLAY -> "play"
            KeyEvent.KEYCODE_MEDIA_PAUSE -> "pause"
            KeyEvent.KEYCODE_MEDIA_STOP -> "stop"
            KeyEvent.KEYCODE_MEDIA_FAST_FORWARD -> "ff"
            KeyEvent.KEYCODE_MEDIA_REWIND -> "rw"
            KeyEvent.KEYCODE_PROG_RED -> "red"
            KeyEvent.KEYCODE_PROG_GREEN -> "green"
            KeyEvent.KEYCODE_PROG_YELLOW -> "yellow"
            KeyEvent.KEYCODE_PROG_BLUE -> "blue"
            KeyEvent.KEYCODE_INFO -> "info"
            else -> null
        }
        if (action != null) {
            if (event.action == KeyEvent.ACTION_DOWN) js("BBKey(${quote(action)})")
            return true
        }
        return super.dispatchKeyEvent(event)
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemBars()
    }

    override fun onStop() {
        super.onStop()
        // leaving the app frees the subscription's single connection
        if (player != null) js("BBKey('stop')")
        releasePlayer()
    }

    override fun onDestroy() {
        releasePlayer()
        web.destroy()
        super.onDestroy()
    }

    private fun releasePlayer() {
        main.removeCallbacks(ticker)
        player?.release()
        player = null
        playerView.player = null
        playerView.visibility = View.GONE
    }

    inner class Bridge {
        @JavascriptInterface
        fun play(url: String, optsJson: String) = main.post {
            val opts = try { JSONObject(optsJson) } catch (e: Exception) { JSONObject() }
            releasePlayer()
            val p = ExoPlayer.Builder(this@MainActivity).build()
            if (opts.optBoolean("englishSubs", true)) {
                p.trackSelectionParameters = p.trackSelectionParameters.buildUpon()
                    .setPreferredTextLanguage("en")
                    .setSelectUndeterminedTextLanguage(false)
                    .build()
            }
            p.addListener(object : Player.Listener {
                override fun onPlaybackStateChanged(state: Int) {
                    when (state) {
                        Player.STATE_BUFFERING -> js("BBPlayerEvent('state', 'buffering')")
                        Player.STATE_READY -> js("BBPlayerEvent('state', ${quote(if (p.playWhenReady) "playing" else "paused")})")
                        Player.STATE_ENDED -> js("BBPlayerEvent('ended')")
                        else -> {}
                    }
                }
                override fun onIsPlayingChanged(isPlaying: Boolean) {
                    if (p.playbackState == Player.STATE_READY) js("BBPlayerEvent('state', ${quote(if (isPlaying) "playing" else "paused")})")
                }
                override fun onPlayerError(error: PlaybackException) {
                    js("BBPlayerEvent('error', ${quote(error.errorCodeName)})")
                }
            })
            player = p
            playerView.player = p
            playerView.visibility = View.VISIBLE
            p.setMediaItem(MediaItem.fromUri(Uri.parse(url)))
            val start = opts.optDouble("start", 0.0)
            if (start > 0) p.seekTo((start * 1000).toLong())
            p.playWhenReady = true
            p.prepare()
            main.post(ticker)
        }

        @JavascriptInterface
        fun stop() = main.post { releasePlayer() }

        @JavascriptInterface
        fun toggle() = main.post { player?.let { it.playWhenReady = !it.playWhenReady } }

        @JavascriptInterface
        fun seekBy(seconds: Double) = main.post { player?.let { it.seekTo((it.currentPosition + (seconds * 1000).toLong()).coerceAtLeast(0)) } }

        @JavascriptInterface
        fun exit() = main.post { finish() }
    }
}
