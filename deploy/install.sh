#!/usr/bin/env bash
# Idempotent install / update for the Songwriter server.
#
# First run prompts for every setting (bracketed defaults; Enter keeps them),
# writes them to a chmod 600 <install dir>/deploy/.env, clones the repo,
# installs dependencies with uv, fetches the runtime DuckDB files, and either
# prints the launch command or (with --service) installs a systemd unit.
# Re-running updates code and dependencies and reuses the saved settings.
#
# Flags:
#   --reconfigure       re-prompt for every setting, using saved values as defaults
#   --service           create the service user and install + start the systemd unit (root, Linux)
#   --non-interactive   never prompt; take settings from the environment / saved .env / defaults
#   -h, --help          show this help
set -euo pipefail

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }

RECONFIGURE=0
INSTALL_SERVICE=0
INTERACTIVE=1
for arg in "$@"; do
  case "$arg" in
    --reconfigure) RECONFIGURE=1 ;;
    --service) INSTALL_SERVICE=1 ;;
    --non-interactive) INTERACTIVE=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $arg (see --help)" >&2; exit 2 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------- defaults
DEF_REPO="https://github.com/rayone/songwriter.git"
DEF_REF="main"
DEF_RELEASE_TAG="v0.2.0"
DEF_HOME="/opt/songwriter"
DEF_LLM_ENDPOINT="http://127.0.0.1:11434/v1"
DEF_HOST="0.0.0.0"
DEF_PORT="8000"
DEF_AUTH_USER=""
SERVICE_USER="songwriter"
SETTINGS="SONGWRITER_REPO SONGWRITER_REF SONGWRITER_RELEASE_TAG SONGWRITER_HOME SONGWRITER_DATA_DIR SONGWRITER_DB_URL
SONGWRITER_LLM_ENDPOINT SONGWRITER_LLM_API_KEY SONGWRITER_HOST SONGWRITER_PORT
SONGWRITER_AUTH_USER SONGWRITER_AUTH_PASSWORD SONGWRITER_CORS_ORIGINS GITHUB_TOKEN"
if [ "$INSTALL_SERVICE" -eq 1 ]; then
  [ "$(uname -s)" = "Linux" ] || die "--service needs Linux with systemd."
  command -v systemctl >/dev/null 2>&1 || die "--service needs systemd (systemctl not found)."
  [ "$(id -u)" -eq 0 ] || die "--service must run as root: sudo bash $0 --service"
fi

# ---------------------------------------------------- locate install + config
# The install directory is, in order: $SONGWRITER_HOME, the checkout this script
# lives in (when it is an existing install), or the default. Settings always
# live in <install dir>/deploy/.env -- never next to a downloaded copy of this
# script, so an unrelated ~/.env is never read.
if [ -z "${SONGWRITER_HOME:-}" ]; then
  if [ -f "$SCRIPT_DIR/.env" ] && [ -d "$SCRIPT_DIR/../.git" ]; then
    SONGWRITER_HOME="$(cd "$SCRIPT_DIR/.." && pwd)"
  else
    SONGWRITER_HOME="$DEF_HOME"
  fi
fi
ENV_FILE="$SONGWRITER_HOME/deploy/.env"
HAVE_ENV=0
if [ -f "$ENV_FILE" ]; then
  HAVE_ENV=1
  # Values already set in the environment win over the saved file.
  for v in $SETTINGS; do
    if [ -n "${!v+x}" ]; then printf -v "__override_$v" '%s' "${!v}"; fi
  done
  set -a
  # shellcheck source=/dev/null
  . "$ENV_FILE"
  set +a
  for v in $SETTINGS; do
    o="__override_$v"
    if [ -n "${!o+x}" ]; then printf -v "$v" '%s' "${!o}"; fi
  done
  echo "Using saved settings in $ENV_FILE"
fi

# prompt VAR "label" "default" [--secret]
prompt() {
  local var="$1" label="$2" default="$3" secret="${4:-}" input shown
  shown="${!var:-$default}"
  if [ "$secret" = "--secret" ]; then
    if [ -n "$shown" ]; then read -rsp "$label [keep existing]: " input; else read -rsp "$label: " input; fi
    echo
  else
    read -rp "$label [$shown]: " input
  fi
  printf -v "$var" '%s' "${input:-$shown}"
}

if [ "$INTERACTIVE" -eq 1 ] && { [ "$HAVE_ENV" -eq 0 ] || [ "$RECONFIGURE" -eq 1 ]; }; then
  [ -t 0 ] || die "No terminal to prompt on. Download the script and run it with bash, or pass --non-interactive with SONGWRITER_* variables set."
  echo "Configure the Songwriter deployment (Enter keeps the bracketed value):"
  prompt SONGWRITER_REPO          "Git clone URL (private repo: use an SSH URL or a token URL)" "$DEF_REPO"
  prompt SONGWRITER_REF           "Branch or tag to deploy"            "$DEF_REF"
  prompt SONGWRITER_RELEASE_TAG   "GitHub release tag for databases"   "$DEF_RELEASE_TAG"
  prompt SONGWRITER_HOME          "Install directory"                  "$SONGWRITER_HOME"
  prompt SONGWRITER_DATA_DIR      "Runtime data directory"             "$SONGWRITER_HOME/data"
  prompt SONGWRITER_DB_URL        "Database source (blank = pull automatically from GitHub release assets)" ""
  prompt SONGWRITER_LLM_API_KEY   "Cowriter LLM API key"               "" --secret
  prompt SONGWRITER_HOST          "HTTP bind address (127.0.0.1 = this machine only)" "$DEF_HOST"
  prompt SONGWRITER_PORT          "HTTP port"                          "$DEF_PORT"
  prompt SONGWRITER_AUTH_USER     "Login username ('none' disables login; only safe on 127.0.0.1)" "$DEF_AUTH_USER"
  if [ -n "$SONGWRITER_AUTH_USER" ] && [ "$SONGWRITER_AUTH_USER" != "none" ]; then
    prompt SONGWRITER_AUTH_PASSWORD "Login password (blank = generate one)" "" --secret
  fi
  prompt SONGWRITER_CORS_ORIGINS  "Extra browser origins allowed cross-origin, comma-separated (usually blank)" ""
fi

: "${SONGWRITER_REPO:=$DEF_REPO}"
: "${SONGWRITER_REF:=$DEF_REF}"
: "${SONGWRITER_RELEASE_TAG:=$DEF_RELEASE_TAG}"
: "${SONGWRITER_DATA_DIR:=$SONGWRITER_HOME/data}"
: "${SONGWRITER_DB_URL:=}"
: "${GITHUB_TOKEN:=}"
: "${SONGWRITER_LLM_ENDPOINT:=$DEF_LLM_ENDPOINT}"
: "${SONGWRITER_LLM_API_KEY:=}"
: "${SONGWRITER_HOST:=$DEF_HOST}"
: "${SONGWRITER_PORT:=$DEF_PORT}"
# '=' not ':=': an explicitly empty login (or 'none') means "no login", not the default.
: "${SONGWRITER_AUTH_USER=$DEF_AUTH_USER}"
case "$SONGWRITER_AUTH_USER" in none|NONE|-) SONGWRITER_AUTH_USER="" ;; esac
if [ -z "$SONGWRITER_AUTH_USER" ]; then SONGWRITER_AUTH_PASSWORD=""; fi
: "${SONGWRITER_AUTH_PASSWORD:=}"
: "${SONGWRITER_CORS_ORIGINS:=}"
ENV_FILE="$SONGWRITER_HOME/deploy/.env"

# ------------------------------------------------------------ validate
case "$SONGWRITER_PORT" in ''|*[!0-9]*) die "SONGWRITER_PORT must be a number, got '$SONGWRITER_PORT'." ;; esac
[ "$SONGWRITER_PORT" -ge 1 ] && [ "$SONGWRITER_PORT" -le 65535 ] || die "SONGWRITER_PORT must be 1-65535."
case "$SONGWRITER_HOME" in /*) ;; *) die "Install directory must be an absolute path." ;; esac
case "$SONGWRITER_DATA_DIR" in /*) ;; *) die "Data directory must be an absolute path." ;; esac
for v in $SETTINGS; do
  case "${!v}" in *"'"*|*$'\n'*) die "$v must not contain a single quote or a newline." ;; esac
done

case "$SONGWRITER_HOST" in 127.*|localhost|::1) LOOPBACK=1 ;; *) LOOPBACK=0 ;; esac
GENERATED_PASSWORD=0
if [ -n "$SONGWRITER_AUTH_USER" ] && [ -z "$SONGWRITER_AUTH_PASSWORD" ]; then
  SONGWRITER_AUTH_PASSWORD="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24 || true)"
  [ ${#SONGWRITER_AUTH_PASSWORD} -eq 24 ] || die "Could not generate a password."
  GENERATED_PASSWORD=1
fi

# ------------------------------------------------------------ prerequisites
for bin in git curl; do command -v "$bin" >/dev/null 2>&1 || die "$bin is required; install it and re-run."; done
PARENT="$(dirname "$SONGWRITER_HOME")"
mkdir -p "$PARENT" 2>/dev/null || true
{ [ -d "$SONGWRITER_HOME" ] && [ -w "$SONGWRITER_HOME" ]; } || [ -w "$PARENT" ] \
  || die "Cannot write to $SONGWRITER_HOME. Re-run with sudo or choose a writable install directory."

# ------------------------------------------------------------ clone / update
if [ ! -d "$SONGWRITER_HOME/.git" ]; then
  # A pre-existing install dir may only hold data/ (databases copied ahead of
  # time) or deploy/ (a saved .env). Anything else is someone else's directory.
  if [ -d "$SONGWRITER_HOME" ]; then
    for entry in "$SONGWRITER_HOME"/* "$SONGWRITER_HOME"/.[!.]*; do
      [ -e "$entry" ] || continue
      case "$(basename "$entry")" in
        data|deploy) ;;
        *) die "$SONGWRITER_HOME exists and holds files other than data/ and deploy/; it is not a git checkout." ;;
      esac
    done
  fi
  echo "Cloning $SONGWRITER_REPO ($SONGWRITER_REF) -> $SONGWRITER_HOME"
  TMP_CLONE="$(mktemp -d "$PARENT/.songwriter-clone.XXXXXX")"
  git clone --branch "$SONGWRITER_REF" "$SONGWRITER_REPO" "$TMP_CLONE" \
    || { rm -rf "$TMP_CLONE"; die "git clone failed. For a private repo, use an SSH URL with a deploy key, or https://<token>@github.com/... ."; }
  # Keep a previously written deploy/.env (e.g. from a failed first run).
  if [ -f "$ENV_FILE" ]; then cp -p "$ENV_FILE" "$TMP_CLONE/deploy/.env"; fi
  if [ -d "$SONGWRITER_HOME" ]; then
    # Merge the checkout in; data/ is gitignored, so it is never touched.
    rm -rf "$SONGWRITER_HOME/deploy"
    (shopt -s dotglob; mv "$TMP_CLONE"/* "$SONGWRITER_HOME"/)
    rmdir "$TMP_CLONE"
  else
    mv "$TMP_CLONE" "$SONGWRITER_HOME"
  fi
  # mktemp -d creates the directory as 700; the service user must traverse it.
  chmod 755 "$SONGWRITER_HOME"
else
  echo "Updating $SONGWRITER_HOME to $SONGWRITER_REF"
  git -C "$SONGWRITER_HOME" fetch --tags origin
  git -C "$SONGWRITER_HOME" checkout -q "$SONGWRITER_REF"
  if git -C "$SONGWRITER_HOME" symbolic-ref -q HEAD >/dev/null; then
    git -C "$SONGWRITER_HOME" pull --ff-only origin "$SONGWRITER_REF" \
      || die "Fast-forward update failed; local changes in $SONGWRITER_HOME conflict with $SONGWRITER_REF."
  fi
fi

# ------------------------------------------------------------ write settings
# Single-quoted values parse identically in bash and in systemd EnvironmentFile.
mkdir -p "$SONGWRITER_HOME/deploy"
(
  umask 077
  {
    echo "# Written by deploy/install.sh. Contains secrets: keep chmod 600, never commit."
    for v in $SETTINGS; do printf "%s='%s'\n" "$v" "${!v}"; done
  } > "$ENV_FILE.tmp"
)
mv "$ENV_FILE.tmp" "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "Saved settings to $ENV_FILE"

# ------------------------------------------------------------ dependencies
# Python and the venv live inside the install tree, so the service user can
# run them without access to root's home directory.
export UV_PYTHON_INSTALL_DIR="$SONGWRITER_HOME/.uv-python"
export UV_LINK_MODE=copy
if ! command -v uv >/dev/null 2>&1; then
  if [ "$(id -u)" -eq 0 ]; then export UV_INSTALL_DIR=/usr/local/bin; else export UV_INSTALL_DIR="$HOME/.local/bin"; fi
  echo "Installing uv to $UV_INSTALL_DIR"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  export PATH="$UV_INSTALL_DIR:$PATH"
fi
echo "Syncing dependencies"
uv sync --frozen --no-dev --directory "$SONGWRITER_HOME"
PYTHON="$SONGWRITER_HOME/.venv/bin/python"
"$PYTHON" -c "import songwriter.config" || die "Dependency install did not produce a working environment."

# ------------------------------------------------------------ runtime data
APP_DB="$SONGWRITER_DATA_DIR/app.duckdb"
SONGS_PQ="$SONGWRITER_DATA_DIR/songs.parquet"

# Extract repo owner and name (e.g. rayone/songwriter)
GH_REPO_SLUG=""
if [[ "$SONGWRITER_REPO" =~ github\.com[:/]([^/]+/[^/.]+)(\.git)?$ ]]; then
  GH_REPO_SLUG="${BASH_REMATCH[1]}"
fi

# download SRC DEST: copy one file into DEST via a .part file, so an interrupted
# transfer is never mistaken for a complete one on the next run.
download() {
  local src="$1" dest="$2" part="$2.part"
  rm -f "$part"
  case "$src" in
    s3://*)
      command -v aws >/dev/null 2>&1 || die "$src needs the AWS CLI (aws) installed."
      aws s3 cp --only-show-errors "$src" "$part" ;;
    http://*|https://*|file://*)
      curl -fL --retry 3 --progress-bar "$src" -o "$part" ;;
    /*)
      [ -f "$src" ] || return 1
      cp "$src" "$part" ;;
    *) die "Unsupported database source '$src' (use https://, s3://, file:// or an absolute directory)." ;;
  esac || { rm -f "$part"; return 1; }
  mv "$part" "$dest"
}

download_gh_asset() {
  local asset_name="$1" target_dir="$2"
  mkdir -p "$target_dir"

  # 1. Try via gh CLI if installed and authenticated
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    echo "Fetching $asset_name via GitHub CLI (release $SONGWRITER_RELEASE_TAG)..."
    if gh release download "$SONGWRITER_RELEASE_TAG" --repo "${GH_REPO_SLUG:-rayone/songwriter}" --pattern "$asset_name" --dir "$target_dir" --clobber >/dev/null 2>&1; then
      return 0
    fi
  fi

  # 2. Try authenticated curl via GITHUB_TOKEN or saved gh token
  local token="${GITHUB_TOKEN:-}"
  if [ -z "$token" ] && command -v gh >/dev/null 2>&1; then
    token="$(gh auth token 2>/dev/null || true)"
  fi

  local dest_tar="$target_dir/$asset_name"
  if [ -n "$token" ] && [ -n "$GH_REPO_SLUG" ]; then
    echo "Fetching $asset_name via GitHub API with auth token..."
    local rel_json asset_url
    rel_json="$(curl -sf -H "Authorization: token $token" "https://api.github.com/repos/$GH_REPO_SLUG/releases/tags/$SONGWRITER_RELEASE_TAG" 2>/dev/null || true)"
    if [ -n "$rel_json" ]; then
      asset_url="$(python3 -c "import sys, json; data=json.loads(sys.stdin.read()); print(next((a['url'] for a in data.get('assets', []) if a['name']=='$asset_name'), ''))" <<< "$rel_json" 2>/dev/null || true)"
      if [ -n "$asset_url" ]; then
        if curl -fL --retry 3 --progress-bar -H "Authorization: token $token" -H "Accept: application/octet-stream" "$asset_url" -o "$dest_tar.part"; then
          mv "$dest_tar.part" "$dest_tar"
          return 0
        fi
      fi
    fi
  fi

  # 3. Direct unauthenticated release asset download (for public releases)
  local pub_url="https://github.com/${GH_REPO_SLUG:-rayone/songwriter}/releases/download/$SONGWRITER_RELEASE_TAG/$asset_name"
  echo "Fetching $asset_name from $pub_url..."
  if curl -fL --retry 3 --progress-bar "$pub_url" -o "$dest_tar.part"; then
    mv "$dest_tar.part" "$dest_tar"
    return 0
  fi

  return 1
}

fetch_and_extract_tar() {
  local dest_file="$1" asset_name="$2" target_dir="$3"
  if [ -s "$dest_file" ]; then
    echo "Present, skipping: $dest_file"
    return 0
  fi
  download_gh_asset "$asset_name" "$target_dir" || return 1
  local downloaded_tar="$target_dir/$asset_name"
  if [ -f "$downloaded_tar" ]; then
    echo "Extracting $asset_name into $target_dir..."
    tar -xzf "$downloaded_tar" -C "$target_dir"
    rm -f "$downloaded_tar"
  fi
}

if [ -n "$SONGWRITER_DB_URL" ]; then
  BASE="${SONGWRITER_DB_URL%/}"
  download "$BASE/app.duckdb" "$APP_DB" || true
  download "$BASE/songs.parquet" "$SONGS_PQ" || true
else
  # Pull automatically from GitHub release assets
  echo "Checking database release assets ($SONGWRITER_RELEASE_TAG)..."
  fetch_and_extract_tar "$APP_DB" "app.duckdb.tar.gz" "$SONGWRITER_DATA_DIR" || echo "Note: app.duckdb could not be fetched automatically."
  fetch_and_extract_tar "$SONGS_PQ" "songs.parquet.tar.gz" "$SONGWRITER_DATA_DIR" || echo "Note: songs.parquet could not be fetched automatically."
fi

DATA_READY=1
[ -s "$APP_DB" ] && [ -s "$SONGS_PQ" ] || DATA_READY=0
if [ "$DATA_READY" -eq 1 ]; then
  echo "Initializing workbench database schema..."
  SONGWRITER_DATA_DIR="$SONGWRITER_DATA_DIR" "$PYTHON" -c "from songwriter.ai.song_store import ensure_schema; ensure_schema()" || die "Could not initialize workbench database schema."
fi
# ------------------------------------------------------------ service / launch
wait_healthy() {
  local url="http://127.0.0.1:$SONGWRITER_PORT/healthz"
  case "$SONGWRITER_HOST" in 127.*|0.0.0.0|localhost) ;; *) url="http://$SONGWRITER_HOST:$SONGWRITER_PORT/healthz" ;; esac
  for _ in $(seq 1 60); do
    curl -fsS "$url" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

if [ "$INSTALL_SERVICE" -eq 1 ]; then
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "Creating system user $SERVICE_USER"
    useradd --system --home-dir "$SONGWRITER_DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  # Code is root-owned and read-only to the service; only the data dir is writable.
  chown -R "$SERVICE_USER:$SERVICE_USER" "$SONGWRITER_DATA_DIR"
  chown root:"$SERVICE_USER" "$ENV_FILE"
  chmod 640 "$ENV_FILE"
  sed -e "s|@SONGWRITER_HOME@|$SONGWRITER_HOME|g" \
      -e "s|@SONGWRITER_DATA_DIR@|$SONGWRITER_DATA_DIR|g" \
      -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
      "$SONGWRITER_HOME/deploy/songwriter.service" > /etc/systemd/system/songwriter.service
  chmod 644 /etc/systemd/system/songwriter.service
  systemctl daemon-reload
  systemctl enable songwriter >/dev/null
  systemctl restart songwriter
  if [ "$DATA_READY" -eq 1 ]; then
    if wait_healthy; then
      echo "Service 'songwriter' is running on $SONGWRITER_HOST:$SONGWRITER_PORT."
    else
      die "Service started but /healthz did not answer. Check: journalctl -u songwriter -n 50"
    fi
  fi
else
  echo
  echo "Install complete. Launch with:"
  echo "  cd $SONGWRITER_HOME && set -a && . deploy/.env && set +a && .venv/bin/python -m songwriter.web"
fi

if [ "$DATA_READY" -eq 0 ]; then
  echo
  echo "Runtime databases are missing. Copy them into place, then (re)start the server:"
  echo "  $APP_DB"
  echo "  $WB_DB"
  if [ "$INSTALL_SERVICE" -eq 1 ]; then
    echo "  chown -R $SERVICE_USER:$SERVICE_USER $SONGWRITER_DATA_DIR && systemctl restart songwriter"
  fi
fi
if [ -n "$SONGWRITER_AUTH_USER" ]; then
  echo
  echo "Login: user '$SONGWRITER_AUTH_USER'; password stored in $ENV_FILE (SONGWRITER_AUTH_PASSWORD)."
  if [ "$GENERATED_PASSWORD" -eq 1 ]; then
    echo "A password was generated for you; read it with: sudo grep AUTH_PASSWORD $ENV_FILE"
  fi
  if [ "$LOOPBACK" -eq 0 ]; then
    echo "Basic auth sends the password on every request: put the server behind HTTPS (see deploy/README.md)."
  fi
fi
exit 0
