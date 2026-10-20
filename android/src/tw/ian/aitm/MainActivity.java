package tw.ian.aitm;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.MediaStore;
import android.provider.Settings;
import android.speech.tts.TextToSpeech;
import android.util.Base64;
import android.util.Log;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

public class MainActivity extends Activity {
    static final String HOST = "appassets.androidplatform.net";
    static final String HOME = "https://" + HOST + "/index.html";
    static final int REQ_PERM = 11, REQ_FILE = 12, REQ_WEBPERM = 13;

    WebView web;
    final Handler ui = new Handler(Looper.getMainLooper());
    TextToSpeech tts;
    boolean ttsOk = false;
    ValueCallback<Uri[]> fileCb;
    PermissionRequest pendingWebPerm;
    final Map<Integer, Object[]> saves = new HashMap<>();
    int saveSeq = 0;
    int insT, insR, insB, insL;
    boolean recording = false;
    volatile boolean readyOk = false, safeMode = false;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        Window w = getWindow();
        w.setStatusBarColor(Color.TRANSPARENT);
        w.setNavigationBarColor(Color.TRANSPARENT);
        if (Build.VERSION.SDK_INT >= 28) {
            WindowManager.LayoutParams lp = w.getAttributes();
            lp.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            w.setAttributes(lp);
        }
        if (Build.VERSION.SDK_INT >= 29) { w.setNavigationBarContrastEnforced(false); w.setStatusBarContrastEnforced(false); }
        if (Build.VERSION.SDK_INT >= 30) w.setDecorFitsSystemWindows(false);
        else w.getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LAYOUT_STABLE | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);

        web = new WebView(this);
        web.setBackgroundColor(night() ? Color.rgb(19, 27, 27) : Color.rgb(214, 225, 222));
        setContentView(web);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setTextZoom(100);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        web.addJavascriptInterface(new Bridge(), "NB");
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new Chrome());
        web.setOnApplyWindowInsetsListener((v, ins) -> {
            float d = getResources().getDisplayMetrics().density;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets i = ins.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                insT = Math.round(i.top / d); insR = Math.round(i.right / d); insB = Math.round(i.bottom / d); insL = Math.round(i.left / d);
            } else {
                insT = Math.round(ins.getSystemWindowInsetTop() / d); insR = Math.round(ins.getSystemWindowInsetRight() / d);
                insB = Math.round(ins.getSystemWindowInsetBottom() / d); insL = Math.round(ins.getSystemWindowInsetLeft() / d);
            }
            pushInsets();
            return ins;
        });
        tts = new TextToSpeech(this, st -> ttsOk = st == TextToSpeech.SUCCESS);
        if (b != null) web.restoreState(b);
        else web.loadUrl(HOME);
    }

    void pushInsets() { js("window.__ins&&__ins(" + insT + "," + insR + "," + insB + "," + insL + ")"); }

    void js(String code) { ui.post(() -> { if (web != null) web.evaluateJavascript(code, null); }); }

    boolean night() { return (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES; }

    boolean has(String p) { return checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED; }

    String permJson() {
        try {
            JSONObject o = new JSONObject();
            o.put("mic", has(Manifest.permission.RECORD_AUDIO) ? "yes" : "no");
            o.put("cam", has(Manifest.permission.CAMERA) ? "yes" : "no");
            o.put("notif", Build.VERSION.SDK_INT < 33 || has("android.permission.POST_NOTIFICATIONS") ? "yes" : "no");
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            o.put("bat", pm != null && pm.isIgnoringBatteryOptimizations(getPackageName()) ? "yes" : "no");
            return o.toString();
        } catch (Exception e) { return "{}"; }
    }

    void requestPerms(String k) {
        List<String> l = new ArrayList<>();
        if (k.equals("bat")) {
            try {
                Intent i = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + getPackageName()));
                startActivity(i);
            } catch (Exception e) {
                try { startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)); } catch (Exception ignored) {}
            }
            return;
        }
        if (k.equals("mic") || k.equals("all")) l.add(Manifest.permission.RECORD_AUDIO);
        if (k.equals("cam") || k.equals("all")) l.add(Manifest.permission.CAMERA);
        if ((k.equals("notif") || k.equals("all")) && Build.VERSION.SDK_INT >= 33) l.add("android.permission.POST_NOTIFICATIONS");
        List<String> need = new ArrayList<>();
        for (String p : l) if (!has(p)) need.add(p);
        if (need.isEmpty()) { js("window.__perm&&__perm(" + permJson() + ",'" + k + "')"); return; }
        boolean blocked = false;
        for (String p : need) if (!shouldShowRequestPermissionRationale(p) && getSharedPreferences("p", 0).getBoolean(p, false)) blocked = true;
        if (blocked && !k.equals("all")) { openAppSettings(); js("window.__perm&&__perm(" + permJson() + ",'" + k + "')"); return; }
        for (String p : need) getSharedPreferences("p", 0).edit().putBoolean(p, true).apply();
        requestPermissions(need.toArray(new String[0]), REQ_PERM);
    }

    void openAppSettings() {
        try { startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName()))); } catch (Exception ignored) {}
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] p, int[] g) {
        super.onRequestPermissionsResult(code, p, g);
        if (code == REQ_WEBPERM && pendingWebPerm != null) {
            grantWeb(pendingWebPerm);
            pendingWebPerm = null;
        }
        js("window.__perm&&__perm(" + permJson() + ")");
    }

    void grantWeb(PermissionRequest r) {
        List<String> ok = new ArrayList<>();
        for (String res : r.getResources()) {
            if (res.equals(PermissionRequest.RESOURCE_AUDIO_CAPTURE) && has(Manifest.permission.RECORD_AUDIO)) ok.add(res);
            else if (res.equals(PermissionRequest.RESOURCE_VIDEO_CAPTURE) && has(Manifest.permission.CAMERA)) ok.add(res);
        }
        if (ok.isEmpty()) r.deny(); else r.grant(ok.toArray(new String[0]));
    }

    @Override
    protected void onResume() {
        super.onResume();
        js("window.__resume&&__resume()");
    }

    @Override
    public void onConfigurationChanged(Configuration c) {
        super.onConfigurationChanged(c);
        js("window.__night&&__night()");
        pushInsets();
    }

    @Override
    protected void onSaveInstanceState(Bundle o) { super.onSaveInstanceState(o); web.saveState(o); }

    @Override
    public void onBackPressed() {
        web.evaluateJavascript("(window.onAppBack&&window.onAppBack())?1:0", v -> {
            if (!"1".equals(v)) {
                if (recording) moveTaskToBack(true); else finish();
            }
        });
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req == REQ_FILE && fileCb != null) {
            Uri[] r = null;
            if (res == RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    ClipData cd = data.getClipData();
                    r = new Uri[cd.getItemCount()];
                    for (int i = 0; i < cd.getItemCount(); i++) r[i] = cd.getItemAt(i).getUri();
                } else if (data.getData() != null) r = new Uri[]{data.getData()};
            }
            fileCb.onReceiveValue(r);
            fileCb = null;
        }
    }

    @Override
    protected void onDestroy() {
        if (tts != null) tts.shutdown();
        if (recording) stopService(new Intent(this, RecService.class));
        if (web != null) { web.destroy(); web = null; }
        super.onDestroy();
    }

    void http(String cb, String method, String url, String headers, byte[] body, File file) {
        int code = 0; String out = "";
        try {
            String u = url;
            for (int hop = 0; hop < 5; hop++) {
                java.net.HttpURLConnection c = (java.net.HttpURLConnection) new java.net.URL(u).openConnection();
                c.setInstanceFollowRedirects(false);
                c.setConnectTimeout(30000); c.setReadTimeout(300000);
                c.setRequestMethod(method);
                if (hop == 0 && headers != null && !headers.isEmpty()) {
                    JSONObject h = new JSONObject(headers);
                    java.util.Iterator<String> it = h.keys();
                    while (it.hasNext()) { String k = it.next(); c.setRequestProperty(k, h.getString(k)); }
                }
                if (body != null || file != null) {
                    c.setDoOutput(true);
                    long len = file != null ? file.length() : body.length;
                    c.setFixedLengthStreamingMode(len);
                    OutputStream os = c.getOutputStream();
                    if (file != null) {
                        InputStream in = new java.io.FileInputStream(file);
                        byte[] b = new byte[65536]; long sent = 0, last = 0; int n;
                        while ((n = in.read(b)) > 0) {
                            os.write(b, 0, n); sent += n;
                            long now = System.currentTimeMillis();
                            if (now - last > 400) { last = now; js("window.__httpP&&__httpP(" + JSONObject.quote(cb) + "," + sent + "," + len + ")"); }
                        }
                        in.close();
                    } else os.write(body);
                    os.close();
                }
                code = c.getResponseCode();
                if ((code == 301 || code == 302 || code == 303 || code == 307 || code == 308) && c.getHeaderField("Location") != null) {
                    u = new java.net.URL(new java.net.URL(u), c.getHeaderField("Location")).toString();
                    method = "GET"; body = null; file = null; c.disconnect();
                    continue;
                }
                InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
                if (in != null) {
                    java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
                    byte[] b = new byte[65536]; int n;
                    while ((n = in.read(b)) > 0) bo.write(b, 0, n);
                    in.close();
                    out = bo.toString("UTF-8");
                }
                c.disconnect();
                break;
            }
        } catch (Exception e) { code = -1; out = String.valueOf(e.getMessage()); }
        final String js = "window.__http&&__http(" + JSONObject.quote(cb) + "," + code + "," + JSONObject.quote(out) + ")";
        js(js);
    }

    /* ---------- 本機資源(含 COOP/COEP,讓 WASM 可用多執行緒) ---------- */
    class Client extends WebViewClient {
        @Override
        public WebResourceResponse shouldInterceptRequest(WebView v, WebResourceRequest req) {
            Uri u = req.getUrl();
            if (!HOST.equals(u.getHost())) return null;
            String path = u.getPath();
            if (path == null || path.equals("/") || path.isEmpty()) path = "/index.html";
            Map<String, String> h = new HashMap<>();
            h.put("Cross-Origin-Opener-Policy", "same-origin");
            h.put("Cross-Origin-Embedder-Policy", "credentialless");
            h.put("Cross-Origin-Resource-Policy", "same-origin");
            h.put("Cache-Control", "no-cache");
            try {
                InputStream in;
                File ov = new File(getFilesDir(), "www" + path);
                if (path.equals("/index.html") && ov.exists() && !safeMode) in = new java.io.FileInputStream(ov);
                else in = getAssets().open("www" + path);
                return new WebResourceResponse(mime(path), mime(path).startsWith("text") || path.endsWith(".js") || path.endsWith(".mjs") ? "utf-8" : null, 200, "OK", h, in);
            } catch (Exception e) {
                return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", h, new java.io.ByteArrayInputStream(new byte[0]));
            }
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) {
            Uri u = req.getUrl();
            if (HOST.equals(u.getHost())) return false;
            try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception ignored) {}
            return true;
        }

        @Override
        public void onPageFinished(WebView v, String url) {
            pushInsets();
            /* 更新版 10 秒內沒有正常啟動 → 自動還原內建版本 */
            final File ov = new File(getFilesDir(), "www/index.html");
            if (ov.exists() && !safeMode) {
                readyOk = false;
                ui.postDelayed(() -> {
                    if (!readyOk && web != null) {
                        ov.delete(); safeMode = true; web.loadUrl(HOME);
                        android.widget.Toast.makeText(MainActivity.this, "新版介面啟動失敗，已還原內建版本", android.widget.Toast.LENGTH_LONG).show();
                    }
                }, 10000);
            }
        }
    }

    static String mime(String p) {
        p = p.toLowerCase(Locale.ROOT);
        if (p.endsWith(".html")) return "text/html";
        if (p.endsWith(".js") || p.endsWith(".mjs")) return "text/javascript";
        if (p.endsWith(".wasm")) return "application/wasm";
        if (p.endsWith(".css")) return "text/css";
        if (p.endsWith(".json")) return "application/json";
        if (p.endsWith(".png")) return "image/png";
        if (p.endsWith(".svg")) return "image/svg+xml";
        return "application/octet-stream";
    }

    class Chrome extends WebChromeClient {
        @Override
        public void onPermissionRequest(PermissionRequest r) {
            ui.post(() -> {
                List<String> need = new ArrayList<>();
                for (String res : r.getResources()) {
                    if (res.equals(PermissionRequest.RESOURCE_AUDIO_CAPTURE) && !has(Manifest.permission.RECORD_AUDIO)) need.add(Manifest.permission.RECORD_AUDIO);
                    if (res.equals(PermissionRequest.RESOURCE_VIDEO_CAPTURE) && !has(Manifest.permission.CAMERA)) need.add(Manifest.permission.CAMERA);
                }
                if (need.isEmpty()) grantWeb(r);
                else { pendingWebPerm = r; requestPermissions(need.toArray(new String[0]), REQ_WEBPERM); }
            });
        }

        @Override
        public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams p) {
            if (fileCb != null) fileCb.onReceiveValue(null);
            fileCb = cb;
            try {
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                String[] acc = p.getAcceptTypes();
                String a = acc != null && acc.length > 0 ? String.join(",", acc) : "";
                if (a.contains("audio")) { i.setType("*/*"); i.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"audio/*", "video/*"}); }
                else if (a.contains("image")) i.setType("image/*");
                else i.setType("*/*");
                startActivityForResult(Intent.createChooser(i, "選擇檔案"), REQ_FILE);
            } catch (Exception e) { fileCb = null; return false; }
            return true;
        }

        @Override
        public boolean onConsoleMessage(ConsoleMessage m) {
            Log.d("AITM", m.message() + " @" + m.sourceId() + ":" + m.lineNumber());
            return true;
        }
    }

    /* ---------- JS 橋接 window.NB ---------- */
    class Bridge {
        @JavascriptInterface public String info() {
            try {
                PackageInfo pi = getPackageManager().getPackageInfo(getPackageName(), 0);
                JSONObject o = new JSONObject();
                o.put("ver", pi.versionName); o.put("code", pi.versionCode); o.put("sdk", Build.VERSION.SDK_INT); o.put("upd", new File(getFilesDir(), "www/index.html").exists()); o.put("model", Build.MODEL);
                return o.toString();
            } catch (Exception e) { return "{}"; }
        }
        @JavascriptInterface public String perm() { return permJson(); }
        @JavascriptInterface public void requestPerms(String k) { ui.post(() -> MainActivity.this.requestPerms(k)); }
        @JavascriptInterface public void openAppSettings() { ui.post(MainActivity.this::openAppSettings); }
        @JavascriptInterface public void openUrl(String u) { ui.post(() -> { try { startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(u))); } catch (Exception ignored) {} }); }
        @JavascriptInterface public boolean night() { return MainActivity.this.night(); }
        @JavascriptInterface public void ready() { readyOk = true; pushInsets(); }
        /* 從 GitHub 下載的新版介面:存到 App 內部,下次載入時優先使用 */
        @JavascriptInterface public boolean writeApp(String name, String text) {
            if (!"index.html".equals(name)) return false;
            try {
                File d = new File(getFilesDir(), "www"); d.mkdirs();
                File tmp = new File(d, name + ".tmp");
                FileOutputStream fo = new FileOutputStream(tmp); fo.write(text.getBytes("UTF-8")); fo.close();
                return tmp.renameTo(new File(d, name));
            } catch (Exception e) { return false; }
        }
        @JavascriptInterface public void resetApp() { new File(getFilesDir(), "www/index.html").delete(); }

        /* ---------- 原生 HTTP(連 GitHub API／上傳錄音,不受瀏覽器跨站限制) ---------- */
        @JavascriptInterface public void req(String cb, String method, String url, String headers, String body) {
            new Thread(() -> http(cb, method, url, headers, body == null ? null : body.getBytes(java.nio.charset.StandardCharsets.UTF_8), null)).start();
        }
        @JavascriptInterface public int upBegin() {
            try {
                File f = File.createTempFile("up", ".bin", getCacheDir());
                int id = ++saveSeq;
                saves.put(id, new Object[]{new FileOutputStream(f), f.getAbsolutePath()});
                return id;
            } catch (Exception e) { return 0; }
        }
        @JavascriptInterface public boolean upB64(int id, String b) { return saveB64(id, b); }
        @JavascriptInterface public void upSend(int id, String cb, String method, String url, String headers) {
            String path = saveEnd(id);
            new Thread(() -> { File f = new File(path); http(cb, method, url, headers, null, f); f.delete(); }).start();
        }
        @JavascriptInterface public void setBars(boolean dark) {
            ui.post(() -> {
                Window w = getWindow();
                if (Build.VERSION.SDK_INT >= 30) {
                    WindowInsetsController c = w.getInsetsController();
                    if (c != null) {
                        int m = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS;
                        c.setSystemBarsAppearance(dark ? 0 : m, m);
                    }
                } else {
                    View d = w.getDecorView();
                    int f = d.getSystemUiVisibility();
                    int m = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
                    d.setSystemUiVisibility(dark ? f & ~m : f | m);
                }
            });
        }
        @JavascriptInterface public void copy(String t) {
            ui.post(() -> { ClipboardManager cm = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE); if (cm != null) cm.setPrimaryClip(ClipData.newPlainText("text", t)); });
        }
        @JavascriptInterface public String paste() {
            ClipboardManager cm = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            if (cm == null || !cm.hasPrimaryClip()) return "";
            ClipData c = cm.getPrimaryClip();
            if (c == null || c.getItemCount() == 0) return "";
            CharSequence s = c.getItemAt(0).coerceToText(MainActivity.this);
            return s == null ? "" : s.toString();
        }
        @JavascriptInterface public void share(String t, String title) {
            ui.post(() -> {
                Intent i = new Intent(Intent.ACTION_SEND); i.setType("text/plain"); i.putExtra(Intent.EXTRA_TEXT, t); i.putExtra(Intent.EXTRA_SUBJECT, title);
                try { startActivity(Intent.createChooser(i, "分享")); } catch (Exception ignored) {}
            });
        }
        @JavascriptInterface public void speak(String t, String lang) {
            if (!ttsOk) { ui.post(() -> android.widget.Toast.makeText(MainActivity.this, "語音朗讀引擎尚未就緒", android.widget.Toast.LENGTH_SHORT).show()); return; }
            Locale l = Locale.forLanguageTag(lang);
            int r = tts.setLanguage(l);
            if (r == TextToSpeech.LANG_MISSING_DATA || r == TextToSpeech.LANG_NOT_SUPPORTED)
                ui.post(() -> android.widget.Toast.makeText(MainActivity.this, "手機沒有安裝這個語言的朗讀語音", android.widget.Toast.LENGTH_SHORT).show());
            tts.speak(t, TextToSpeech.QUEUE_FLUSH, null, "u" + System.currentTimeMillis());
        }
        @JavascriptInterface public void stopSpeak() { if (tts != null) tts.stop(); }
        @JavascriptInterface public void keepRec(boolean on, String title) {
            ui.post(() -> {
                recording = on;
                Intent i = new Intent(MainActivity.this, RecService.class);
                if (on) {
                    i.putExtra("title", title);
                    if (Build.VERSION.SDK_INT >= 26) startForegroundService(i); else startService(i);
                    getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                } else {
                    stopService(i);
                    getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                }
            });
        }
        /* 檔案輸出:Android 10+ 存到 下載/AI翻譯會議 */
        @JavascriptInterface public int saveBegin(String name, String mime) {
            try {
                OutputStream os; String shown;
                name = name.replaceAll("[\\\\/:*?\"<>|]", "_");
                if (Build.VERSION.SDK_INT >= 29) {
                    ContentValues cv = new ContentValues();
                    cv.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
                    cv.put(MediaStore.MediaColumns.MIME_TYPE, mime);
                    cv.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/AI翻譯會議");
                    Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, cv);
                    if (uri == null) return 0;
                    os = getContentResolver().openOutputStream(uri);
                    shown = "下載/AI翻譯會議/" + name;
                } else {
                    File d = new File(getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS), "AI翻譯會議");
                    d.mkdirs();
                    File f = new File(d, name);
                    os = new FileOutputStream(f);
                    shown = f.getAbsolutePath();
                }
                int id = ++saveSeq;
                saves.put(id, new Object[]{os, shown});
                return id;
            } catch (Exception e) { return 0; }
        }
        @JavascriptInterface public boolean saveText(int id, String t) {
            Object[] o = saves.get(id); if (o == null) return false;
            try { ((OutputStream) o[0]).write(t.getBytes("UTF-8")); return true; } catch (Exception e) { return false; }
        }
        @JavascriptInterface public boolean saveB64(int id, String b) {
            Object[] o = saves.get(id); if (o == null) return false;
            try { ((OutputStream) o[0]).write(Base64.decode(b, Base64.DEFAULT)); return true; } catch (Exception e) { return false; }
        }
        @JavascriptInterface public String saveEnd(int id) {
            Object[] o = saves.remove(id); if (o == null) return "";
            try { ((OutputStream) o[0]).close(); } catch (Exception ignored) {}
            return (String) o[1];
        }
    }
}
