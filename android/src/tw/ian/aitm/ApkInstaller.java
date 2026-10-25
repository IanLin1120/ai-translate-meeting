package tw.ian.aitm;

import android.app.Activity;
import android.app.DownloadManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.Settings;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/* 在 App 裡直接下載並安裝新版 APK（覆蓋舊版，資料保留）：用系統的 PackageInstaller，不需要額外函式庫 */
public class ApkInstaller {
    public interface Cb { void on(String state, int pct, String msg); }
    static Cb cb;

    static boolean canInstall(Context c) {
        return Build.VERSION.SDK_INT < 26 || c.getPackageManager().canRequestPackageInstalls();
    }

    static void askPermission(Activity a) {
        try {
            a.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + a.getPackageName())));
        } catch (Exception e) {
            a.startActivity(new Intent(Settings.ACTION_SECURITY_SETTINGS));
        }
    }

    static void install(Activity a, String url, Cb callback) {
        cb = callback;
        new Thread(() -> {
            PackageInstaller.Session s = null;
            try {
                cb.on("download", 0, "");
                HttpURLConnection c = null;
                String u = url;
                for (int i = 0; i < 6; i++) {  // GitHub 會轉址到下載伺服器
                    c = (HttpURLConnection) new URL(u).openConnection();
                    c.setInstanceFollowRedirects(false);
                    c.setConnectTimeout(20000);
                    c.setReadTimeout(60000);
                    int code = c.getResponseCode();
                    if (code >= 300 && code < 400) { u = new URL(new URL(u), c.getHeaderField("Location")).toString(); c.disconnect(); continue; }
                    if (code != 200) throw new Exception("下載失敗（HTTP " + code + "）");
                    break;
                }
                long total = c.getContentLengthLong();
                PackageInstaller pi = a.getPackageManager().getPackageInstaller();
                PackageInstaller.SessionParams p = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
                p.setAppPackageName(a.getPackageName());
                if (total > 0) p.setSize(total);
                int sid = pi.createSession(p);
                s = pi.openSession(sid);
                try (InputStream in = c.getInputStream(); OutputStream out = s.openWrite("app.apk", 0, total > 0 ? total : -1)) {
                    byte[] buf = new byte[1 << 16];
                    long got = 0, last = 0;
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        out.write(buf, 0, n);
                        got += n;
                        if (total > 0 && got - last > total / 50) { last = got; cb.on("download", (int) (got * 100 / total), ""); }
                    }
                    s.fsync(out);
                }
                if (total > 0 && s != null) cb.on("download", 100, "");
                cb.on("install", 100, "");
                Intent i = new Intent(a, Receiver.class);
                int fl = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0);
                s.commit(PendingIntent.getBroadcast(a, sid, i, fl).getIntentSender());
                s.close();
            } catch (Exception e) {
                if (s != null) try { s.abandon(); } catch (Exception ignored) {}
                cb.on("error", 0, String.valueOf(e.getMessage()));
            }
        }).start();
    }

    /* 只下載 APK 檔到「下載」資料夾 */
    static void download(Context c, String url, String name) {
        DownloadManager dm = (DownloadManager) c.getSystemService(Context.DOWNLOAD_SERVICE);
        DownloadManager.Request r = new DownloadManager.Request(Uri.parse(url));
        r.setTitle(name);
        r.setDescription("AI翻譯會議");
        r.setMimeType("application/vnd.android.package-archive");
        r.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
        r.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
        dm.enqueue(r);
    }

    public static class Receiver extends BroadcastReceiver {
        @Override
        public void onReceive(Context c, Intent i) {
            int st = i.getIntExtra(PackageInstaller.EXTRA_STATUS, -999);
            if (st == PackageInstaller.STATUS_PENDING_USER_ACTION) {
                Intent confirm = i.getParcelableExtra(Intent.EXTRA_INTENT);
                if (confirm != null) { confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK); c.startActivity(confirm); }
                if (cb != null) cb.on("confirm", 100, "");
            } else if (st == PackageInstaller.STATUS_SUCCESS) {
                if (cb != null) cb.on("done", 100, "");
            } else if (cb != null) {
                String m = i.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
                if (st == PackageInstaller.STATUS_FAILURE_CONFLICT || (m != null && m.contains("DOWNGRADE")))
                    m = "這是比較舊的版本，Android 不允許直接覆蓋安裝（要先解除安裝目前的 App，資料會被清除）";
                else if (st == PackageInstaller.STATUS_FAILURE_ABORTED) m = "已取消安裝";
                cb.on("error", 0, m == null ? "安裝失敗" : m);
            }
        }
    }
}
