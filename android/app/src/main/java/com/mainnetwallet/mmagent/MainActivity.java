package com.mainnetwallet.mmagent;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Bundle;
import android.text.InputType;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.JsPromptResult;
import android.webkit.JsResult;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.net.URI;
import java.net.URISyntaxException;

/**
 * Thin, secure WebView shell for the multi-market-agent PWA.
 *
 * The backend is expected to be an HTTPS Render deployment. The URL is chosen
 * on first launch and can be changed later from the action bar. This app never
 * stores venue keys; login is the normal signed HttpOnly cookie from the agent.
 */
public class MainActivity extends Activity {

    private static final String PREFS = "mm_agent_prefs";
    private static final String KEY_BASE_URL = "base_url";

    private WebView webView;
    private ProgressBar progress;
    private TextView offline;
    private SharedPreferences prefs;
    private String baseUrl = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        webView = findViewById(R.id.webview);
        progress = findViewById(R.id.progress);
        offline = findViewById(R.id.offline);
        configureWebView();

        String saved = prefs.getString(KEY_BASE_URL, "");
        if (saved == null || saved.trim().isEmpty()) {
            saved = BuildConfig.DEFAULT_BASE_URL == null ? "" : BuildConfig.DEFAULT_BASE_URL.trim();
            if (saved.contains("YOUR-SERVICE") || saved.contains("your-service")) {
                saved = "";
            }
        }
        if (saved.isEmpty()) {
            showServerDialog(true);
        } else {
            baseUrl = saved;
            loadApp();
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setMediaPlaybackRequiresUserGesture(true);
        settings.setSupportMultipleWindows(true);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setUserAgentString(settings.getUserAgentString() + " MMAgentAndroid/1.0");
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);

        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, true);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (isAppUri(uri)) {
                    return false;
                }
                openExternal(uri);
                return true;
            }

            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                offline.setVisibility(View.GONE);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                CookieManager.getInstance().flush();
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    offline.setVisibility(View.VISIBLE);
                }
            }

            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                // Fail closed on certificate problems.
                handler.cancel();
                offline.setVisibility(View.VISIBLE);
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                progress.setVisibility(newProgress >= 100 ? View.GONE : View.VISIBLE);
                progress.setProgress(newProgress);
            }

            @Override
            public boolean onJsAlert(WebView view, String url, String message, JsResult result) {
                new AlertDialog.Builder(MainActivity.this)
                        .setMessage(message)
                        .setPositiveButton(android.R.string.ok, (d, w) -> result.confirm())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }

            @Override
            public boolean onJsConfirm(WebView view, String url, String message, JsResult result) {
                new AlertDialog.Builder(MainActivity.this)
                        .setMessage(message)
                        .setPositiveButton(android.R.string.ok, (d, w) -> result.confirm())
                        .setNegativeButton(android.R.string.cancel, (d, w) -> result.cancel())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }

            @Override
            public boolean onJsPrompt(WebView view, String url, String message, String defaultValue,
                                      JsPromptResult result) {
                EditText input = new EditText(MainActivity.this);
                input.setInputType(InputType.TYPE_CLASS_TEXT);
                input.setText(defaultValue == null ? "" : defaultValue);
                FrameLayout wrap = new FrameLayout(MainActivity.this);
                int pad = getResources().getDimensionPixelSize(R.dimen.dialog_padding);
                wrap.setPadding(pad, pad / 2, pad, 0);
                wrap.addView(input);
                new AlertDialog.Builder(MainActivity.this)
                        .setMessage(message)
                        .setView(wrap)
                        .setPositiveButton(android.R.string.ok,
                                (d, w) -> result.confirm(input.getText().toString()))
                        .setNegativeButton(android.R.string.cancel, (d, w) -> result.cancel())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }

            @Override
            public boolean onCreateWindow(WebView view, boolean isDialog, boolean isUserGesture,
                                          android.os.Message resultMsg) {
                // Links with target="_blank" (research sources) open in the browser,
                // never in a second uncontrolled WebView.
                WebView bridge = new WebView(MainActivity.this);
                bridge.setWebViewClient(new WebViewClient() {
                    @Override
                    public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest request) {
                        openExternal(request.getUrl());
                        return true;
                    }
                });
                WebView.WebViewTransport transport = (WebView.WebViewTransport) resultMsg.obj;
                transport.setWebView(bridge);
                resultMsg.sendToTarget();
                return true;
            }
        });

        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) ->
                openExternal(Uri.parse(url)));
    }

    private boolean isAppUri(Uri uri) {
        if (uri == null) {
            return false;
        }
        String scheme = uri.getScheme();
        if (!"https".equalsIgnoreCase(scheme)) {
            return false;
        }
        try {
            URI base = new URI(baseUrl);
            int basePort = base.getPort() == -1 ? 443 : base.getPort();
            int uriPort = uri.getPort() == -1 ? 443 : uri.getPort();
            return base.getHost() != null && base.getHost().equalsIgnoreCase(uri.getHost())
                    && basePort == uriPort;
        } catch (URISyntaxException | NullPointerException e) {
            return false;
        }
    }

    private void openExternal(Uri uri) {
        if (uri == null) {
            return;
        }
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (Exception e) {
            Toast.makeText(this, R.string.invalid_url, Toast.LENGTH_SHORT).show();
        }
    }

    private void loadApp() {
        offline.setVisibility(View.GONE);
        String url = baseUrl.endsWith("/") ? baseUrl : baseUrl + "/";
        webView.loadUrl(url);
    }

    private void showServerDialog(boolean firstRun) {
        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setHint(R.string.server_hint);
        input.setText(baseUrl.isEmpty() ? "" : baseUrl);
        FrameLayout wrap = new FrameLayout(this);
        int pad = getResources().getDimensionPixelSize(R.dimen.dialog_padding);
        wrap.setPadding(pad, pad / 2, pad, 0);
        wrap.addView(input);

        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle(R.string.server_title)
                .setMessage(R.string.server_message)
                .setView(wrap)
                .setPositiveButton(R.string.connect, null)
                .setNegativeButton(R.string.cancel, (d, w) -> {
                    if (firstRun) {
                        finish();
                    }
                })
                .create();
        dialog.setCancelable(!firstRun);
        dialog.setOnShowListener(d -> dialog.getButton(AlertDialog.BUTTON_POSITIVE)
                .setOnClickListener(v -> {
                    String normalized = normalizeBaseUrl(input.getText().toString());
                    if (normalized == null) {
                        input.setError(getString(R.string.invalid_url));
                        return;
                    }
                    boolean changed = !normalized.equals(baseUrl);
                    baseUrl = normalized;
                    prefs.edit().putString(KEY_BASE_URL, baseUrl).apply();
                    if (changed) {
                        CookieManager.getInstance().removeAllCookies(null);
                        webView.clearCache(true);
                    }
                    dialog.dismiss();
                    loadApp();
                }));
        dialog.show();
    }

    static String normalizeBaseUrl(String raw) {
        String value = raw == null ? "" : raw.trim();
        if (value.isEmpty()) {
            return null;
        }
        if (!value.contains("://")) {
            value = "https://" + value;
        }
        try {
            URI uri = new URI(value);
            if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null) {
                return null;
            }
            if (uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null) {
                return null;
            }
            String path = uri.getPath();
            if (path != null && !path.isEmpty() && !"/".equals(path)) {
                return null;
            }
            return "https://" + uri.getHost().toLowerCase()
                    + (uri.getPort() == -1 ? "" : ":" + uri.getPort());
        } catch (URISyntaxException e) {
            return null;
        }
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        getMenuInflater().inflate(R.menu.main, menu);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        int id = item.getItemId();
        if (id == R.id.action_server) {
            showServerDialog(false);
            return true;
        }
        if (id == R.id.action_reload) {
            if (!baseUrl.isEmpty()) {
                webView.reload();
            }
            return true;
        }
        if (id == R.id.action_browser) {
            if (!baseUrl.isEmpty()) {
                openExternal(Uri.parse(baseUrl));
            }
            return true;
        }
        if (id == R.id.action_clear_session) {
            confirmClearSession();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    private void confirmClearSession() {
        new AlertDialog.Builder(this)
                .setTitle(R.string.clear_session_title)
                .setMessage(R.string.clear_session_message)
                .setPositiveButton(android.R.string.ok, (d, w) -> {
                    CookieManager.getInstance().removeAllCookies(null);
                    CookieManager.getInstance().flush();
                    webView.clearCache(true);
                    webView.clearHistory();
                    if (!baseUrl.isEmpty()) {
                        webView.loadUrl(baseUrl + "/");
                    }
                })
                .setNegativeButton(android.R.string.cancel, null)
                .show();
    }

    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onPause() {
        CookieManager.getInstance().flush();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            ViewGroup parent = (ViewGroup) webView.getParent();
            if (parent != null) {
                parent.removeView(webView);
            }
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}
