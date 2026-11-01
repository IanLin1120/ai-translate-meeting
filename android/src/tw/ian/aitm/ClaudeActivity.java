package tw.ian.aitm;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;

/**
 * App 內建的 Claude 視窗：在 App 裡開啟 claude.ai 上的「會議整理台」頁面（用使用者自己的 Claude 方案），
 * 自動把工作（逐字稿／要翻譯的文字）傳給頁面，完成後自動把結果送回 App 並關閉，不用複製貼上、不用切換 App。
 * 傳遞方式：在 claude.ai 主頁面注入 claude-inject.js，用 postMessage 和頁面裡的整理台互相溝通。
 */
public class ClaudeActivity extends Activity {
    static volatile String job = "";
    static volatile String target = "";
    static volatile boolean running = false;

    WebView web;
    TextView status;
    ProgressBar bar;
    final Handler ui = new Handler(Looper.getMainLooper());
    String inject = "";
    boolean done = false;

    int dp(int v) { return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics())); }

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        running = true;
        try (InputStream in = getAssets().open("www/claude-inject.js")) {
            ByteArrayOutputStream o = new ByteArrayOutputStream(); byte[] buf = new byte[8192]; int n;
            while ((n = in.read(buf)) > 0) o.write(buf, 0, n);
            inject = o.toString("UTF-8");
        } catch (Exception ignored) {}

        boolean dark = (getResources().getConfiguration().uiMode & android.content.res.Configuration.UI_MODE_NIGHT_MASK) == android.content.res.Configuration.UI_MODE_NIGHT_YES;
        int bg = dark ? Color.parseColor("#1A2324") : Color.parseColor("#F6F8F7"), fg = dark ? Color.parseColor("#E4ECEA") : Color.parseColor("#1C2A2D");
        getWindow().setStatusBarColor(bg);
        if (!dark) getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR);

        LinearLayout root = new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL); root.setBackgroundColor(bg);
        LinearLayout top = new LinearLayout(this); top.setOrientation(LinearLayout.HORIZONTAL); top.setGravity(Gravity.CENTER_VERTICAL);
        top.setPadding(dp(14), dp(8), dp(8), dp(8)); top.setBackgroundColor(bg);
        LinearLayout tx = new LinearLayout(this); tx.setOrientation(LinearLayout.VERTICAL);
        TextView title = new TextView(this); title.setText("✨ Claude 整理（你的 Claude 方案）"); title.setTextColor(fg); title.setTypeface(Typeface.DEFAULT_BOLD); title.setTextSize(15);
        status = new TextView(this); status.setText("開啟中…"); status.setTextColor(fg); status.setAlpha(.75f); status.setTextSize(12.5f);
        tx.addView(title); tx.addView(status);
        top.addView(tx, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        Button close = new Button(this); close.setText("關閉"); close.setAllCaps(false);
        close.setOnClickListener(v -> finish());
        top.addView(close);
        bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal); bar.setMax(100); bar.setIndeterminate(true);
        root.addView(top); root.addView(bar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(4)));

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true); s.setDomStorageEnabled(true); s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false); s.setSupportMultipleWindows(false); s.setJavaScriptCanOpenWindowsAutomatically(true);
        /* 用一般手機瀏覽器的識別（去掉 WebView 標記），登入頁比較不會擋 */
        s.setUserAgentString(s.getUserAgentString().replace("; wv", "").replaceAll("Version/[0-9.]+ ", ""));
        CookieManager cm = CookieManager.getInstance(); cm.setAcceptCookie(true); cm.setAcceptThirdPartyCookies(web, true);
        web.addJavascriptInterface(new Bridge(), "AITMC");
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) { return false; }
            @Override public void onPageFinished(WebView v, String url) {
                if (inject.length() > 0) v.evaluateJavascript(inject, null);
                if (url != null && url.contains("/login")) say("請先登入 Claude（第一次需要；之後會記住）。用 Email 登入碼最穩定", -1);
                /* 登入完成後常會跳到首頁 → 自動回到整理台 */
                else if (url != null && url.startsWith("https://claude.ai/") && !url.contains("/artifact/") && !url.contains("login") && !url.contains("magic") && !url.contains("oauth") && target.length() > 0)
                    ui.postDelayed(() -> { if (!done && web != null) web.loadUrl(target); }, 800);
                CookieManager.getInstance().flush();
            }
        });
        root.addView(web, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        setContentView(root);
        root.setOnApplyWindowInsetsListener((v, ins) -> { v.setPadding(0, ins.getSystemWindowInsetTop(), 0, ins.getSystemWindowInsetBottom()); return ins; });
        web.loadUrl(target);
        say("連線到 claude.ai…", -1);
    }

    void say(String t, int p) {
        ui.post(() -> {
            if (status != null) status.setText(t);
            if (bar != null) { if (p < 0) bar.setIndeterminate(true); else { bar.setIndeterminate(false); bar.setProgress(p); } }
        });
    }

    class Bridge {
        @JavascriptInterface public String job() { return done ? "" : job; }
        @JavascriptInterface public void prog(String stage, int p) {
            say(stage, p);
            MainActivity.toApp("window.__claudeProg&&__claudeProg(" + org.json.JSONObject.quote(stage) + "," + p + ")");
        }
        @JavascriptInterface public void result(String res) {
            if (done) return; done = true; job = "";
            say("完成！正在套用到 App…", 100);
            MainActivity.toApp("window.__claudeRes&&__claudeRes(" + org.json.JSONObject.quote(res) + ")");
            ui.postDelayed(ClaudeActivity.this::finish, 900);
        }
        @JavascriptInterface public void error(String msg, String code) {
            say("❌ " + msg, -1);
            MainActivity.toApp("window.__claudeErr&&__claudeErr(" + org.json.JSONObject.quote(msg) + "," + org.json.JSONObject.quote(code) + ")");
        }
    }

    @Override public void onBackPressed() { if (web != null && web.canGoBack() && !done) web.goBack(); else super.onBackPressed(); }

    @Override protected void onDestroy() {
        running = false;
        if (!done) MainActivity.toApp("window.__claudeErr&&__claudeErr('已關閉 Claude 視窗','closed')");
        if (web != null) { web.destroy(); web = null; }
        super.onDestroy();
    }
}
