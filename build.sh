#!/usr/bin/env bash
# VoiceKey —— 无 Gradle 构建脚本（aapt2 + javac + d8 + apksigner）
#
# 需要先设置好这些环境变量（或直接改下面默认值）：
#   ANDROID_SDK_ROOT   例：$HOME/Android/Sdk 或 D:/sdk
#   BUILD_TOOLS        例：34.0.0
#   ANDROID_JAR        例：$ANDROID_SDK_ROOT/platforms/android-33/android.jar
#   JAVA_HOME          JDK 17
#   DEBUG_KEYSTORE     调试签名，没有就先 keytool 生成一个
#
# 用法：bash build.sh            产出 dist/VoiceKey.apk
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$ROOT/src"
OUT="$ROOT/build"

SDK="${ANDROID_SDK_ROOT:?请先设置 ANDROID_SDK_ROOT}"
BT_NAME="${BUILD_TOOLS:-34.0.0}"
BT="$SDK/build-tools/$BT_NAME"
AJAR="${ANDROID_JAR:-$SDK/platforms/android-33/android.jar}"
JAVA="${JAVA_HOME:-/usr}/bin/java"
JAVAC="${JAVA_HOME:-/usr}/bin/javac"
KS="${DEBUG_KEYSTORE:-$HOME/.android/debug.keystore}"
VERSION_CODE="${VERSION_CODE:-13}"
VERSION_NAME="${VERSION_NAME:-0.13}"

rm -rf "$OUT"
mkdir -p "$OUT/classes" "$OUT/dex" "$OUT/gen" "$ROOT/dist"

echo "[1/6] 编译资源"
"$BT/aapt2" compile --dir "$SRC/res" -o "$OUT/res.zip"

echo "[2/6] 链接资源 + 清单"
"$BT/aapt2" link -o "$OUT/base.apk" -I "$AJAR" \
    --manifest "$SRC/AndroidManifest.xml" \
    --java "$OUT/gen" \
    --min-sdk-version 30 --target-sdk-version 30 \
    --version-code "$VERSION_CODE" --version-name "$VERSION_NAME" \
    "$OUT/res.zip"

echo "[3/6] 编译 java"
"$JAVAC" -encoding UTF-8 --release 8 -nowarn -cp "$AJAR" -d "$OUT/classes" \
    $(find "$SRC/java" "$OUT/gen" -name "*.java")

echo "[4/6] 转 dex"
"$JAVA" -cp "$BT/lib/d8.jar" com.android.tools.r8.D8 \
    --min-api 30 --lib "$AJAR" --output "$OUT/dex" \
    $(find "$OUT/classes" -name "*.class")

echo "[5/6] 塞进 apk"
python "$ROOT/tools/inject.py" "$OUT/base.apk" "$OUT/dex/classes.dex" "$OUT/merged.apk"

echo "[6/6] 对齐 + 签名"
"$BT/zipalign" -f -p 4 "$OUT/merged.apk" "$OUT/aligned.apk"
"$JAVA" -jar "$BT/lib/apksigner.jar" sign \
    --ks "$KS" --ks-pass pass:android --key-pass pass:android \
    --ks-key-alias androiddebugkey \
    --out "$ROOT/dist/VoiceKey-$VERSION_NAME.apk" "$OUT/aligned.apk"

"$JAVA" -jar "$BT/lib/apksigner.jar" verify "$ROOT/dist/VoiceKey-$VERSION_NAME.apk" | head -1
ls -lh "$ROOT/dist/VoiceKey-$VERSION_NAME.apk"
echo "BUILD OK"
