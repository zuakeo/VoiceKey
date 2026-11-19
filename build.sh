#!/usr/bin/env bash
set -e
ROOT="<project-root>/voicekey"
SRC="$ROOT/src"
OUT="$ROOT/build"
TC="<project-root>/toolchain"
JAVA="$TC/jdk-17.0.21+9/bin/java.exe"
JAVAC="$TC/jdk-17.0.21+9/bin/javac.exe"
BT="$TC/sdk/build-tools/36.0.0"
APKSIGNER="$BT/lib/apksigner.jar"
AAPT2="$BT/aapt2.exe"
ZIPALIGN="$BT/zipalign.exe"
AJAR="$TC/sdk/platforms/android-36/android.jar"
KS="C:/Users/<you>/.android/debug.keystore"

rm -rf "$OUT"
mkdir -p "$OUT/classes" "$OUT/dex" "$OUT/gen" "$ROOT/dist"

echo "[1/6] compile resources"
"$AAPT2" compile --dir "$SRC/res" -o "$OUT/res.zip"

echo "[2/6] link"
"$AAPT2" link -o "$OUT/base.apk" -I "$AJAR" \
    --manifest "$SRC/AndroidManifest.xml" \
    --java "$OUT/gen" \
    --min-sdk-version 30 --target-sdk-version 30 \
    --version-code 14 --version-name 0.14 \
    "$OUT/res.zip"

echo "[3/6] compile java"
find "$SRC/java" "$OUT/gen" -name "*.java" > "$OUT/src.txt"
"$JAVAC" -encoding UTF-8 --release 8 -nowarn -cp "$AJAR" -d "$OUT/classes" @"$OUT/src.txt"

echo "[4/6] dex"
find "$OUT/classes" -name "*.class" > "$OUT/cls.txt"
"$JAVA" -cp "$BT/lib/d8.jar" com.android.tools.r8.D8 --min-api 30 --lib "$AJAR" --output "$OUT/dex" @"$OUT/cls.txt"

echo "[5/6] inject dex"
python "$ROOT/tools/inject.py" "$OUT/base.apk" "$OUT/dex/classes.dex" "$OUT/merged.apk"

echo "[6/6] align + sign"
"$ZIPALIGN" -f -p 4 "$OUT/merged.apk" "$OUT/aligned.apk"
"$JAVA" -jar "$APKSIGNER" sign \
    --ks "$KS" --ks-pass pass:android --key-pass pass:android \
    --ks-key-alias androiddebugkey \
    --out "$ROOT/dist/VoiceKey-0.14.apk" "$OUT/aligned.apk"

"$JAVA" -jar "$APKSIGNER" verify --print-certs "$ROOT/dist/VoiceKey-0.14.apk" | head -3
ls -lh "$ROOT/dist/VoiceKey-0.14.apk"
echo "BUILD OK"
