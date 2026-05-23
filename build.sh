#!/bin/bash
# build.sh -- Build Lytrize .deb package
# Usage: bash build.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP=lytrize
BUILD=build/$APP
VENV_TARGET=/opt/$APP/venv
VENV_BUILD=$BUILD$VENV_TARGET

echo "======================================"
echo "  Lytrize .deb builder"
echo "  Output: $SCRIPT_DIR/$BUILD.deb"
echo "======================================"
echo ""

# Verify build tools
for tool in python3 dpkg-deb; do
    if ! command -v "$tool" &>/dev/null; then
        echo "ERROR: '$tool' not found. Install with: sudo apt install $tool"
        exit 1
    fi
done

echo "[1/7] Cleaning old build..."
rm -rf build
mkdir -p "$BUILD/opt/$APP"

echo "[2/7] Copying app files..."
cp -r backend  "$BUILD/opt/$APP/"
cp -r desktop  "$BUILD/opt/$APP/"
cp service/lytrize.service "$BUILD/opt/$APP/"

# Remove Python caches from the source copy
find "$BUILD/opt/$APP" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "$BUILD/opt/$APP" -name '*.pyc' -o -name '*.pyo' -delete 2>/dev/null || true

# Remove stray editor copy files (filenames with spaces break packaging)
find "$BUILD/opt/$APP" -name "* (copy*).py" -delete 2>/dev/null || true
find "$BUILD/opt/$APP" -name "* (Copy*).py" -delete 2>/dev/null || true

# Leave the bundled .env offline-safe. Cloud sync now uses Supabase Auth plus
# the public anon key, not a PostgreSQL password. Do not ship any secret DB URL
# in the desktop package.
ENV_FILE="$BUILD/opt/$APP/backend/.env"
if [ -f "$ENV_FILE" ]; then
    cat >> "$ENV_FILE" << 'ENVEOF'

# Supabase public configuration (not a database password):
#   LYTRIZE_SUPABASE_URL=https://<project-ref>.supabase.co
#   LYTRIZE_SUPABASE_ANON_KEY=<public anon key>
#
# For packaged builds, place these in ~/.local/share/lytrize/sync.env or set
# them in the environment before launch.
ENVEOF
fi

echo "[3/7] Creating virtual environment..."
python3 -m venv "$VENV_BUILD"

echo "[4/7] Installing Python dependencies..."
"$VENV_BUILD/bin/pip" install --upgrade pip --quiet
"$VENV_BUILD/bin/pip" install \
    "streamlit>=1.40.0" \
    "pandas>=2.2.0" \
    "plotly>=5.22.0" \
    "openpyxl>=3.1.0" \
    "python-dotenv>=1.0.0" \
    "requests>=2.31.0" \
    "psycopg2-binary>=2.9.9" \
    "statsmodels>=0.14.0" \
    --quiet
echo "      Installing PySide6 (may take 2-3 minutes)..."
"$VENV_BUILD/bin/pip" install PySide6 --quiet

echo "[5/7] Patching venv shebangs for portability..."
VENV_BUILD_ABS="$(realpath "$VENV_BUILD")"
find "$VENV_BUILD/bin" -maxdepth 1 -type f | while read -r f; do
    if head -c 2 "$f" 2>/dev/null | grep -q '#!'; then
        sed -i "1s|#!${VENV_BUILD_ABS}/bin/python[0-9.]*|#!/opt/lytrize/venv/bin/python3|g" "$f"
    fi
done
PYDIR="$(dirname "$(readlink -f "$(which python3)")")"
sed -i "s|^home = .*|home = $PYDIR|" "$VENV_BUILD/pyvenv.cfg" 2>/dev/null || true

echo "[6/7] Slimming venv..."
find "$VENV_BUILD" -type d \( -name '__pycache__' -o -name 'tests' -o -name 'test' -o -name 'docs' \) \
    -exec rm -rf {} + 2>/dev/null || true
find "$VENV_BUILD" -name '*.pyc' -o -name '*.pyo' -delete 2>/dev/null || true

echo "[7/7] Building .deb package..."
cp -r packaging/DEBIAN "$BUILD/"
cp -r packaging/usr    "$BUILD/"

find "$BUILD" -type d -exec chmod 755 {} \;
find "$BUILD" -type f -exec chmod 644 {} \;
chmod 755 "$BUILD/usr/local/bin/lytrize"
chmod 755 "$BUILD/DEBIAN/postinst"
chmod 755 "$BUILD/DEBIAN/postrm"
chmod 755 "$BUILD/opt/$APP/desktop/gui.py"
chmod 755 "$BUILD/opt/$APP/desktop/launcher.py"
find "$VENV_BUILD/bin" -type f -exec chmod 755 {} \;

dpkg-deb --build --root-owner-group "$BUILD"

echo ""
echo "======================================"
echo "  Build complete!"
echo "  Package : $SCRIPT_DIR/$BUILD.deb"
echo "  Size    : $(du -sh "$BUILD.deb" | cut -f1)"
echo ""
echo "  Install : sudo dpkg -i $BUILD.deb"
echo "  Launch  : lytrize"
echo "======================================"
