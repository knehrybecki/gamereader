#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ICON_SRC="$ROOT/macos/gamereader-icon.png"
HELPER="$ROOT/electron/build/GameReaderHelper"
ICONSET="$ROOT/macos/AppIcon.iconset"
ELEC="$ROOT/electron"
LSREG="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

mkdir -p "$ROOT/electron/build" "$ICONSET"

SDK="$(xcrun --sdk macosx --show-sdk-path)"
swiftc -parse-as-library -O -target arm64-apple-macosx14.2 -sdk "$SDK" \
  -o "$HELPER" "$ROOT/macos/GameReader.swift" "$ROOT/macos/HelperHub.swift" "$ROOT/macos/Ducker.swift" \
  -framework AppKit -framework ScreenCaptureKit -framework CoreMedia -framework Foundation -framework CoreAudio -framework AudioToolbox -framework ImageIO -framework CoreGraphics

if [ -f "$ICON_SRC" ]; then
  sips -z 16 16 "$ICON_SRC" --out "$ICONSET/icon_16x16.png" >/dev/null
  sips -z 32 32 "$ICON_SRC" --out "$ICONSET/icon_16x16@2x.png" >/dev/null
  sips -z 32 32 "$ICON_SRC" --out "$ICONSET/icon_32x32.png" >/dev/null
  sips -z 64 64 "$ICON_SRC" --out "$ICONSET/icon_32x32@2x.png" >/dev/null
  sips -z 128 128 "$ICON_SRC" --out "$ICONSET/icon_128x128.png" >/dev/null
  sips -z 256 256 "$ICON_SRC" --out "$ICONSET/icon_128x128@2x.png" >/dev/null
  sips -z 256 256 "$ICON_SRC" --out "$ICONSET/icon_256x256.png" >/dev/null
  sips -z 512 512 "$ICON_SRC" --out "$ICONSET/icon_256x256@2x.png" >/dev/null
  sips -z 512 512 "$ICON_SRC" --out "$ICONSET/icon_512x512.png" >/dev/null
  sips -z 1024 1024 "$ICON_SRC" --out "$ICONSET/icon_512x512@2x.png" >/dev/null
  iconutil -c icns "$ICONSET" -o "$ELEC/build/icon.icns"
fi

cd "$ELEC"
if [ ! -d node_modules ]; then
  npm install
fi
npx electron-builder --mac dir --arm64 --config.mac.icon="$ELEC/build/icon.icns"

APP="$ELEC/dist/mac-arm64/LiveDub.app"
if [ ! -d "$APP" ]; then
  APP="$ELEC/dist/mac/LiveDub.app"
fi
cp "$HELPER" "$APP/Contents/Resources/GameReaderHelper"
chmod +x "$APP/Contents/Resources/GameReaderHelper"
mkdir -p "$APP/Contents/Resources/engine"
cp "$ROOT/gamereader_engine.py" "$ROOT/gamereader_worker.py" "$ROOT/lektor_brain.py" "$ROOT/omnivoice_sidecar.py" "$ROOT/requirements.txt" "$ROOT/pl_diacritics.tsv" "$APP/Contents/Resources/engine/"

PY_REL="Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python"
PY_APP="/opt/homebrew/Cellar/python@3.14/3.14.7/$PY_REL"
if [ ! -f "$PY_APP" ]; then
  PY_APP="$(brew --prefix python@3.14)/$PY_REL"
fi
ENGINE_APP="$APP/Contents/Helpers/Engine.app"
ENGINE_BUILD="$ELEC/build/Engine.app"
rm -rf "$ENGINE_APP" "$ENGINE_BUILD"
mkdir -p "$ENGINE_APP/Contents/MacOS" "$ENGINE_BUILD/Contents/MacOS"
cp "$ROOT/macos/EngineInfo.plist" "$ENGINE_APP/Contents/Info.plist"
cp "$ROOT/macos/EngineInfo.plist" "$ENGINE_BUILD/Contents/Info.plist"
cp "$PY_APP" "$ENGINE_APP/Contents/MacOS/Engine"
cp "$PY_APP" "$ENGINE_BUILD/Contents/MacOS/Engine"
chmod +x "$ENGINE_APP/Contents/MacOS/Engine" "$ENGINE_BUILD/Contents/MacOS/Engine"
printf 'APPL????' > "$ENGINE_APP/Contents/PkgInfo"
printf 'APPL????' > "$ENGINE_BUILD/Contents/PkgInfo"
codesign --force --sign - "$ENGINE_APP/Contents/MacOS/Engine" || true
codesign --force --sign - "$ENGINE_BUILD" || true

# CI (GitHub Actions): tylko paczka do wydania, bez instalowania w ~/Applications
if [ "${LIVEDUB_NO_INSTALL:-}" = "1" ]; then
  # LIVEDUB_SIGN_ID = stały certyfikat „LiveDub Release” (z sekretów CI) — każda wersja ma ten sam podpis,
  # więc macOS pamięta zgody po aktualizacji; bez niego ad-hoc
  SIGN_ID="${LIVEDUB_SIGN_ID:--}"
  xattr -cr "$APP" || true
  codesign --force --sign "$SIGN_ID" "$APP/Contents/Resources/GameReaderHelper"
  # podpis całej aplikacji na końcu — po dopisaniu silnika i helpera stary podpis Electrona
  # już się nie zgadza i macOS pokazuje „aplikacja jest uszkodzona”
  codesign --force --deep --sign "$SIGN_ID" "$APP"
  codesign --verify --deep --strict "$APP"
  codesign -d -r- "$APP" 2>&1 | grep designated || true
  rm -f "$ELEC/dist/LiveDub-mac-arm64.zip"
  ditto -c -k --keepParent "$APP" "$ELEC/dist/LiveDub-mac-arm64.zip"
  printf 'ZIP_OK %s\n' "$ELEC/dist/LiveDub-mac-arm64.zip"
  exit 0
fi

# lokalny certyfikat (macos/make_signing_cert.sh) = stały podpis, macOS pamięta zgody
SIGN_ID="-"
if security find-identity -v -p codesigning 2>/dev/null | grep -q "LiveDub Local"; then
  SIGN_ID="LiveDub Local"
fi

mkdir -p "$HOME/Applications"
# stara nazwa (GameReader) też znika, żeby nie było dwóch aplikacji
for old in "$HOME/Applications/LiveDub.app" "$HOME/Applications/GameReader.app" "$HOME/Desktop/GameReader.app" "/Applications/GameReader.app"; do
  if [ -e "$old" ]; then
    "$LSREG" -u "$old" >/dev/null 2>&1 || true
    rm -rf "$old"
  fi
done
cp -R "$APP" "$HOME/Applications/LiveDub.app"
xattr -cr "$HOME/Applications/LiveDub.app" || true
codesign --force --sign - "$HOME/Applications/LiveDub.app/Contents/Resources/GameReaderHelper" || true
codesign --force --deep --sign "$SIGN_ID" "$HOME/Applications/LiveDub.app" || true
"$LSREG" -f "$HOME/Applications/LiveDub.app"
printf 'APP_OK %s\n' "$HOME/Applications/LiveDub.app"
