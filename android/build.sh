#!/bin/bash
# 建置 AI翻譯會議.apk:aapt2 + javac + D8 + uber-apk-signer
set -e
T=~/tools; AAPT=$T/x_aaptjs3-2.0.2/package/bin/x64/linux/aapt2
rm -rf build && mkdir -p build/flat build/gen build/cls build/dex
$AAPT compile --dir res -o build/flat/res.zip
$AAPT link -o build/base.apk -I $T/android.jar --manifest AndroidManifest.xml -A assets --java build/gen --auto-add-overlay build/flat/res.zip
javac --release 8 -cp $T/android.jar -encoding UTF-8 -nowarn -d build/cls $(find src build/gen -name '*.java') 2>&1 | grep -v -E "JAVA_TOOL|warning" || true
java -cp $T/jadx/lib/jadx-1.5.3-all.jar com.android.tools.r8.D8 --release --min-api 26 --lib $T/android.jar --output build/dex $(find build/cls -name '*.class') 2>&1 | grep -v JAVA_TOOL || true
cp build/base.apk build/unsigned.apk && (cd build/dex && zip -q ../unsigned.apk classes.dex)
java -jar $T/uber.jar -a build/unsigned.apk --ks release.jks --ksAlias aitm --ksPass aitm2026 --ksKeyPass aitm2026 -o build/out 2>&1 | grep -E "verified|ERROR"
cp build/out/unsigned-aligned-signed.apk AI翻譯會議.apk
