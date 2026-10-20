# AI翻譯會議

開源 AI 翻譯與會議記錄 App：**文字／圖片／拍照／即時語音翻譯**，以及能**分辨講者、中英夾雜、不限時長**的會議語音轉文字。
所有 AI 都在手機或瀏覽器裡運算，錄音與文字不會上傳。功能、函式庫和 AI 模型都放在這個 GitHub 儲存庫，App 需要時直接從這裡下載。

- 🌐 **網頁版**：<https://ianlin1120.github.io/ai-translate-meeting/>
- 📱 **Android APK**：[Releases](https://github.com/IanLin1120/ai-translate-meeting/releases/latest)

## 儲存庫結構

| 位置 | 內容 | App 如何使用 |
|---|---|---|
| `main` 分支 → GitHub Pages | `index.html`（整個 App）、`lib/`（Transformers.js、ONNX Runtime、Tesseract、OpenCC）、`lib/tessdata/`（18 種語言 OCR 語言包）、`version.json` | 網頁版直接執行；APK 開啟時檢查 `version.json`，有新版就自動下載新的 `index.html`（不用重裝 APK） |
| `models` 分支 | 開源 AI 模型（ONNX），超過 90 MB 的檔案切成 `.part000`、`.part001`…；`manifest.json` 記錄大小、sha256、分片數 | 透過 `raw.githubusercontent.com` 下載並自動把分片接回；GitHub 沒有的檔案才改用 Hugging Face |
| `models.json` | 要鏡像的模型清單 | 修改後推送到 `main`，GitHub Actions 會自動同步 |
| `android/` | APK 原始碼（WebView 外殼＋原生橋接） | — |

## ☁️ GitHub 雲端會議轉文字

會議錄音和上傳的音檔改在 GitHub Actions 的伺服器上辨識，手機不用下載語音／講者模型：

1. App 把錄音上傳到這個儲存庫的**草稿 Release**（只有擁有者看得到，不會進 Git 歷史）
2. App 觸發 [`transcribe.yml`](.github/workflows/transcribe.yml)：faster-whisper（large-v3-turbo，中英夾雜）＋ sherpa-onnx 講者辨識（pyannote segmentation 3.0 ＋ 3D-Speaker ERes2Net）
3. 伺服器下載錄音後**立刻從 GitHub 刪除**；結果 `result.json` 放回草稿，App 取回後**刪除整個草稿**
4. [`cleanup-jobs.yml`](.github/workflows/cleanup-jobs.yml) 每小時刪除超過 6 小時沒被取回的草稿

公開儲存庫的 Actions 紀錄任何人都看得到，所以腳本不會在紀錄裡印出逐字稿內容。

App 需要一組 **Fine-grained 權杖**：Repository access 只選本儲存庫，Permissions 把 **Contents** 和 **Actions** 設成 Read and write，填到 App「設定 → ☁️ GitHub 雲端轉文字」。

## 模型同步

[`.github/workflows/sync-models.yml`](.github/workflows/sync-models.yml) 會從 Hugging Face 下載 `models.json` 列出的模型，切片後提交到 `models` 分支。
- 自動：`models.json` 有變動並推送到 `main` 時執行
- 手動：Actions → 「同步 AI 模型到 GitHub」→ Run workflow
- 已同步、內容沒變的檔案會跳過；Hugging Face 上的模型更新後再執行一次就會同步新版

新增模型：在 `models.json` 加一筆 `{ "id": "Hugging Face 模型名稱", "onnx": ["要下載的 ONNX 檔名"] }`。

## 使用的開源模型與授權

| 功能 | 模型 | 授權 |
|---|---|---|
| 語音轉文字 | OpenAI Whisper tiny / base / small / large-v3-turbo（[onnx-community](https://huggingface.co/onnx-community)） | MIT |
| 講者分段 | pyannote segmentation 3.0 | MIT |
| 講者聲紋 | Microsoft WavLM Base Plus SV | MIT |
| 翻譯 中↔英 | Helsinki-NLP Opus-MT | CC-BY 4.0 |
| 翻譯 200 種語言 | Meta NLLB-200 distilled 600M | **CC-BY-NC 4.0（僅限非商業用途）** |
| 翻譯 100 種語言 | Meta M2M100 418M | MIT |
| LLM 翻譯 | Alibaba Qwen2.5 0.5B / 1.5B Instruct | Apache 2.0 |
| 圖片文字辨識 | Tesseract OCR（tessdata_best int） | Apache 2.0 |
| 推論引擎 | Hugging Face Transformers.js 4.3.1、ONNX Runtime Web | Apache 2.0 / MIT |
| 繁簡轉換 | OpenCC（opencc-js） | Apache 2.0 / MIT |

模型檔案是從 Hugging Face 原樣鏡像過來的，版權屬於各自的作者，使用時請遵守原授權。
