package tw.ian.aitm;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Message;
import android.net.Uri;
import android.widget.FrameLayout;
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
    FrameLayout frame;
    WebView pop;
    int homeHops = 0;

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
        setup(web);
        web.addJavascriptInterface(new Bridge(), "AITMC");
        web.setWebChromeClient(new Chrome());
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) { return false; }
            @Override public void onPageStarted(WebView v, String url, android.graphics.Bitmap f) { hint(url); }
            @Override public void onPageFinished(WebView v, String url) {
                if (inject.length() > 0) v.evaluateJavascript(inject, null);
                hint(url);
                /* 登入完成後 claude.ai 常停在首頁或對話列表 → 自動回到整理台（登入過程中的網址不碰，避免打斷登入） */
                Uri u = url == null ? null : Uri.parse(url);
                String path = u == null || u.getPath() == null ? "" : u.getPath();
                boolean home = u != null && "claude.ai".equals(u.getHost()) && (path.equals("") || path.equals("/") || path.startsWith("/new") || path.startsWith("/recents") || path.startsWith("/chats") || path.startsWith("/chat/") || path.startsWith("/projects"));
                if (home && target.length() > 0 && homeHops < 6) {
                    homeHops++;
                    ui.postDelayed(() -> { if (!done && web != null) { String c = web.getUrl(); if (c != null && !c.contains("/artifact/") && !c.contains("login")) web.loadUrl(target); } }, 1500);
                }
                CookieManager.getInstance().flush();
            }
        });
        frame = new FrameLayout(this);
        frame.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        root.addView(frame, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        setContentView(root);
        root.setOnApplyWindowInsetsListener((v, ins) -> { v.setPadding(0, ins.getSystemWindowInsetTop(), 0, ins.getSystemWindowInsetBottom()); return ins; });
        web.loadUrl(target);
        say("連線到 claude.ai…", -1);
    }

    void setup(WebView w) {
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true); s.setDomStorageEnabled(true); s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        /* Google 登入是用「彈出視窗」完成的：一定要支援多視窗，登入結果才傳得回 claude.ai */
        s.setSupportMultipleWindows(true); s.setJavaScriptCanOpenWindowsAutomatically(true);
        s.setUserAgentString(s.getUserAgentString().replace("; wv", "").replaceAll("Version/[0-9.]+ ", ""));
        CookieManager cm = CookieManager.getInstance(); cm.setAcceptCookie(true); cm.setAcceptThirdPartyCookies(w, true);
    }

    void hint(String url) {
        if (url == null || done) return;
        if (url.contains("accounts.google.com")) say("Google 登入中…（選帳戶 → 繼續）", -1);
        else if (url.contains("/login") || url.contains("magic-link")) say("請先登入 Claude（第一次需要；之後會記住）", -1);
        else if (url.contains("/artifact/")) say("會議整理台開啟中…（第一次會問是否允許使用 Claude，請按允許）", -1);
        else if (url.contains("claude.ai")) say("登入完成，正在回到會議整理台…", -1);
    }

    /* 彈出視窗（Google 登入）：疊在主畫面上；登入完成後它會自己關閉 */
    class Chrome extends WebChromeClient {
        @Override public boolean onCreateWindow(WebView view, boolean dialog, boolean gesture, Message msg) {
            closePop();
            pop = new WebView(ClaudeActivity.this);
            setup(pop);
            pop.setWebChromeClient(new WebChromeClient() {
                @Override public void onCloseWindow(WebView w) { closePop(); }
            });
            pop.setWebViewClient(new WebViewClient() {
                @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) { return false; }
                @Override public void onPageStarted(WebView v, String url, android.graphics.Bitmap f) { hint(url); }
                @Override public void onPageFinished(WebView v, String url) {
                    CookieManager.getInstance().flush();
                    /* 登入結果頁回到 claude.ai 後若沒有自己關閉 → 幫它關掉，主畫面重新整理 */
                    if (url != null && url.startsWith("https://claude.ai/")) {
                        final WebView me = v;
                        ui.postDelayed(() -> { if (pop == me) { closePop(); if (web != null && !done) web.loadUrl(target); } }, 3000);
                    }
                }
            });
            frame.addView(pop, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
            WebView.WebViewTransport t = (WebView.WebViewTransport) msg.obj;
            t.setWebView(pop);
            msg.sendToTarget();
            return true;
        }
        @Override public void onCloseWindow(WebView w) { }
    }

    void closePop() {
        if (pop == null) return;
        WebView p = pop; pop = null;
        try { frame.removeView(p); p.destroy(); } catch (Exception ignored) {}
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

    @Override public void onBackPressed() { if (pop != null) { if (pop.canGoBack()) pop.goBack(); else closePop(); return; } if (web != null && web.canGoBack() && !done) web.goBack(); else super.onBackPressed(); }

    @Override protected void onDestroy() {
        running = false;
        if (!done) MainActivity.toApp("window.__claudeErr&&__claudeErr('已關閉 Claude 視窗','closed')");
        closePop();
        if (web != null) { web.destroy(); web = null; }
        super.onDestroy();
    }
}
