# APK 原始碼

WebView 外殼：從 App 內建資源載入 `index.html`（加上 COOP/COEP 標頭，啟用多執行緒 WASM），並提供 `window.NB` 原生橋接（麥克風／相機權限、前景錄音服務、檔案儲存、朗讀、從 GitHub 更新介面）。

建置前先把 `index.html` 和 `lib/` 複製到 `assets/www/`。建置工具：aapt2、javac、D8、uber-apk-signer（見 `build.sh`）。簽章金鑰不放在儲存庫裡。
