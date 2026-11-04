<p align="center">
  <img src="assets/icon.svg" width="128" height="128" alt="oci-a1-claimer">
</p>

<h1 align="center">oci-a1-claimer</h1>

<p align="center"><a href="README.md">English</a> | 繁體中文</p>

<p align="center"><a href="https://github.com/xjin9612/oci-a1-claimer/actions/workflows/ci.yml"><img src="https://github.com/xjin9612/oci-a1-claimer/actions/workflows/ci.yml/badge.svg" alt="CI"></a></p>

持續透過 OCI 官方 API 申請 Always Free 的 `VM.Standard.A1.Flex`（ARM）實例，直到成功為止。

- 依序嘗試每個可用域（AD）；容量不足就換下一個，整輪結束後休息並加隨機 jitter。
- 429 長退避；額度已滿立即停止；網路錯誤退避重試，不會中斷。
- 成功後等待 RUNNING、取得公網 IP、寫入結果檔、送出通知（選用），然後結束。
- 只依賴官方 [`oci` Python SDK](https://pypi.org/project/oci/)，其餘為標準庫；Python 3.10 以上；程式只有 `claimer.py` 一個檔案，另附 `setup.sh` 一鍵安裝。

## 開始前須知

- **免費額度以官方為準**：見 [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) 與主控台 Governance → Limits, Quotas and Usage。規格（`OCPUS`、`MEMORY_GB`）與額度上限（`FREE_OCPU_LIMIT`、`FREE_MEMORY_GB_LIMIT`）都是 `config.env` 的必填項，程式內沒有預設值，政策變動時只需改設定。
- **Home region**：Always Free 運算實例只能建在 home region，且 home region 註冊後無法更改（[Managing Regions](https://docs.oracle.com/en-us/iaas/Content/Identity/Tasks/managingregions.htm)）。本工具啟動時會檢查目標 region 是否為 home region，不是就拒絕執行（設 `REQUIRE_HOME_REGION=false` 時只警告）。
- **帳號類型**：
  - 免費帳號超過免費額度時，A1 實例會被停用並刪除。
  - PAYG（隨用隨付）帳號超出免費額度的部分**會計費**。請確認 `OCPUS` / `MEMORY_GB` 不超過你帳號的免費額度。
- **一人一帳號**：Oracle 規定每人只能有一個免費帳號，建立多個帳號會被停用或終止（[Free Tier FAQ](https://www.oracle.com/cloud/free/faq/)）。

## 快速開始

**還沒有 Oracle 帳號或還沒準備 API Key**：先照 [docs/oracle-setup.md](docs/oracle-setup.md) 完成註冊、網路、IAM 與 API Key。

適用 Linux、macOS 與 WSL。Windows 請在 WSL 內執行，並把專案放在 WSL 自己的檔案系統（例如 `~/projects/oci-a1-claimer`）：放在 `/mnt/c`、`/mnt/d` 等 Windows 磁碟時，`chmod` 無效，金鑰權限無法收緊。

1. 把三個檔案放進 `secrets/`：

   | 檔案 | 內容 |
   |---|---|
   | `secrets/oci_config` | OCI 設定檔：主控台新增 API key 時產生的設定片段 |
   | `secrets/oci_api_key.pem` | API 私鑰 |
   | `secrets/ssh.pub` | 要注入實例的 SSH 公鑰 |

2. 執行 `./setup.sh`。第一次執行會從範本建立 `config.env` 並提示你填寫。
3. 編輯 `config.env`，填入 `SUBNET_ID` 與規格，再執行一次 `./setup.sh`。
4. 開始申請：

```bash
./setup.sh                    # 建立 .venv、收緊權限、設定 key_file、執行 dry-run
./setup.sh --telegram         # 選用：設定 Telegram 通知
.venv/bin/python claimer.py   # 前景執行，Ctrl+C 可安全停止
sudo ./setup.sh --systemd     # 或安裝成 systemd 服務長期執行
```

前景執行建議放在 `tmux` 裡，關閉終端機也不會中斷：

```bash
tmux new -s a1                # 建立工作階段後執行 .venv/bin/python claimer.py
                              # Ctrl+B 再按 D 離開；tmux attach -t a1 回來查看
```

log 持續出現「容量不足」與「本輪無果」是正常的，代表正在等待容量釋出。WSL 會隨 Windows 睡眠停止，長時間執行請關閉 Windows 的睡眠。

`setup.sh` 本機模式會：
- 檢查 Python 版本，建立 `.venv` 並安裝相依套件。
- 把 `secrets/` 設為 700、裡面的檔案與 `config.env` 設為 600。
- 把 `secrets/oci_config` 的 `key_file` 指向 `secrets/oci_api_key.pem`。
- 檢查檔案齊全、`config.env` 已填寫，最後執行 `--dry-run`。

`--dry-run` 依序檢查設定與認證、home region、可用域、子網、映像檔、現有用量，然後退出，不送通知；網路不通時最多等 2 分鐘。

### Telegram 通知

```bash
./setup.sh --telegram
```

依提示操作：
1. 在 Telegram 搜尋 **@BotFather**（帳號名稱完全相符、有認證勾勾），傳 `/newbot` 建立 Bot。
2. 貼上 token。輸入不會顯示，也不會留在 shell 歷史。
3. 對新 Bot 按「開始」並傳一則訊息，回到終端機按 Enter。
4. 從列出的對話中選擇你自己的帳號。任何人都能私訊 Bot，因此腳本不會自動選擇。

腳本會驗證 token、取得 chat id、發送測試訊息，並寫入 `config.env` 的 `TG_BOT_TOKEN`、`TG_CHAT_ID`。重新啟動 claimer 後生效。通知時機：開始申請時、每 `HEARTBEAT_HOURS` 小時回報一次進度（預設 24，設 `0` 關閉）、申請成功（附公網 IP）、額度已滿而停止、實例已建立但後續步驟失敗、需要人工確認而停止（申請結果不明或狀態檔損毀）、開始申請後才發生的設定錯誤（例如 API 金鑰過期）。定期回報沒有如期出現，代表程式已停止（例如主機進入睡眠）。

發送失敗時 log 會顯示 Telegram 的錯誤說明，例如 `chat not found`（chat id 錯誤）、`bot was blocked by the user`（已封鎖 Bot）。token 外洩時到 @BotFather 傳 `/revoke` 重新產生。

### 設定

- 所有設定項說明見 `config.example.env`。
- 設定檔路徑可用 `-c` 或環境變數 `CLAIMER_CONFIG` 指定。
- 同名環境變數會覆寫設定檔；環境變數設為空字串代表使用預設值。
- `SSH_PUBLIC_KEY_FILE`、`STATE_DIR`、`OCI_CONFIG_FILE` 的相對路徑以設定檔所在目錄為基準。
- 換 region：設定 `REGION`（例如 `ap-tokyo-1`），或改 `secrets/oci_config` 的 `region`。

## 以 systemd 長期執行

```bash
sudo ./setup.sh --systemd                  # 安裝並啟動
sudo ./setup.sh --systemd --update-config  # 以專案內的 config.env 與 secrets/ 覆蓋已安裝的版本
sudo ./setup.sh --uninstall                # 移除服務與程式，保留設定與結果
sudo ./setup.sh --uninstall --purge        # 全部移除，含服務使用者

journalctl -u oci-a1-claimer -f            # 查看 log
sudo cat /var/lib/oci-a1-claimer/result.json
```

安裝後的配置：

| 位置 | 內容 | 擁有者 / 權限 |
|---|---|---|
| `/opt/oci-a1-claimer/` | 程式與 `.venv` | `root`，755 / 644 |
| `/etc/oci-a1-claimer/` | `config.env` 與 `secrets/` | `root:oci-claimer`，750 / 640 |
| `/var/lib/oci-a1-claimer/` | 狀態檔與結果 | systemd `StateDirectory`，700 |

- 服務以專用的非 root 使用者 `oci-claimer` 執行，並啟用 `NoNewPrivileges`、`ProtectSystem=strict`、`ProtectHome`、`PrivateTmp` 等強化選項；log 寫到 journald。
- 設定與金鑰由 root 擁有、服務群組唯讀，服務無法改寫自己的設定。
- `RestartPreventExitStatus=2 3 4`：只有未預期錯誤會自動重啟。
- 程式與設定複製到 `/opt`、`/etc` 後，以服務使用者執行一次 dry-run；失敗就不安裝、不啟動服務。
- 重複執行不會覆蓋 `/etc` 內已存在的設定；內容不同時會提示，要覆蓋請加 `--update-config`。
- `config.env` 須使用預設的 `secrets/` 路徑。Telegram token 等機密放在 `config.env`，不要寫進 unit 的 `Environment=`。
- WSL 需先在 `/etc/wsl.conf` 設定 `[boot]` 下的 `systemd=true` 並重啟 WSL。

## 結束碼與結果檔

| 碼 | 意義 | systemd 重啟 |
|---|---|---|
| 0 | 成功、dry-run 通過，或先前已完成 | 否 |
| 1 | 未預期錯誤 | 是 |
| 2 | 設定 / 權限錯誤（含設定檔、私鑰、SSH 公鑰讀取失敗，非 home region） | 否 |
| 3 | 免費額度已滿，或 `LimitExceeded` / `QuotaExceeded` | 否 |
| 4 | 實例已建立但後續步驟失敗，或狀態不明需人工確認 | 否 |
| 130 | 收到 SIGINT / SIGTERM | 否 |

- 結果寫在 `STATE_DIR` 的 `result.json`（instance OCID、AD、公網 / 私有 IP）。
- `state.json` 在建立成功當下寫入；存在時不會再申請，要再申請一台請先刪除它。
- 結束碼 4 時，log 與 Telegram 訊息會說明原因與處理方式。

## 最小權限 IAM

用專用的 IAM 使用者執行，不要用管理員帳號的 API 金鑰。使用者、群組、Policy 的建立步驟與每條語句的用途見 [docs/oracle-setup.md](docs/oracle-setup.md#6-建立-policy)。

## 已知限制

- 用量檢查只看 `COMPARTMENT_ID` 與 `USAGE_EXTRA_COMPARTMENT_IDS` 指定的 compartment；免費額度以整個 tenancy 計算。
- `STOPPED` 狀態的實例也計入用量（保守做法）。
- 同一個 `STATE_DIR` 同時只允許一個程序申請。本機執行與 systemd 服務使用不同的 `STATE_DIR`，彼此無法偵測，不要同時執行。
- 不要自行刪除 `pending_launch.json`，否則可能重複建立實例；只有程式要求人工確認、且已在主控台確認沒有多出實例時才刪除。
- 續做模式下若實例已被終止，會持續以結束碼 4 結束，確認後需手動刪除 `state.json`。
- 指派公網 IP 是否需要額外的 `public-ips` 權限尚未實測（要等實際建立成功才能確認）。

## 開發

運作流程、錯誤分類、冪等設計、測試與相依套件見 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

## 授權

MIT
