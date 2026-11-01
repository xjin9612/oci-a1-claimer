#!/usr/bin/env bash
# oci-a1-claimer 安裝腳本（Linux / macOS / WSL）
#
#   ./setup.sh                         本機安裝：建立 .venv、檢查 secrets/ 與 config.env、執行 dry-run
#   ./setup.sh --telegram              設定 Telegram 通知：驗證 token、取得 chat id、發測試訊息、寫入 config.env
#   sudo ./setup.sh --systemd          安裝成 systemd 服務（專用使用者、/opt、/etc、/var/lib）
#   sudo ./setup.sh --systemd --update-config
#                                      以專案內的 config.env 與 secrets/ 覆蓋已安裝的版本
#   sudo ./setup.sh --uninstall        移除服務與程式，保留 /etc 設定與 /var/lib 結果
#   sudo ./setup.sh --uninstall --purge
#                                      連同設定、金鑰、結果與服務使用者一併移除
#
# 使用前把以下三個檔案放進 secrets/：
#   oci_config        OCI 設定檔（主控台新增 API key 時產生的設定片段）
#   oci_api_key.pem   API 私鑰
#   ssh.pub           要注入實例的 SSH 公鑰
set -euo pipefail

readonly APP=oci-a1-claimer
readonly SVC_USER=oci-claimer
readonly OPT_DIR=/opt/$APP
readonly ETC_DIR=/etc/$APP
readonly VAR_DIR=/var/lib/$APP
readonly UNIT_PATH=/etc/systemd/system/$APP.service
readonly SECRET_FILES=(oci_config oci_api_key.pem ssh.pub)

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR

info()  { printf '\033[32m[資訊]\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m[警告]\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[31m[錯誤]\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# 讀取 KEY=VALUE 檔中最後一個 KEY 的值（去除前後引號）
env_value() {
    local key=$1 file=$2
    sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "$file" | tail -n 1 | sed "s/^[\"']//; s/[\"']$//"
}

find_python() {
    local py
    for py in python3 python; do
        if command -v "$py" >/dev/null 2>&1 &&
            "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            command -v "$py"
            return 0
        fi
    done
    die "需要 Python 3.10 以上（Debian / Ubuntu：sudo apt install python3 python3-venv）"
}

make_venv() {
    local py=$1 dir=$2
    if [[ ! -x "$dir/bin/python" ]]; then
        "$py" -m venv "$dir" || die "建立虛擬環境失敗（Debian / Ubuntu 請先安裝：sudo apt install python3-venv）"
    fi
    "$dir/bin/python" -m pip install --quiet --disable-pip-version-check -r "$PROJECT_DIR/requirements.txt"
}

# 把 OCI 設定檔中指定 profile 的 key_file 設為指定路徑（不存在則新增）
set_key_file() {
    local py=$1 cfg=$2 profile=$3 key=$4
    "$py" - "$cfg" "$profile" "$key" <<'PY'
import sys
from pathlib import Path

cfg, profile, key = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = cfg.read_text(encoding="utf-8").splitlines()
out, section, found, seen = [], None, False, False
for line in lines:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        if section == profile and not found:
            out.append(f"key_file={key}")
            found = True
        section = stripped[1:-1]
        seen = seen or section == profile
    elif section == profile and stripped.split("=", 1)[0].strip() == "key_file":
        line, found = f"key_file={key}", True
    out.append(line)
if section == profile and not found:
    out.append(f"key_file={key}")
    found = True
if not seen:
    sys.exit(f"OCI 設定檔中找不到 profile [{profile}]")
cfg.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

check_project_files() {
    local missing=() f
    for f in "${SECRET_FILES[@]}"; do
        [[ -f "$PROJECT_DIR/secrets/$f" ]] || missing+=("secrets/$f")
    done
    if ((${#missing[@]})); then
        die "缺少檔案：${missing[*]}（說明見 README「快速開始」）"
    fi
    grep -Eq '^[[:space:]]*[A-Z_]+[[:space:]]*=.*replace-me' "$PROJECT_DIR/config.env" &&
        die "config.env 仍有 replace-me 佔位值，請先填入 SUBNET_ID 等設定"
    grep -q '^-----BEGIN' "$PROJECT_DIR/secrets/ssh.pub" &&
        die "secrets/ssh.pub 看起來是私鑰，請改放 .pub 公鑰"
    return 0
}

# ---------------------------------------------------------------------------
# 本機安裝
# ---------------------------------------------------------------------------
install_local() {
    cd "$PROJECT_DIR"
    if grep -qi microsoft /proc/version 2>/dev/null && [[ "$PROJECT_DIR" == /mnt/[a-z]/* ]]; then
        warn "專案位於 Windows 磁碟（$PROJECT_DIR），chmod 在這裡無效，金鑰權限無法收緊。"
        warn "建議把專案 clone 到 WSL 內（例如 ~/$APP）再執行。"
    fi

    local py
    py=$(find_python)
    info "Python：$("$py" --version 2>&1)"
    make_venv "$py" .venv
    info "相依套件已安裝到 .venv"

    umask 077
    mkdir -p secrets
    chmod 700 secrets
    if [[ ! -f config.env ]]; then
        cp config.example.env config.env
        info "已建立 config.env，請填入 SUBNET_ID 與規格後重新執行"
    fi
    chmod 600 config.env
    find secrets -type f -exec chmod 600 {} +
    check_project_files

    local profile
    profile=$(env_value OCI_PROFILE config.env)
    set_key_file .venv/bin/python secrets/oci_config "${profile:-DEFAULT}" "$PROJECT_DIR/secrets/oci_api_key.pem"
    info "secrets/oci_config 的 key_file 已指向 secrets/oci_api_key.pem"

    info "執行 dry-run…"
    .venv/bin/python claimer.py --dry-run
    info "設定檢查通過。開始申請：.venv/bin/python claimer.py"
    info "長期執行：sudo ./setup.sh --systemd"
}

# ---------------------------------------------------------------------------
# Telegram 通知設定
# ---------------------------------------------------------------------------
setup_telegram() {
    cd "$PROJECT_DIR"
    [[ -f config.env ]] || die "找不到 config.env，請先執行 ./setup.sh"
    local py token script
    py=$(find_python)

    echo "1. 在 Telegram 搜尋 @BotFather（帳號名稱完全相符、有認證勾勾），傳 /newbot 建立 Bot。"
    read -rsp "2. 貼上 BotFather 給的 token 後按 Enter（輸入不會顯示）：" token
    echo
    [[ -n "$token" ]] || die "沒有輸入 token"

    # token 以環境變數傳給 Python，不出現在指令列參數與 shell 歷史中
    script=$(cat <<'PY'
import json, os, re, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path

token = os.environ["TG_TOKEN"].strip()
config = Path(sys.argv[1])

def api(method, **params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode() if params else None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            return json.load(r)["result"]
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode()).get("description", "")
        except Exception:
            desc = ""
        sys.exit(f"[錯誤] Telegram API {method} 失敗：HTTP {e.code} {desc}")
    except urllib.error.URLError as e:
        sys.exit(f"[錯誤] 無法連線到 Telegram：{e.reason}")

me = api("getMe")
print(f"[資訊] token 有效，Bot：@{me['username']}")
with open("/dev/tty") as tty:
    print(f"3. 在 Telegram 打開 @{me['username']}，按「開始」並傳一則訊息，完成後回到這裡按 Enter", end="", flush=True)
    tty.readline()

chats = [u["message"]["chat"] for u in api("getUpdates") if "message" in u]
if not chats:
    sys.exit("[錯誤] 沒有收到任何訊息：請確認已對 Bot 傳訊息，再重新執行 ./setup.sh --telegram")
chat = chats[-1]
name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
print(f"[資訊] chat id：{chat['id']}（{name}）")

api("sendMessage", chat_id=chat["id"], text="oci-a1-claimer：Telegram 通知設定完成")
print("[資訊] 已發送測試訊息，請確認 Telegram 有收到")

text = config.read_text(encoding="utf-8")
for key, value in (("TG_BOT_TOKEN", token), ("TG_CHAT_ID", str(chat["id"]))):
    pattern = re.compile(rf"^[ \t]*#?[ \t]*{key}[ \t]*=.*$", re.M)
    line = f"{key}={value}"
    text, n = pattern.subn(lambda _m: line, text, count=1)
    if not n:
        text = text.rstrip("\n") + f"\n{line}\n"
config.write_text(text, encoding="utf-8")
print("[資訊] 已寫入 config.env 的 TG_BOT_TOKEN 與 TG_CHAT_ID")
PY
)
    TG_TOKEN="$token" "$py" -c "$script" "$PROJECT_DIR/config.env"
    chmod 600 config.env
    info "重新啟動 claimer 後生效（前景執行：Ctrl+C 後重跑 .venv/bin/python claimer.py）"
    if [[ -f "$ETC_DIR/config.env" ]]; then
        info "已安裝 systemd 服務：執行 sudo ./setup.sh --systemd --update-config 套用到服務"
    fi
}

# ---------------------------------------------------------------------------
# systemd 安裝 / 移除
# ---------------------------------------------------------------------------
require_root_systemd() {
    [[ $EUID -eq 0 ]] || die "此模式需要 root，請用 sudo 執行"
    [[ -d /run/systemd/system ]] ||
        die "系統未以 systemd 執行（WSL 需在 /etc/wsl.conf 設定 [boot] systemd=true 後重啟 WSL）"
}

# 比對兩個檔案；key_file 行會被本腳本改寫成各自的路徑，不列入比較
same_content() {
    cmp -s <(grep -v '^[[:space:]]*key_file[[:space:]]*=' "$1") <(grep -v '^[[:space:]]*key_file[[:space:]]*=' "$2")
}

# 安裝單一設定檔：不存在才安裝；已存在且內容不同時保留舊檔並提示
install_conf() {
    local src=$1 dst=$2 update=$3
    if [[ -f "$dst" && $update -eq 0 ]]; then
        same_content "$src" "$dst" || warn "$dst 已存在且與專案內容不同，保留原檔（要覆蓋請加 --update-config）"
        return 0
    fi
    install -o root -g "$SVC_USER" -m 640 "$src" "$dst"
}

install_systemd() {
    local update=$1
    require_root_systemd
    cd "$PROJECT_DIR"
    [[ -f config.env ]] || die "找不到 config.env，請先執行 ./setup.sh 完成本機設定"
    check_project_files

    local cfg_path key_path
    cfg_path=$(env_value OCI_CONFIG_FILE config.env)
    key_path=$(env_value SSH_PUBLIC_KEY_FILE config.env)
    [[ "$cfg_path" == secrets/oci_config && "$key_path" == secrets/ssh.pub ]] ||
        die "systemd 模式要求 config.env 使用預設路徑 OCI_CONFIG_FILE=secrets/oci_config、SSH_PUBLIC_KEY_FILE=secrets/ssh.pub"

    if ! id "$SVC_USER" >/dev/null 2>&1; then
        useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin "$SVC_USER"
        info "已建立服務使用者 $SVC_USER"
    fi

    local py
    py=$(find_python)
    install -d -o root -g root -m 755 "$OPT_DIR"
    install -o root -g root -m 644 claimer.py requirements.txt "$OPT_DIR/"
    make_venv "$py" "$OPT_DIR/.venv"
    info "程式已安裝到 $OPT_DIR"

    install -d -o root -g "$SVC_USER" -m 750 "$ETC_DIR" "$ETC_DIR/secrets"
    install_conf config.env "$ETC_DIR/config.env" "$update"
    local f
    for f in "${SECRET_FILES[@]}"; do
        install_conf "secrets/$f" "$ETC_DIR/secrets/$f" "$update"
    done
    local profile
    profile=$(env_value OCI_PROFILE "$ETC_DIR/config.env")
    set_key_file "$OPT_DIR/.venv/bin/python" "$ETC_DIR/secrets/oci_config" "${profile:-DEFAULT}" \
        "$ETC_DIR/secrets/oci_api_key.pem"
    info "設定與金鑰已安裝到 $ETC_DIR（root:$SVC_USER，640）"

    info "以服務使用者執行 dry-run…"
    runuser -u "$SVC_USER" -- env CLAIMER_CONFIG="$ETC_DIR/config.env" STATE_DIR="$VAR_DIR" \
        "$OPT_DIR/.venv/bin/python" "$OPT_DIR/claimer.py" --dry-run

    install -o root -g root -m 644 deploy/$APP.service "$UNIT_PATH"
    systemctl daemon-reload
    systemctl enable "$APP" >/dev/null
    systemctl restart "$APP"
    info "服務已啟動。查看 log：journalctl -u $APP -f"
    info "結果檔：$VAR_DIR/result.json"
}

uninstall_systemd() {
    local purge=$1
    require_root_systemd
    if [[ -f "$UNIT_PATH" ]]; then
        systemctl disable --now "$APP" >/dev/null 2>&1 || true
        rm -f "$UNIT_PATH"
        systemctl daemon-reload
        info "已移除服務"
    fi
    rm -rf "$OPT_DIR"
    info "已移除 $OPT_DIR"
    if ((purge)); then
        rm -rf "$ETC_DIR" "$VAR_DIR"
        if id "$SVC_USER" >/dev/null 2>&1; then
            userdel "$SVC_USER"
        fi
        info "已移除 $ETC_DIR、$VAR_DIR 與服務使用者"
    else
        info "保留 $ETC_DIR（設定與金鑰）與 $VAR_DIR（結果）；完全移除請加 --purge"
    fi
}

main() {
    local mode=local update=0 purge=0
    while (($#)); do
        case $1 in
            --systemd) mode=systemd ;;
            --telegram) mode=telegram ;;
            --uninstall) mode=uninstall ;;
            --update-config) update=1 ;;
            --purge) purge=1 ;;
            -h | --help) usage 0 ;;
            *) warn "未知參數：$1"; usage 1 ;;
        esac
        shift
    done
    case $mode in
        local) install_local ;;
        telegram) setup_telegram ;;
        systemd) install_systemd "$update" ;;
        uninstall) uninstall_systemd "$purge" ;;
    esac
}

main "$@"
