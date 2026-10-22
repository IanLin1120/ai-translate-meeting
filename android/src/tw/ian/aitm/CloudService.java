package tw.ian.aitm;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Iterator;

/* GitHub 雲端轉文字的背景監看:App 在背景或被關掉時,仍定時查詢 GitHub 進度,
   在通知欄顯示進度條,完成或失敗時通知。工作清單存在 SharedPreferences("cloud")。 */
public class CloudService extends Service {
    static final String CH_P = "cloudp", CH_D = "cloudd";
    static volatile boolean appVisible = false;
    Handler h;
    Thread worker;
    volatile boolean run = true;

    static SharedPreferences sp(Context c) { return c.getSharedPreferences("cloud", 0); }

    static JSONObject jobs(Context c) {
        try { return new JSONObject(sp(c).getString("jobs", "{}")); } catch (Exception e) { return new JSONObject(); }
    }

    static void saveJobs(Context c, JSONObject j) { sp(c).edit().putString("jobs", j.toString()).apply(); }

    /* 由 JS 呼叫:開始／更新監看一個工作 */
    static synchronized void watch(Context c, String mid, String title, String repo, String token, long rel, String text, int pct, boolean disp) {
        JSONObject j = jobs(c);
        try {
            JSONObject o = j.optJSONObject(mid);
            if (o == null) o = new JSONObject();
            o.put("title", title);
            if (repo != null && !repo.isEmpty()) o.put("repo", repo);
            if (token != null && !token.isEmpty()) o.put("tok", token);
            if (rel > 0) o.put("rel", rel);
            if (text != null) o.put("text", text);
            o.put("p", pct);
            o.put("disp", disp ? 1 : 0);
            o.put("t", System.currentTimeMillis());
            j.put(mid, o);
        } catch (Exception ignored) {}
        saveJobs(c, j);
        Intent i = new Intent(c, CloudService.class);
        try {
            if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i); else c.startService(i);
        } catch (Exception ignored) {}
    }

    /* 由 JS 呼叫:工作結束(ok=1 完成、0 失敗、-1 取消) */
    static synchronized void done(Context c, String mid, int ok, String msg, boolean notify) {
        JSONObject j = jobs(c);
        JSONObject o = j.optJSONObject(mid);
        j.remove(mid);
        saveJobs(c, j);
        NotificationManager nm = (NotificationManager) c.getSystemService(NOTIFICATION_SERVICE);
        if (nm != null) nm.cancel(1000 + (mid.hashCode() & 0xffff));
        if (notify && ok >= 0) notifyDone(c, mid, o != null ? o.optString("title", "會議") : "會議", ok == 1, msg);
    }

    static void channels(Context c) {
        NotificationManager nm = (NotificationManager) c.getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26 && nm != null) {
            nm.createNotificationChannel(new NotificationChannel(CH_P, "雲端轉文字進度", NotificationManager.IMPORTANCE_LOW));
            NotificationChannel d = new NotificationChannel(CH_D, "雲端轉文字完成", NotificationManager.IMPORTANCE_HIGH);
            d.enableVibration(true);
            nm.createNotificationChannel(d);
        }
    }

    static PendingIntent openApp(Context c, String mid) {
        Intent open = new Intent(c, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP).putExtra("openMeeting", mid);
        return PendingIntent.getActivity(c, mid.hashCode() & 0xffff, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    static void notifyDone(Context c, String mid, String title, boolean ok, String msg) {
        channels(c);
        NotificationManager nm = (NotificationManager) c.getSystemService(NOTIFICATION_SERVICE);
        if (nm == null) return;
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(c, CH_D) : new Notification.Builder(c);
        b.setSmallIcon(R.drawable.ic_stat).setAutoCancel(true).setContentIntent(openApp(c, mid))
                .setContentTitle(ok ? "✅ GitHub 處理完成" : "❌ GitHub 處理失敗")
                .setContentText(title + (msg != null && !msg.isEmpty() ? "・" + msg : ""))
                .setStyle(new Notification.BigTextStyle().bigText(title + (msg != null && !msg.isEmpty() ? "\n" + msg : "") + (ok ? "\n點一下查看逐字稿與重點整理" : "\n點一下開啟 App 查看")));
        nm.notify(2000 + (mid.hashCode() & 0xffff), b.build());
    }

    static String etaS(int s) { return s >= 3600 ? (s / 3600) + " 小時 " + Math.round((s % 3600) / 60.0) + " 分" : s >= 90 ? Math.round(s / 60.0) + " 分鐘" : "1 分鐘內"; }

    Notification progress(String title, String text, int pct, int count) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CH_P) : new Notification.Builder(this);
        b.setSmallIcon(R.drawable.ic_stat).setOngoing(true).setOnlyAlertOnce(true)
                .setContentTitle("☁️ " + title + (count > 1 ? "（另有 " + (count - 1) + " 場）" : ""))
                .setContentText(text == null || text.isEmpty() ? "GitHub 處理中" : text)
                .setContentIntent(openApp(this, "")).setProgress(100, Math.max(0, Math.min(100, pct)), pct < 0);
        return b.build();
    }

    @Override
    public int onStartCommand(Intent i, int flags, int id) {
        channels(this);
        Notification n = progress("雲端轉文字", "準備中", -1, 1);
        try {
            if (Build.VERSION.SDK_INT >= 29) startForeground(999, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
            else startForeground(999, n);
        } catch (Exception e) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (worker == null) {
            h = new Handler(Looper.getMainLooper());
            worker = new Thread(this::loop);
            worker.start();
        }
        return START_STICKY;
    }

    String get(String url, String tok) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(20000);
        c.setReadTimeout(30000);
        c.setRequestProperty("Accept", "application/vnd.github+json");
        c.setRequestProperty("Authorization", "Bearer " + tok);
        c.setRequestProperty("X-GitHub-Api-Version", "2022-11-28");
        int code = c.getResponseCode();
        if (code == 404) return null;
        if (code >= 400) throw new Exception("HTTP " + code);
        InputStream in = c.getInputStream();
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
        in.close();
        return bo.toString("UTF-8");
    }

    void loop() {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        int idle = 0;
        while (run) {
            JSONObject j = jobs(this);
            if (j.length() == 0) {
                if (++idle > 2) break;
            } else idle = 0;
            String firstTitle = null, firstText = null;
            int firstP = -1;
            Iterator<String> it = j.keys();
            java.util.List<String> keys = new java.util.ArrayList<>();
            while (it.hasNext()) keys.add(it.next());
            for (String mid : keys) {
                JSONObject o = j.optJSONObject(mid);
                if (o == null) continue;
                String title = o.optString("title", "會議"), text = o.optString("text", "");
                int p = o.optInt("p", -1);
                long rel = o.optLong("rel", 0);
                String tok = o.optString("tok", ""), repo = o.optString("repo", "");
                /* 太久沒有更新(超過 7 小時)就放棄 */
                if (System.currentTimeMillis() - o.optLong("t0", o.optLong("t", 0)) > 7 * 3600 * 1000L && o.has("t0")) { done(this, mid, 0, "等太久，請開啟 App 重試", true); continue; }
                if (!o.has("t0")) try { o.put("t0", System.currentTimeMillis()); } catch (Exception ignored) {}
                if (rel > 0 && !tok.isEmpty() && !repo.isEmpty() && o.optInt("disp", 1) == 1) {
                    try {
                        String body = get("https://api.github.com/repos/" + repo + "/releases/" + rel, tok);
                        if (body == null) { if (!appVisible) done(this, mid, 0, "GitHub 上的工作已被刪除", true); else done(this, mid, -1, null, false); continue; }
                        JSONObject r = new JSONObject(body);
                        JSONArray as = r.optJSONArray("assets");
                        boolean res = false;
                        if (as != null) for (int k = 0; k < as.length(); k++) if ("result.json".equals(as.getJSONObject(k).optString("name"))) res = true;
                        JSONObject st = new JSONObject();
                        try { st = new JSONObject(r.optString("body", "{}")); } catch (Exception ignored) {}
                        if (res) { done(this, mid, 1, "結果已準備好", !appVisible); continue; }
                        String state = st.optString("state", "");
                        if ("error".equals(state)) { done(this, mid, 0, st.optString("err", "處理失敗"), !appVisible); continue; }
                        if ("running".equals(state)) {
                            p = st.has("p") ? (int) Math.round(st.optDouble("p", -1)) : -1;
                            int eta = st.optInt("eta", -1);
                            text = "GitHub " + st.optString("stage", "處理中") + (p >= 0 ? "　" + p + "%" : "") + (eta > 0 ? "・剩約 " + etaS(eta) : "");
                            o.put("shown", 1);
                        }
                        else if (rel > 0) { text = "等待 GitHub 伺服器啟動"; p = -1; }
                        o.put("text", text); o.put("p", p);
                        JSONObject j2 = jobs(this);
                        if (j2.has(mid)) { j2.put(mid, o); saveJobs(this, j2); }
                    } catch (Exception e) { text = "連線中斷，稍後重試"; }
                }
                if (firstTitle == null) { firstTitle = title; firstText = text + (p >= 0 && !text.contains("%") ? "　" + p + "%" : ""); firstP = p; }
            }
            int cnt = jobs(this).length();
            if (cnt > 0 && firstTitle != null && nm != null) nm.notify(999, progress(firstTitle, firstText, firstP, cnt));
            try { Thread.sleep(appVisible ? 20000 : 15000); } catch (InterruptedException e) { break; }
        }
        worker = null;
        h.post(() -> {
            /* 結束前又有新工作 → 繼續監看 */
            if (run && jobs(this).length() > 0) { worker = new Thread(this::loop); worker.start(); return; }
            stopForeground(true); stopSelf();
        });
    }

    @Override
    public void onDestroy() { run = false; if (worker != null) worker.interrupt(); super.onDestroy(); }

    @Override
    public IBinder onBind(Intent i) { return null; }
}
