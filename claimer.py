#!/usr/bin/env python3
"""oci-a1-claimer：持續申請 OCI Always Free 的 VM.Standard.A1.Flex（ARM）實例，成功後寫出結果並退出。

STATE_DIR 內有 state.json 時只補做建立後步驟，絕不重新申請。
EXIT_* 須與 deploy/oci-a1-claimer.service 的 RestartPreventExitStatus 一致。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import math
import os
import random
import re
import secrets
import signal
import string
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

try:
    import oci
    from oci.exceptions import ClientError, ServiceError
except ImportError:  # pragma: no cover - 只在未安裝時觸發
    sys.exit("找不到 oci 套件，請先執行：pip install -r requirements.txt")


SHAPE = "VM.Standard.A1.Flex"

EXIT_OK = 0            # 成功（或 dry-run 通過、或之前已完成）
EXIT_UNEXPECTED = 1    # 未預期例外；systemd 會重啟
EXIT_CONFIG = 2        # 設定 / 權限錯誤；重啟也沒用
EXIT_QUOTA = 3         # 免費額度已滿或配額不足；重啟也沒用
EXIT_POST_LAUNCH = 4   # 實例已建立但後續步驟失敗，或狀態不明需人工確認；不可自動重啟
EXIT_INTERRUPTED = 130  # 收到 SIGINT / SIGTERM

STATE_FILE = "state.json"
RESULT_FILE = "result.json"
PENDING_FILE = "pending_launch.json"
LOCK_FILE = "claimer.lock"
# opc-retry-token 有效 24 小時，保守扣 1 小時
PENDING_TOKEN_TTL_SECONDS = 23 * 3600
DRY_RUN_TIMEOUT_SECONDS = 120

# 網路層暫時性錯誤。SDK 把 requests 的例外包成 oci.exceptions.ConnectTimeout /
# RequestException，兩者沒有共同的 oci 父類別，因此分別列出。
NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    oci.exceptions.RequestException,
    oci.exceptions.ConnectTimeout,
    ConnectionError,
    TimeoutError,
)

log = logging.getLogger("oci-a1-claimer")


class ConfigError(Exception):
    """設定或權限錯誤，重試無意義。"""


class QuotaExhausted(Exception):
    """免費額度 / 配額不足，重試無意義。"""


class PostLaunchError(Exception):
    """實例已建立，但後續步驟（等待、取 IP、寫檔）失敗。"""

    def __init__(self, instance_id: str, reason: str):
        super().__init__(reason)
        self.instance_id = instance_id


class UncertainLaunchError(Exception):
    """無法確定先前的申請是否已建立實例，需要人工確認。"""


class Interrupted(Exception):
    """收到停止訊號。"""


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def parse_env_file(path: Path) -> dict[str, str]:
    """接受整行 # 註解、export 前綴與成對引號；不支援行尾註解（# 之後併入值）。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"無法讀取設定檔 {path}：{type(exc).__name__}") from None
    values: dict[str, str] = {}
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise ConfigError(f"{path}:{lineno} 格式錯誤，應為 KEY=VALUE")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


@dataclass
class Settings:
    # 必填：規格與免費額度上限刻意不設預設值，政策變動時只需改設定
    subnet_id: str
    ssh_public_key_file: str
    ocpus: float
    memory_gb: float
    free_ocpu_limit: float
    free_memory_gb_limit: float
    region: str = ""                    # 空字串 = 使用 OCI 設定檔 profile 的 region
    require_home_region: bool = True
    compartment_id: str = ""            # 空字串 = 使用 tenancy（root compartment）
    image_id: str = ""
    image_os: str = "Canonical Ubuntu"
    image_os_version: str = ""
    image_exclude_minimal: bool = True
    image_prefer_lts: bool = True
    display_name: str = "a1-claimer"
    boot_volume_gb: int = 50
    assign_public_ip: bool = True
    availability_domains: list[str] = field(default_factory=list)
    interval_seconds: float = 120.0
    jitter_seconds: float = 30.0
    per_ad_delay_seconds: float = 5.0
    rate_limit_sleep_seconds: float = 300.0
    rate_limit_max_sleep_seconds: float = 1800.0
    transient_retry_base_seconds: float = 10.0
    transient_retry_max_seconds: float = 300.0
    transient_max_attempts_per_ad: int = 3
    usage_extra_compartment_ids: list[str] = field(default_factory=list)
    running_timeout_seconds: float = 900.0
    public_ip_timeout_seconds: float = 300.0
    state_dir: str = "."
    oci_config_file: str = "~/.oci/config"
    oci_profile: str = "DEFAULT"
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    log_level: str = "INFO"
    heartbeat_hours: float = 24.0       # 0 = 不發送定期回報


# (環境變數 / 設定鍵, dataclass 欄位, 型別)
_SETTING_SPECS: list[tuple[str, str, str]] = [
    ("SUBNET_ID", "subnet_id", "str"),
    ("SSH_PUBLIC_KEY_FILE", "ssh_public_key_file", "str"),
    ("REGION", "region", "str"),
    ("REQUIRE_HOME_REGION", "require_home_region", "bool"),
    ("COMPARTMENT_ID", "compartment_id", "str"),
    ("IMAGE_ID", "image_id", "str"),
    ("IMAGE_OS", "image_os", "str"),
    ("IMAGE_OS_VERSION", "image_os_version", "str"),
    ("IMAGE_EXCLUDE_MINIMAL", "image_exclude_minimal", "bool"),
    ("IMAGE_PREFER_LTS", "image_prefer_lts", "bool"),
    ("DISPLAY_NAME", "display_name", "str"),
    ("OCPUS", "ocpus", "float"),
    ("MEMORY_GB", "memory_gb", "float"),
    ("BOOT_VOLUME_GB", "boot_volume_gb", "int"),
    ("ASSIGN_PUBLIC_IP", "assign_public_ip", "bool"),
    ("AVAILABILITY_DOMAINS", "availability_domains", "list"),
    ("INTERVAL_SECONDS", "interval_seconds", "float"),
    ("JITTER_SECONDS", "jitter_seconds", "float"),
    ("PER_AD_DELAY_SECONDS", "per_ad_delay_seconds", "float"),
    ("RATE_LIMIT_SLEEP_SECONDS", "rate_limit_sleep_seconds", "float"),
    ("RATE_LIMIT_MAX_SLEEP_SECONDS", "rate_limit_max_sleep_seconds", "float"),
    ("TRANSIENT_RETRY_BASE_SECONDS", "transient_retry_base_seconds", "float"),
    ("TRANSIENT_RETRY_MAX_SECONDS", "transient_retry_max_seconds", "float"),
    ("TRANSIENT_MAX_ATTEMPTS_PER_AD", "transient_max_attempts_per_ad", "int"),
    ("FREE_OCPU_LIMIT", "free_ocpu_limit", "float"),
    ("FREE_MEMORY_GB_LIMIT", "free_memory_gb_limit", "float"),
    ("USAGE_EXTRA_COMPARTMENT_IDS", "usage_extra_compartment_ids", "list"),
    ("RUNNING_TIMEOUT_SECONDS", "running_timeout_seconds", "float"),
    ("PUBLIC_IP_TIMEOUT_SECONDS", "public_ip_timeout_seconds", "float"),
    ("STATE_DIR", "state_dir", "str"),
    ("OCI_CONFIG_FILE", "oci_config_file", "str"),
    ("OCI_PROFILE", "oci_profile", "str"),
    ("TG_BOT_TOKEN", "tg_bot_token", "str"),
    ("TG_CHAT_ID", "tg_chat_id", "str"),
    ("LOG_LEVEL", "log_level", "str"),
    ("HEARTBEAT_HOURS", "heartbeat_hours", "float"),
]

_REQUIRED = ("SUBNET_ID", "SSH_PUBLIC_KEY_FILE", "OCPUS", "MEMORY_GB", "FREE_OCPU_LIMIT", "FREE_MEMORY_GB_LIMIT")


def _convert(key: str, raw: str, kind: str):
    try:
        if kind == "str":
            return raw
        if kind == "int":
            return int(raw)
        if kind == "float":
            return float(raw)
        if kind == "bool":
            low = raw.lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ValueError(raw)
        if kind == "list":
            return [item.strip() for item in raw.split(",") if item.strip()]
    except ValueError:
        raise ConfigError(f"設定 {key} 的值無效：{raw!r}（預期型別 {kind}）") from None
    raise AssertionError(kind)


# 相對路徑以設定檔所在目錄為基準（無設定檔時以目前目錄），避免因啟動目錄不同而找不到 state.json
_PATH_ATTRS = ("ssh_public_key_file", "state_dir", "oci_config_file")
_POSITIVE_ATTRS = ("ocpus", "memory_gb", "free_ocpu_limit", "free_memory_gb_limit")


def load_settings(config_path: Path | None, environ: dict[str, str] | None = None) -> Settings:
    """環境變數只要有設定就優先，即使是空字串；空字串代表使用預設值，
    因此可用 TG_BOT_TOKEN= 這種寫法關閉設定檔中的值。
    """
    environ = os.environ if environ is None else environ
    file_values: dict[str, str] = {}
    base_dir = Path.cwd()
    if config_path is not None:
        if config_path.exists():
            file_values = parse_env_file(config_path)
            base_dir = config_path.resolve().parent
        else:
            log.warning("找不到設定檔 %s，只使用環境變數", config_path)

    merged: dict[str, object] = {}
    for key, attr, kind in _SETTING_SPECS:
        raw = environ[key] if key in environ else file_values.get(key)
        if raw is None or raw == "":
            continue
        merged[attr] = _convert(key, raw, kind)

    missing = [k for k in _REQUIRED if _attr_of(k) not in merged]
    if missing:
        raise ConfigError("缺少必要設定：" + ", ".join(missing))

    settings = Settings(**merged)  # type: ignore[arg-type]

    for attr in _PATH_ATTRS:
        p = Path(getattr(settings, attr)).expanduser()
        if not p.is_absolute():
            p = base_dir / p
        setattr(settings, attr, str(p))

    for key, attr, kind in _SETTING_SPECS:
        if kind not in ("int", "float"):
            continue
        value = getattr(settings, attr)
        if not math.isfinite(value) or value < 0:
            raise ConfigError(f"{key} 必須是有限的非負數，目前為 {value}")
        if attr in _POSITIVE_ATTRS and value <= 0:
            raise ConfigError(f"{key} 必須大於 0")
    if settings.boot_volume_gb < 50:
        raise ConfigError("BOOT_VOLUME_GB 不可小於 50（OCI 開機磁碟最小值）")
    if settings.transient_max_attempts_per_ad < 1:
        raise ConfigError("TRANSIENT_MAX_ATTEMPTS_PER_AD 至少為 1")
    return settings


def _attr_of(key: str) -> str:
    for k, attr, _ in _SETTING_SPECS:
        if k == key:
            return attr
    raise KeyError(key)


def check_file_permissions(path: Path, label: str) -> bool:
    """group 可寫或 other 有任何權限時警告並回傳 False；無法 stat 時不檢查、回傳 True。

    允許 0640，以支援「root 擁有、服務群組唯讀」的部署方式。
    """
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return True  # 不存在或無法 stat，交給後續實際讀取時報錯
    if mode & 0o027:
        log.warning("%s（%s）權限為 %o，過寬；建議 chmod 600（或 640 且群組僅限服務使用者）",
                    label, path, mode)
        return False
    return True


def write_json_atomic(path: Path, data: dict) -> None:
    """原子替換寫入 JSON，權限 0600。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    # 確保目錄項目也落盤；失敗不影響結果正確性
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


class StateFileError(Exception):
    """state.json 損毀或內容不完整，需要人工確認。"""


def load_state(path: Path) -> dict | None:
    """不存在回傳 None；損毀或缺 instance_id 時拋 StateFileError，呼叫端不得當成沒有 state。"""
    try:
        state = read_json(path)
    except (OSError, ValueError) as exc:
        raise StateFileError(f"無法讀取 {path}：{type(exc).__name__}: {exc}") from None
    if state is None:
        return None
    if not isinstance(state, dict) or not state.get("instance_id"):
        raise StateFileError(f"{path} 內容不完整（缺少 instance_id）")
    return state


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RedactingFormatter(logging.Formatter):
    """在最終輸出字串（含例外 traceback）上遮蔽已知敏感值，例如 Telegram token。

    程式本身不會把憑證寫進 log；這裡是避免第三方例外訊息意外夾帶時的最後防線。
    """

    def __init__(self, fmt: str, secrets_: list[str] | tuple[str, ...] = ()):
        super().__init__(fmt)
        self.secrets = [s for s in secrets_ if s]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for s in self.secrets:
            text = text.replace(s, "***")
        return text


def setup_logging(level: str, secrets_: list[str] | tuple[str, ...] = ()) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s", secrets_))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # oci SDK 在 DEBUG 等級會輸出請求細節，固定壓在 WARNING
    logging.getLogger("oci").setLevel(logging.WARNING)


class TelegramNotifier:
    """token 或 chat_id 未設定時不發送；send 失敗只記錄、回傳 False，不拋例外。"""

    def __init__(self, token: str, chat_id: str, timeout: float = 10.0):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
            log.info("Telegram 通知已送出")
            return True
        except urllib.error.HTTPError as exc:
            # HTTPError 的字串表示含完整 URL（內有 token），只記錄狀態碼與 API 回傳的說明
            log.warning("Telegram 通知失敗：HTTP %s %s", exc.code, telegram_error_description(exc))
        except Exception as exc:  # noqa: BLE001 - 通知為盡力而為
            log.warning("Telegram 通知失敗：%s", type(exc).__name__)
        return False


def telegram_error_description(exc: urllib.error.HTTPError) -> str:
    """取出 Telegram API 回應中的 description（例如 chat not found），不含 URL。"""
    try:
        return str(json.loads(exc.read().decode("utf-8")).get("description", ""))
    except Exception:  # noqa: BLE001
        return ""


class ErrorKind(Enum):
    CAPACITY = "容量不足"
    RATE_LIMIT = "請求過於頻繁（429）"
    QUOTA = "額度 / 配額不足"
    CONFIG = "設定或權限錯誤"
    TRANSIENT = "暫時性錯誤"


# 409 中可重試者（官方 API Errors 文件與 SDK 的 RETRYABLE_STATUSES_AND_CODES）
_RETRYABLE_409 = {"IncorrectState", "ExternalServerIncorrectState", "LockConflict"}
_QUOTA_CODES = {"LimitExceeded", "QuotaExceeded"}


def classify_service_error(exc: ServiceError) -> ErrorKind:
    """以 status + code 為主、訊息字串為輔判斷錯誤類型。

    依據：OCI API Errors（https://docs.oracle.com/en-us/iaas/Content/API/References/apierrors.htm）。容量不足不在官方錯誤表內，
    社群實測回應為 500 / code=InternalError / message="Out of host capacity."，
    因此 5xx 時才以訊息含 "capacity" 作為輔助判斷。
    """
    status = exc.status
    code = exc.code or ""
    message = (exc.message or "").lower()

    if status == 429:
        return ErrorKind.RATE_LIMIT
    if code in _QUOTA_CODES:
        return ErrorKind.QUOTA
    if isinstance(status, int) and status >= 500:
        if "capacity" in message:
            return ErrorKind.CAPACITY
        return ErrorKind.TRANSIENT
    if status == 409:
        if code in _RETRYABLE_409:
            return ErrorKind.TRANSIENT
        return ErrorKind.CONFIG
    if status == 400 and ("service limit" in message or "quota" in message):
        # code 不符但訊息明顯指向額度問題（字串比對為輔）
        return ErrorKind.QUOTA
    if isinstance(status, int) and 400 <= status < 500:
        return ErrorKind.CONFIG
    return ErrorKind.TRANSIENT


def describe_service_error(exc: ServiceError) -> str:
    return f"{exc.status} {exc.code}: {exc.message} (opc-request-id={exc.request_id})"


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text or ""))


def is_ubuntu_lts(version: str) -> bool:
    """Ubuntu LTS 版號為偶數年 + .04，例如 22.04、24.04。"""
    m = re.match(r"^(\d+)\.04$", (version or "").strip())
    return bool(m) and int(m.group(1)) % 2 == 0


def pick_image(images: list, settings: Settings):
    """從候選映像檔中依設定挑一個；無結果回傳 None。

    排序：(是否 LTS, 版本號, 建立時間) 由大到小。LTS 僅對 Ubuntu 有意義。
    """
    candidates = list(images)
    if settings.image_os_version:
        candidates = [i for i in candidates if i.operating_system_version == settings.image_os_version]
    if settings.image_exclude_minimal:
        candidates = [i for i in candidates if "minimal" not in (i.display_name or "").lower()]
    if not candidates:
        return None
    ubuntu = "ubuntu" in settings.image_os.lower()

    def key(img):
        lts = settings.image_prefer_lts and ubuntu and is_ubuntu_lts(img.operating_system_version)
        created = img.time_created.timestamp() if img.time_created else 0
        return (lts, _version_tuple(img.operating_system_version), created)

    return max(candidates, key=key)


@dataclass
class OciClients:
    compute: object
    network: object
    identity: object
    tenancy_id: str
    region: str


def build_clients(settings: Settings) -> OciClients:
    """明確指定 retry_strategy，避免行為受環境變數 OCI_SDK_DEFAULT_RETRY_ENABLED 影響：
    啟動階段的唯讀呼叫用 SDK 的 DEFAULT_RETRY_STRATEGY；launch_instance 與建立後的輪詢
    在呼叫時改傳 NoneRetryStrategy，由本程式控制退避節奏。

    明確停用斷路器：OCI_SDK_DEFAULT_CIRCUITBREAKER_ENABLED=true 時，連續 5xx 會讓
    之後的呼叫被擋下，而輪詢容量不足正好是連續 500。
    """
    config_path = settings.oci_config_file
    check_file_permissions(Path(config_path), "OCI 設定檔")  # 可能含 pass_phrase
    try:
        cfg = oci.config.from_file(file_location=config_path, profile_name=settings.oci_profile)
        if settings.region:
            cfg["region"] = settings.region
        oci.config.validate_config(cfg)
        key_file = cfg.get("key_file")
        if key_file:
            check_file_permissions(Path(os.path.expanduser(key_file)), "OCI API 私鑰")
        kwargs = {
            "retry_strategy": oci.retry.DEFAULT_RETRY_STRATEGY,
            "circuit_breaker_strategy": oci.circuit_breaker.NoCircuitBreakerStrategy(),
        }
        # 私鑰在建立 client 時才載入，私鑰相關錯誤會在這裡發生
        clients = OciClients(
            compute=oci.core.ComputeClient(cfg, **kwargs),
            network=oci.core.VirtualNetworkClient(cfg, **kwargs),
            identity=oci.identity.IdentityClient(cfg, **kwargs),
            tenancy_id=cfg["tenancy"],
            region=cfg.get("region", ""),
        )
    except (ClientError, OSError, ValueError) as exc:
        raise ConfigError(f"OCI 設定或金鑰有問題（{config_path} / {settings.oci_profile}）："
                          f"{type(exc).__name__}: {exc}") from None
    return clients


def new_retry_token() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(32))


class Claimer:
    def __init__(
        self,
        settings: Settings,
        clients: OciClients,
        notifier: TelegramNotifier | None = None,
        *,
        sleep_fn: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
        token_factory: Callable[[], str] = new_retry_token,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.s = settings
        self.c = clients
        self.notifier = notifier or TelegramNotifier("", "")
        self.stop_event = threading.Event()
        self._sleep_fn = sleep_fn
        self.rng = rng or random.Random()
        self.token_factory = token_factory
        self.clock = clock
        # pending 的 created_at 要跨程序比較，不能用 monotonic
        self.wall_clock = wall_clock
        self._pending_created_at: float | None = None
        self.compartment_id = settings.compartment_id or clients.tenancy_id
        self.state_path = Path(settings.state_dir).expanduser() / STATE_FILE
        self.result_path = Path(settings.state_dir).expanduser() / RESULT_FILE
        self.pending_path = Path(settings.state_dir).expanduser() / PENDING_FILE
        self.lock_path = Path(settings.state_dir).expanduser() / LOCK_FILE
        self._rate_limit_streak = 0
        # call_with_retry 未指定 deadline 時採用的 self.clock() 時間點；None 表示無限重試，dry-run 會設定
        self.default_deadline: float | None = None
        # 申請統計，供定期回報使用
        self.stats = {"attempts": 0, "capacity": 0, "rate_limited": 0, "transient": 0, "network": 0}
        self.rounds = 0
        self.started_at = ""
        self.next_heartbeat: float | None = None

    def sleep(self, seconds: float) -> None:
        """收到停止訊號時拋 Interrupted。"""
        if seconds <= 0:
            return
        if self._sleep_fn is not None:
            self._sleep_fn(seconds)
        elif self.stop_event.wait(seconds):
            raise Interrupted()
        if self.stop_event.is_set():
            raise Interrupted()

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise Interrupted()

    def _transient_delay(self, attempt: int) -> float:
        base = self.s.transient_retry_base_seconds * (2 ** max(0, attempt - 1))
        delay = min(base, self.s.transient_retry_max_seconds)
        return delay + self.rng.uniform(0, delay * 0.2)

    def _rate_limit_delay(self) -> float:
        self._rate_limit_streak += 1
        base = self.s.rate_limit_sleep_seconds * (2 ** (self._rate_limit_streak - 1))
        delay = min(base, self.s.rate_limit_max_sleep_seconds)
        return delay + self.rng.uniform(0, self.s.jitter_seconds)

    def call_with_retry(self, what: str, fn: Callable, *args, deadline: float | None = None, **kwargs):
        """唯讀呼叫：網路錯誤、暫時性錯誤、429 退避重試；設定類錯誤轉 ConfigError、額度類轉 QuotaExhausted。

        deadline 為 self.clock() 的時間點，超過拋 TimeoutError；None 時用 default_deadline，兩者皆 None 則無限重試。
        """
        if deadline is None:
            deadline = self.default_deadline
        if deadline is not None:
            # 有時限時關閉 SDK 內建重試（最長 600 秒），否則時限形同虛設
            kwargs.setdefault("retry_strategy", oci.retry.NoneRetryStrategy())
        attempt = 0
        while True:
            self._check_stop()
            attempt += 1
            try:
                result = fn(*args, **kwargs)
                self._rate_limit_streak = 0
                return result
            except ServiceError as exc:
                kind = classify_service_error(exc)
                if kind is ErrorKind.CONFIG:
                    raise ConfigError(f"{what} 失敗：{describe_service_error(exc)}") from None
                if kind is ErrorKind.QUOTA:
                    raise QuotaExhausted(f"{what} 失敗：{describe_service_error(exc)}") from None
                delay = self._rate_limit_delay() if kind is ErrorKind.RATE_LIMIT else self._transient_delay(attempt)
                reason = f"{kind.value}（{describe_service_error(exc)}）"
            except NETWORK_ERRORS as exc:
                delay = self._transient_delay(attempt)
                reason = f"網路錯誤 {type(exc).__name__}"
            if deadline is not None:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TimeoutError(f"{what}：{reason}，重試 {attempt} 次後超過等待時限")
                delay = min(delay, remaining)
            log.warning("%s：%s，%.0f 秒後重試", what, reason, delay)
            self.sleep(delay)

    def list_all(self, what: str, fn: Callable, **kwargs) -> list:
        """自行處理分頁（不用 oci.pagination，方便以 mock 測試）。"""
        items: list = []
        page = None
        while True:
            if page:
                kwargs["page"] = page
            resp = self.call_with_retry(what, fn, **kwargs)
            items.extend(resp.data)
            if not getattr(resp, "has_next_page", False):
                return items
            page = resp.next_page

    def current_a1_usage(self) -> tuple[float, float, list]:
        """回傳 (OCPU, 記憶體 GB, 實例)；加總 compartment 與 USAGE_EXTRA_COMPARTMENT_IDS 內未終止的 A1 實例，STOPPED 也計入。"""
        ocpus = mem = 0.0
        found = []
        compartments = [self.compartment_id] + [c for c in self.s.usage_extra_compartment_ids if c != self.compartment_id]
        for comp in compartments:
            for inst in self.list_all("列出實例", self.c.compute.list_instances, compartment_id=comp):
                if inst.shape != SHAPE:
                    continue
                if inst.lifecycle_state in ("TERMINATED", "TERMINATING"):
                    continue
                cfg = inst.shape_config
                ocpus += float(getattr(cfg, "ocpus", 0) or 0)
                mem += float(getattr(cfg, "memory_in_gbs", 0) or 0)
                found.append(inst)
        return ocpus, mem, found

    def check_usage(self) -> None:
        used_cpu, used_mem, found = self.current_a1_usage()
        for inst in found:
            log.info("現有實例：%s（%s，%s OCPU / %s GB）", inst.display_name, inst.lifecycle_state,
                     inst.shape_config.ocpus, inst.shape_config.memory_in_gbs)
        log.info("已用 %.1f OCPU / %.1f GB；本次申請 %.1f OCPU / %.1f GB；上限 %.1f OCPU / %.1f GB",
                 used_cpu, used_mem, self.s.ocpus, self.s.memory_gb,
                 self.s.free_ocpu_limit, self.s.free_memory_gb_limit)
        if used_cpu >= self.s.free_ocpu_limit or used_mem >= self.s.free_memory_gb_limit:
            raise QuotaExhausted("免費額度已用滿，不需要再申請")
        if used_cpu + self.s.ocpus > self.s.free_ocpu_limit or used_mem + self.s.memory_gb > self.s.free_memory_gb_limit:
            raise QuotaExhausted(
                f"剩餘額度不足：還可用 {self.s.free_ocpu_limit - used_cpu:.1f} OCPU / "
                f"{self.s.free_memory_gb_limit - used_mem:.1f} GB，請調低 OCPUS / MEMORY_GB")

    def check_home_region(self) -> None:
        """Always Free 運算實例只能建在 home region；在其他 region 建立會是付費資源。"""
        subs = self.call_with_retry("查詢 region 訂閱", self.c.identity.list_region_subscriptions,
                                    self.c.tenancy_id).data
        home = next((r for r in subs if r.is_home_region), None)
        home_name = home.region_name if home else None
        log.info("目標 region：%s；home region：%s", self.c.region, home_name)
        # OCI 設定檔的 region 可能寫完整名稱（ap-tokyo-1）或代碼（NRT）
        target = (self.c.region or "").lower()
        if home is None or target not in {(home.region_name or "").lower(), (home.region_key or "").lower()}:
            msg = f"目標 region {self.c.region} 不是 home region {home_name}，建立的實例不會是 Always Free"
            if self.s.require_home_region:
                raise ConfigError(msg + "（確定要建立請設 REQUIRE_HOME_REGION=false）")
            log.warning(msg)

    def check_subnet(self) -> None:
        subnet = self.call_with_retry("讀取子網", self.c.network.get_subnet, self.s.subnet_id).data
        log.info("子網：%s（%s）", subnet.display_name, subnet.cidr_block)
        if self.s.assign_public_ip and subnet.prohibit_public_ip_on_vnic:
            raise ConfigError("ASSIGN_PUBLIC_IP=true，但此子網是私有子網（禁止公網 IP）")

    def resolve_image(self):
        if self.s.image_id:
            img = self.call_with_retry("讀取映像檔", self.c.compute.get_image, self.s.image_id).data
            log.info("使用指定映像檔：%s", img.display_name)
            return img
        images = self.list_all(
            "列出映像檔", self.c.compute.list_images,
            compartment_id=self.compartment_id, operating_system=self.s.image_os,
            shape=SHAPE, sort_by="TIMECREATED", sort_order="DESC", lifecycle_state="AVAILABLE",
        )
        img = pick_image(images, self.s)
        if img is None:
            raise ConfigError(f"找不到符合條件、可用於 {SHAPE} 的映像檔（IMAGE_OS={self.s.image_os}）")
        log.info("自動選用映像檔：%s", img.display_name)
        return img

    def list_ads(self) -> list[str]:
        ads = [ad.name for ad in self.call_with_retry(
            "列出可用域", self.c.identity.list_availability_domains, self.c.tenancy_id).data]
        if self.s.availability_domains:
            wanted = self.s.availability_domains
            ads = [ad for ad in ads if any(ad == w or ad.endswith(w) for w in wanted)]
            if not ads:
                raise ConfigError(f"AVAILABILITY_DOMAINS={','.join(wanted)} 不符合任何可用域")
        if not ads:
            raise ConfigError("此區域沒有可用域")
        return ads

    def read_ssh_key(self) -> str:
        path = Path(self.s.ssh_public_key_file).expanduser()
        if not path.is_file():
            raise ConfigError(f"找不到 SSH 公鑰：{path}")
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"無法讀取 SSH 公鑰 {path}：{type(exc).__name__}") from None
        if not re.match(r"^(ssh-|ecdsa-|sk-)", text):
            raise ConfigError(f"{path} 看起來不是 SSH 公鑰（是否誤填成私鑰？）")
        return text

    def launch_spec(self, image_id: str) -> dict:
        """申請內容中會隨時間或設定改變的部分；與 token 一起存進 pending，重送時沿用。"""
        return {"image_id": image_id, "ocpus": self.s.ocpus, "memory_gb": self.s.memory_gb,
                "boot_volume_gb": self.s.boot_volume_gb}

    def build_launch_details(self, ad: str, spec: dict, ssh_key: str):
        m = oci.core.models
        return m.LaunchInstanceDetails(
            availability_domain=ad,
            compartment_id=self.compartment_id,
            display_name=self.s.display_name,
            shape=SHAPE,
            shape_config=m.LaunchInstanceShapeConfigDetails(ocpus=spec["ocpus"], memory_in_gbs=spec["memory_gb"]),
            source_details=m.InstanceSourceViaImageDetails(
                image_id=spec["image_id"], boot_volume_size_in_gbs=spec["boot_volume_gb"]),
            create_vnic_details=m.CreateVnicDetails(subnet_id=self.s.subnet_id, assign_public_ip=self.s.assign_public_ip),
            metadata={"ssh_authorized_keys": ssh_key},
        )

    def try_launch(self, ad: str, details, spec: dict, token: str | None = None, uncertain: bool = False):
        """對單一 AD 嘗試建立；成功回傳 Instance，本 AD 放棄回傳 None。

        送出前先把 token 寫入 pending_launch.json，直到拿到明確結果才清除，因此送出後
        任何中斷（網路錯誤、例外、程序被殺）都會在下次啟動以同一 AD、同一 token 重送。
        結果不明期間 429 / 5xx / 可重試 409 只退避，不換 AD、不換 token；token 超過
        PENDING_TOKEN_TTL_SECONDS 時拋 UncertainLaunchError。成功時不清 pending，
        由呼叫端寫入 state.json 後清除。
        """
        if token is None:
            token = self.token_factory()
            self.save_pending(ad, token, spec)
        try:
            result = self._try_launch(ad, details, spec, token, uncertain)
        except (QuotaExhausted, ConfigError):
            # 同一 token 拿到明確失敗：這個 token 沒有建立任何東西
            self.clear_pending()
            raise
        if result is None:
            self.clear_pending()
        return result

    def _check_token_age(self, ad: str) -> None:
        if self._pending_created_at is None:
            return
        if self.wall_clock() - self._pending_created_at > PENDING_TOKEN_TTL_SECONDS:
            raise UncertainLaunchError(
                f"[{ad}] 結果不明的申請持續超過 {PENDING_TOKEN_TTL_SECONDS // 3600} 小時，"
                "retry token 已不保證冪等；請到主控台確認沒有多出的實例後刪除 pending_launch.json")

    def _try_launch(self, ad: str, details, spec: dict, token: str, uncertain: bool):
        transient_failures = 0
        network_failures = 0
        while True:
            self._check_stop()
            if uncertain:
                self._check_token_age(ad)
            self.stats["attempts"] += 1
            try:
                resp = self.c.compute.launch_instance(
                    details, opc_retry_token=token, retry_strategy=oci.retry.NoneRetryStrategy())
                self._rate_limit_streak = 0
                return resp.data
            except ServiceError as exc:
                if code_is_invalid_token(exc):
                    if uncertain:
                        # 此 token 曾造成系統變更後被作廢：先前的請求很可能已建立實例
                        raise UncertainLaunchError(
                            f"[{ad}] 結果不明的申請回傳 InvalidatedRetryToken，可能已建立過實例："
                            f"{describe_service_error(exc)}") from None
                    token = self.token_factory()
                    self.save_pending(ad, token, spec)
                    log.warning("[%s] retry token 已失效，換新 token 重試", ad)
                    continue
                kind = classify_service_error(exc)
                if uncertain and exc.status == 409 and kind is ErrorKind.CONFIG:
                    # 原請求可能仍在處理中，不能當成明確失敗而放棄 token
                    kind = ErrorKind.TRANSIENT
                if kind is ErrorKind.CAPACITY:
                    self._rate_limit_streak = 0
                    self.stats["capacity"] += 1
                    log.info("[%s] 容量不足", ad)
                    return None
                if kind is ErrorKind.QUOTA:
                    raise QuotaExhausted(describe_service_error(exc)) from None
                if kind is ErrorKind.CONFIG:
                    raise ConfigError(f"[{ad}] {describe_service_error(exc)}") from None
                if kind is ErrorKind.RATE_LIMIT:
                    self.stats["rate_limited"] += 1
                    delay = self._rate_limit_delay()
                    if uncertain:
                        log.warning("[%s] 被限流（429），退避 %.0f 秒後以同一 token 重送（先前結果不明）", ad, delay)
                        self.sleep(delay)
                        continue
                    log.warning("[%s] 被限流（429），退避 %.0f 秒", ad, delay)
                    self.sleep(delay)
                    return None
                # TRANSIENT：伺服器有回應但失敗，以同一 token 重試，以防它其實已受理。
                self.stats["transient"] += 1
                transient_failures += 1
                if not uncertain and transient_failures >= self.s.transient_max_attempts_per_ad:
                    log.warning("[%s] 暫時性錯誤 %d 次（%s），換下一個 AD",
                                ad, transient_failures, describe_service_error(exc))
                    return None
                delay = self._transient_delay(transient_failures)
                log.warning("[%s] 暫時性錯誤（%s），%.0f 秒後以同一 token 重試",
                            ad, describe_service_error(exc), delay)
                self.sleep(delay)
            except NETWORK_ERRORS as exc:
                uncertain = True
                self.stats["network"] += 1
                network_failures += 1
                delay = self._transient_delay(network_failures)
                log.warning("[%s] 網路錯誤 %s，%.0f 秒後以同一 token 重送", ad, type(exc).__name__, delay)
                self.sleep(delay)

    def save_pending(self, ad: str, token: str, spec: dict) -> None:
        self._pending_created_at = self.wall_clock()
        write_json_atomic(self.pending_path, {
            "availability_domain": ad, "token": token, "created_at": self._pending_created_at, "spec": spec})

    def clear_pending(self) -> None:
        self._pending_created_at = None
        try:
            self.pending_path.unlink()
        except FileNotFoundError:
            pass

    def load_pending(self) -> dict | None:
        """不存在回傳 None；內容不完整或 token 已超過 PENDING_TOKEN_TTL_SECONDS 時拋 StateFileError。"""
        try:
            pending = read_json(self.pending_path)
        except (OSError, ValueError) as exc:
            raise StateFileError(f"無法讀取 {self.pending_path}：{type(exc).__name__}") from None
        if pending is None:
            return None
        if (not isinstance(pending, dict) or not pending.get("availability_domain") or not pending.get("token")
                or not isinstance(pending.get("spec"), dict)):
            raise StateFileError(f"{self.pending_path} 內容不完整")
        try:
            created_at = float(pending["created_at"])
        except (KeyError, TypeError, ValueError):
            raise StateFileError(f"{self.pending_path} 缺少有效的 created_at") from None
        if self.wall_clock() - created_at > PENDING_TOKEN_TTL_SECONDS:
            raise StateFileError(
                f"{self.pending_path} 已超過 {PENDING_TOKEN_TTL_SECONDS // 3600} 小時，"
                "無法確認先前結果不明的申請是否成功；請到主控台確認沒有多出的實例後刪除此檔")
        self._pending_created_at = created_at
        return pending

    def save_state(self, instance, status: str = "launched") -> None:
        write_json_atomic(self.state_path, {
            "status": status,
            "instance_id": instance.id,
            "display_name": instance.display_name,
            "availability_domain": instance.availability_domain,
            "launched_at": utc_now(),
        })

    def _poll(self, what: str, fn: Callable, *args, deadline: float, **kwargs):
        return self.call_with_retry(what, fn, *args, deadline=deadline, **kwargs).data

    def wait_running(self, instance_id: str):
        deadline = self.clock() + self.s.running_timeout_seconds
        while True:
            try:
                inst = self._poll("查詢實例狀態", self.c.compute.get_instance, instance_id, deadline=deadline)
            except ConfigError as exc:
                # 剛建立時可能因最終一致性短暫回 404，逾時前持續重試
                if self.clock() >= deadline:
                    raise
                log.warning("查詢實例暫時失敗（%s），15 秒後重試", exc)
                self.sleep(15)
                continue
            if inst.lifecycle_state == "RUNNING":
                return inst
            if inst.lifecycle_state in ("TERMINATED", "TERMINATING"):
                raise RuntimeError(f"實例進入 {inst.lifecycle_state} 狀態")
            if self.clock() >= deadline:
                raise RuntimeError(f"等待 RUNNING 逾時（目前 {inst.lifecycle_state}）")
            log.info("實例狀態 %s，等待中…", inst.lifecycle_state)
            self.sleep(15)

    def get_ips(self, inst) -> tuple[str | None, str | None]:
        """回傳 (公網, 私有)；ASSIGN_PUBLIC_IP=false 時公網為 None。"""
        deadline = self.clock() + self.s.public_ip_timeout_seconds
        while True:
            atts = self._poll("列出 VNIC 附加", self.c.compute.list_vnic_attachments, inst.compartment_id,
                              instance_id=inst.id, deadline=deadline)
            for att in atts:
                if att.lifecycle_state != "ATTACHED" or not att.vnic_id:
                    continue
                vnic = self._poll("讀取 VNIC", self.c.network.get_vnic, att.vnic_id, deadline=deadline)
                if not vnic.is_primary:
                    continue
                if vnic.public_ip or not self.s.assign_public_ip:
                    return vnic.public_ip, vnic.private_ip
            if self.clock() >= deadline:
                raise RuntimeError("等待公網 IP 逾時")
            self.sleep(10)

    def finish(self, instance_id: str) -> int:
        """除 Interrupted 外，任何失敗都轉成 PostLaunchError；呼叫端不得回到申請迴圈。"""
        try:
            inst = self.wait_running(instance_id)
            public_ip, private_ip = self.get_ips(inst)
            result = {
                "instance_id": inst.id,
                "display_name": inst.display_name,
                "region": self.c.region,
                "availability_domain": inst.availability_domain,
                "shape": SHAPE,
                "ocpus": inst.shape_config.ocpus,
                "memory_gb": inst.shape_config.memory_in_gbs,
                "public_ip": public_ip,
                "private_ip": private_ip,
                "completed_at": utc_now(),
            }
            write_json_atomic(self.result_path, result)
            state = load_state(self.state_path) or {"instance_id": instance_id}
            state["status"] = "completed"
            write_json_atomic(self.state_path, state)
        except Interrupted:
            raise
        except Exception as exc:  # noqa: BLE001 - 所有失敗都不得導致重新申請
            raise PostLaunchError(instance_id, f"{type(exc).__name__}: {exc}") from exc

        log.info("完成：%s 公網 IP=%s，結果已寫入 %s", inst.display_name, public_ip, self.result_path)
        self.notifier.send(
            f"OCI 實例已建立\n名稱：{inst.display_name}\n區域：{self.c.region}\n"
            f"AD：{inst.availability_domain}\n公網 IP：{public_ip or '（無）'}\nOCID：{inst.id}")
        return EXIT_OK

    def prepare(self, check_usage: bool = True):
        """啟動前檢查；回傳 (ads, image, ssh_key)。dry-run 與正式執行共用。"""
        ssh_key = self.read_ssh_key()
        self.check_home_region()
        ads = self.list_ads()
        log.info("可用域：%s", ", ".join(ads))
        self.check_subnet()
        image = self.resolve_image()
        if check_usage:
            self.check_usage()
        return ads, image, ssh_key

    def _on_launched(self, ad: str, instance) -> int:
        log.info("建立成功！[%s] %s", ad, instance.id)
        # 先落盤再做任何事，確保之後任何失敗都不會重新申請
        try:
            self.save_state(instance)
        except Exception as exc:  # noqa: BLE001
            raise PostLaunchError(instance.id, f"寫入 state 失敗：{exc}") from exc
        self.clear_pending()
        return self.finish(instance.id)

    @contextlib.contextmanager
    def _state_dir_lock(self):
        """同一 STATE_DIR 只允許一個程序申請；拿不到鎖時拋 ConfigError。"""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConfigError(f"{self.lock_path.parent} 正由另一個 oci-a1-claimer 程序使用") from None
            yield
        finally:
            os.close(fd)

    def run(self) -> int:
        with self._state_dir_lock():
            return self._run()

    def _run(self) -> int:
        state = load_state(self.state_path)
        if state:
            if state.get("status") == "completed":
                log.info("%s 顯示已完成（%s），不再申請。如要再申請請先移除此檔。",
                         self.state_path, state.get("instance_id"))
                return EXIT_OK
            log.warning("偵測到先前已建立的實例 %s，進入續做模式（不會重新申請）", state["instance_id"])
            return self.finish(state["instance_id"])

        pending = self.load_pending()
        # 有結果不明的申請時，不做用量檢查：那台若已建立，檢查會誤判額度已滿而無法續做
        ads, image, ssh_key = self.prepare(check_usage=pending is None)
        spec = self.launch_spec(image.id)
        details_by_ad = {ad: self.build_launch_details(ad, spec, ssh_key) for ad in ads}

        if pending:
            ad, pending_spec = pending["availability_domain"], pending["spec"]
            log.warning("[%s] 發現結果不明的申請，以原 token 與原申請內容重送確認", ad)
            details = self.build_launch_details(ad, pending_spec, ssh_key)
            instance = self.try_launch(ad, details, pending_spec, token=pending["token"], uncertain=True)
            if instance is not None:
                return self._on_launched(ad, instance)
            self.check_usage()

        self.start_heartbeat(ads)
        while True:
            self.rounds += 1
            log.info("第 %d 輪，共 %d 個可用域", self.rounds, len(ads))
            for idx, ad in enumerate(ads):
                instance = self.try_launch(ad, details_by_ad[ad], spec)
                if instance is not None:
                    return self._on_launched(ad, instance)
                if idx < len(ads) - 1:
                    self.sleep(self.s.per_ad_delay_seconds)
            self.maybe_heartbeat()
            delay = self.s.interval_seconds + self.rng.uniform(0, self.s.jitter_seconds)
            log.info("本輪無果，%.0f 秒後再試", delay)
            self.sleep(delay)

    def start_heartbeat(self, ads: list[str]) -> None:
        """開始輪詢時通知一次，並排定之後的定期回報；用來發現程式已無聲停止（例如主機睡眠）。"""
        self.started_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")
        if self.s.heartbeat_hours > 0:
            self.next_heartbeat = self.clock() + self.s.heartbeat_hours * 3600
        self.notifier.send(
            f"oci-a1-claimer 開始申請\n規格：{self.s.ocpus:g} OCPU / {self.s.memory_gb:g} GB\n"
            f"區域：{self.c.region}（{len(ads)} 個可用域）\n"
            + (f"每 {self.s.heartbeat_hours:g} 小時回報一次進度" if self.next_heartbeat else "未啟用定期回報"))

    def maybe_heartbeat(self) -> None:
        if self.next_heartbeat is None or self.clock() < self.next_heartbeat:
            return
        st = self.stats
        msg = (f"oci-a1-claimer 仍在執行（自 {self.started_at}）\n"
               f"已嘗試 {st['attempts']} 次、{self.rounds} 輪\n"
               f"容量不足 {st['capacity']}、限流 {st['rate_limited']}、"
               f"暫時錯誤 {st['transient']}、網路錯誤 {st['network']}")
        log.info(msg.replace("\n", "；"))
        self.notifier.send(msg)
        self.next_heartbeat = self.clock() + self.s.heartbeat_hours * 3600

    def dry_run(self) -> int:
        # dry-run 是互動驗證，網路不通時不無限等待
        self.default_deadline = self.clock() + DRY_RUN_TIMEOUT_SECONDS
        state = load_state(self.state_path)
        if state:
            log.warning("[dry-run] 注意：%s 已存在（status=%s），正式執行時不會申請新實例；略過用量檢查",
                        self.state_path, state.get("status"))
        has_pending = self.pending_path.exists()
        if has_pending:
            log.warning("[dry-run] 注意：%s 存在，正式執行時會先以原 token 重送確認；略過用量檢查", self.pending_path)
        ads, image, _ = self.prepare(check_usage=state is None and not has_pending)
        log.info("[dry-run] 將以 %s %.1f OCPU / %.1f GB、開機磁碟 %d GB、公網 IP=%s 申請",
                 SHAPE, self.s.ocpus, self.s.memory_gb, self.s.boot_volume_gb, self.s.assign_public_ip)
        log.info("[dry-run] 映像檔：%s（%s）", image.display_name, image.id)
        log.info("[dry-run] compartment：%s；輪詢 AD：%s", self.compartment_id, ", ".join(ads))
        log.info("[dry-run] 設定檢查通過，未呼叫 launch_instance")
        return EXIT_OK


def code_is_invalid_token(exc: ServiceError) -> bool:
    return exc.status == 409 and exc.code == "InvalidatedRetryToken"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="持續申請 OCI Always Free A1.Flex（ARM）實例")
    parser.add_argument("-c", "--config", default=os.environ.get("CLAIMER_CONFIG", "config.env"),
                        help="設定檔路徑（預設 config.env，可用環境變數 CLAIMER_CONFIG 指定）")
    parser.add_argument("--dry-run", action="store_true", help="只驗證設定、列 AD、解析映像檔後退出")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    config_path = Path(args.config).expanduser()
    notifier = TelegramNotifier("", "")
    claimer: Claimer | None = None

    try:
        settings = load_settings(config_path)
        setup_logging(settings.log_level, [settings.tg_bot_token])
        check_file_permissions(config_path, "設定檔")
        configured = bool(settings.tg_bot_token and settings.tg_chat_id)
        if args.dry_run:
            log.info("Telegram 通知：%s（dry-run 不發送）", "已設定" if configured else "未設定")
        else:
            notifier = TelegramNotifier(settings.tg_bot_token, settings.tg_chat_id)
            log.info("Telegram 通知：%s", "啟用" if configured else "未設定")
        clients = build_clients(settings)
        claimer = Claimer(settings, clients, notifier)

        def _on_signal(_signum, _frame):
            # 訊號處理器內只設旗標；進行中的 API 呼叫會完成，之後在安全點停止
            claimer.stop_event.set()

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

        return claimer.dry_run() if args.dry_run else claimer.run()

    except ConfigError as exc:
        log.error("設定錯誤：%s", exc)
        if claimer is not None and claimer.started_at:
            # 申請開始後才出現的設定錯誤（例如金鑰過期）不會重啟，需要主動通知
            notifier.send(f"oci-a1-claimer 因設定錯誤停止：{exc}")
        return EXIT_CONFIG
    except QuotaExhausted as exc:
        log.error("停止：%s", exc)
        notifier.send(f"oci-a1-claimer 已停止：{exc}")
        return EXIT_QUOTA
    except PostLaunchError as exc:
        log.error("實例 %s 已建立，但後續步驟失敗：%s", exc.instance_id, exc)
        log.error("請到 OCI 主控台確認；重新啟動會進入續做模式，不會重新申請")
        notifier.send(f"OCI 實例已建立（{exc.instance_id}），但後續步驟失敗：{exc}")
        return EXIT_POST_LAUNCH
    except (StateFileError, UncertainLaunchError) as exc:
        log.error("需要人工確認：%s", exc)
        notifier.send(f"oci-a1-claimer 需要人工確認：{exc}")
        return EXIT_POST_LAUNCH
    except TimeoutError as exc:
        log.error("逾時：%s（請確認網路可連到 OCI API）", exc)
        return EXIT_UNEXPECTED
    except Interrupted:
        log.warning("收到停止訊號，已停止")
        return EXIT_INTERRUPTED
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception:  # noqa: BLE001
        log.exception("未預期錯誤")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
