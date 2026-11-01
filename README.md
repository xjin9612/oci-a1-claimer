<p align="center">
  <img src="assets/icon.svg" width="128" height="128" alt="oci-a1-claimer">
</p>

<h1 align="center">oci-a1-claimer</h1>

持續透過 OCI 官方 API 申請 Always Free 的 `VM.Standard.A1.Flex`（ARM）實例，直到成功為止。

- 依序嘗試每個可用域（AD）；容量不足就換下一個，整輪結束後休息並加隨機 jitter。
- 429 長退避；額度已滿立即停止；網路錯誤退避重試，不會中斷。
- 成功後等待 RUNNING、取得公網 IP、寫入結果檔、送出通知（選用），然後結束。
- 只依賴官方 [`oci` Python SDK](https://pypi.org/project/oci/)，其餘為標準庫；Python 3.10 以上；程式只有 `claimer.py` 一個檔案，另附 `setup.sh` 一鍵安裝。

## 開始前須知

- **免費額度以官方為準**：見 [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) 與主控台 Governance → Limits, Quotas and Usage。規格（`OCPUS`、`MEMORY_GB`）與額度上限（`FREE_OCPU_LIMIT`、`FREE_MEMORY_GB_LIMIT`）都是 `config.env` 的必填項，程式內沒有預設值，政策變動時只需改設定。
- **Home region**：Always Free 運算實例只能建在 home region，且 home region 註冊後無法更改（[Managing Regions](https://docs.oracle.com/en-us/iaas/Content/Identity/Tasks/managingregions.htm)）。本工具啟動時會檢查目標 region 是否為 home region，不是就拒絕執行。
- **帳號類型**：
  - 免費帳號超過免費額度時，A1 實例會被停用並刪除。
  - PAYG（隨用隨付）帳號超出免費額度的部分**會計費**。請確認 `OCPUS` / `MEMORY_GB` 不超過你帳號的免費額度。
- **一人一帳號**：Oracle 規定每人只能有一個免費帳號，建立多個帳號會被停用或終止（[Free Tier FAQ](https://www.oracle.com/cloud/free/faq/)）。

## 快速開始

適用 Linux、macOS 與 WSL。Windows 請在 WSL 內執行，並把專案放在 WSL 的家目錄（例如 `~/oci-a1-claimer`）：放在 `/mnt/c`、`/mnt/d` 等 Windows 磁碟時，`chmod` 無效，金鑰權限無法收緊。

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
.venv/bin/python claimer.py   # 前景執行，Ctrl+C 可安全停止
sudo ./setup.sh --systemd     # 或安裝成 systemd 服務長期執行
```

`setup.sh` 本機模式會：
- 檢查 Python 版本，建立 `.venv` 並安裝相依套件。
- 把 `secrets/` 設為 700、裡面的檔案與 `config.env` 設為 600。
- 把 `secrets/oci_config` 的 `key_file` 指向 `secrets/oci_api_key.pem`。
- 檢查檔案齊全、`config.env` 已填寫，最後執行 `--dry-run`。

`--dry-run` 依序檢查設定與認證、home region、可用域、子網、映像檔、現有用量，然後退出，不送通知；網路不通時最多等 2 分鐘。

### 設定

- 所有設定項說明見 `config.example.env`。
- 設定檔路徑可用 `-c` 或環境變數 `CLAIMER_CONFIG` 指定。
- 同名環境變數會覆寫設定檔；環境變數設為空字串代表使用預設值。
- `SSH_PUBLIC_KEY_FILE`、`STATE_DIR`、`OCI_CONFIG_FILE` 的相對路徑以設定檔所在目錄為基準。
- 換 region：設定 `REGION`（例如 `ap-tokyo-1`），或改 `secrets/oci_config` 的 `region`。

## 運作流程

```
啟動
 ├─ state.json 存在？
 │    ├─ completed → 已完成，結束（0）
 │    ├─ launched  → 續做模式：只補做建立後步驟，不重新申請
 │    └─ 損毀      → 需人工確認，結束（4）
 ├─ 檢查 SSH 公鑰、home region、可用域、子網、映像檔
 ├─ pending_launch.json 存在？ → 先以原 AD + 原 token 重送，拿到明確結果才繼續
 ├─ 用量檢查：已用 + 本次申請 > 上限 → 結束（3）
 └─ 迴圈：對每個 AD 呼叫 launch_instance
        容量不足  → 下一個 AD
        429       → 長退避（連續時加倍，有上限）後換下一個 AD
        配額超限  → 結束（3）
        權限/參數 → 結束（2）
        網路錯誤  → 結果不明：寫 pending，只用同一 AD + token 重送，直到拿到明確結果
        其他 5xx  → 同一 token 重試數次，仍失敗換下一個 AD
      整輪結束 → sleep INTERVAL_SECONDS + 0~JITTER_SECONDS
 成功 → 寫 state.json → 等 RUNNING → 取公網 IP → 寫 result.json → 通知 → 結束（0）
```

### 結束碼

| 碼 | 意義 | systemd 重啟 |
|---|---|---|
| 0 | 成功、dry-run 通過，或先前已完成 | 否 |
| 1 | 未預期錯誤 | 是 |
| 2 | 設定 / 權限錯誤（含設定檔、私鑰、SSH 公鑰讀取失敗，非 home region） | 否 |
| 3 | 免費額度已滿，或 `LimitExceeded` / `QuotaExceeded` | 否 |
| 4 | 實例已建立但後續步驟失敗，或狀態不明需人工確認 | 否 |
| 130 | 收到 SIGINT / SIGTERM | 否 |

### 狀態檔

都放在 `STATE_DIR`，以原子方式寫入，權限 `0600`。

| 檔案 | 內容 |
|---|---|
| `state.json` | 建立成功當下立即寫入。存在時不會再申請；要再申請一台請先刪除 |
| `result.json` | 最終結果：instance OCID、AD、公網 / 私有 IP |
| `pending_launch.json` | 網路錯誤導致結果不明時的 AD 與 retry token；拿到明確結果後自動刪除 |

### 錯誤分類

以 HTTP status 與 OCI error code 為主，訊息字串為輔（[API Errors](https://docs.oracle.com/en-us/iaas/Content/API/References/apierrors.htm)）：

| 條件 | 分類 |
|---|---|
| 429 | 限流 |
| `LimitExceeded` / `QuotaExceeded` | 額度不足 |
| 5xx 且訊息含 `capacity` | 容量不足 |
| 其他 5xx；409 `IncorrectState` / `ExternalServerIncorrectState` / `LockConflict` | 暫時性錯誤 |
| 其他 400、401、403、404、409 | 設定錯誤 |
| `oci.exceptions.RequestException` / `ConnectTimeout`、`ConnectionError`、`TimeoutError` | 網路錯誤 |

容量不足不在官方錯誤表內，實際回應為 `500 / InternalError / "Out of host capacity."`。訊息格式若改變，會被當成暫時性錯誤退避重試，不會中斷。

### 冪等設計

- `launch_instance` 一律帶自行產生的 `opc_retry_token`，並在呼叫時傳 `NoneRetryStrategy()`，避免 SDK 自動重試 429 干擾長退避。
- 網路錯誤後無法確定伺服器是否已建立，因此只用同一個 AD 和 token 重送：若已建立，伺服器會回傳同一台實例。這段期間遇到 429、5xx、409 只退避，不換 token；程式被停止時，下次啟動會先完成這個重送。
- retry token 有效 24 小時；pending 超過 23 小時就停止並要求人工確認。
- 所有 client 明確停用 SDK 斷路器，避免連續的容量不足 500 讓後續呼叫被擋下。

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
- 安裝前會以服務使用者執行一次 dry-run，失敗就不安裝。
- 重複執行不會覆蓋 `/etc` 內已存在的設定；內容不同時會提示，要覆蓋請加 `--update-config`。
- `config.env` 須使用預設的 `secrets/` 路徑。Telegram token 等機密放在 `config.env`，不要寫進 unit 的 `Environment=`。
- WSL 需先在 `/etc/wsl.conf` 設定 `[boot]` 下的 `systemd=true` 並重啟 WSL。

## 最小權限 IAM

用專用的 IAM 使用者執行，不要用管理員帳號的 API 金鑰。

1. Identity & Security → Domains → Default → Users：建立使用者 `oci-a1-claimer`，只保留 API keys 能力。
2. Groups：建立群組 `oci-a1-claimer` 並加入該使用者。
3. 在該使用者的 API keys 新增金鑰，將產生的設定片段存成 OCI 設定檔。
4. 在 tenancy 建立以下 policy。`<instance-compartment>` 為實例所在 compartment，`<network-compartment>` 為 VCN / 子網所在 compartment，相同時填同一個名稱。

```text
Allow group oci-a1-claimer to inspect tenancies in tenancy
Allow group oci-a1-claimer to inspect compartments in tenancy
Allow group oci-a1-claimer to read app-catalog-listing in tenancy
Allow group oci-a1-claimer to manage instances in compartment <instance-compartment>
Allow group oci-a1-claimer to read instance-images in compartment <instance-compartment>
Allow group oci-a1-claimer to inspect vnic-attachments in compartment <instance-compartment>
Allow group oci-a1-claimer to use vnics in compartment <instance-compartment>
Allow group oci-a1-claimer to use subnets in compartment <network-compartment>
Allow group oci-a1-claimer to use network-security-groups in compartment <network-compartment>
```

| 語句 | 用途 |
|---|---|
| `inspect tenancies in tenancy` | `ListRegionSubscriptions`（home region 檢查） |
| `inspect compartments in tenancy` | `ListAvailabilityDomains` |
| `read app-catalog-listing in tenancy` | `LaunchInstance` 附帶需求 |
| `manage instances` | `LaunchInstance`；涵蓋 `ListInstances` / `GetInstance` |
| `read instance-images` | `LaunchInstance` 附帶需求；涵蓋 `ListImages` / `GetImage` |
| `inspect vnic-attachments` | `ListVnicAttachments` |
| `use vnics` | `LaunchInstance` 附帶需求；涵蓋 `GetVnic` |
| `use subnets` | `LaunchInstance` 附帶需求；涵蓋 `GetSubnet` |
| `use network-security-groups` | `LaunchInstance` 附帶需求 |

- 刻意不授權 `manage instance-family`、`use volume-family` 與任何 VCN 建立 / 刪除權限。`manage instances` 仍含終止實例的權限，這是 `LaunchInstance` 的最低 verb。
- 有設定 `USAGE_EXTRA_COMPARTMENT_IDS` 時，那些 compartment 另需 `read instances`。
- 遇到 404 `NotAuthorizedOrNotFound` 時，可先改用官方較寬的範本確認問題不在權限，再逐步收斂：

```text
Allow group oci-a1-claimer to manage instance-family in compartment <instance-compartment>
Allow group oci-a1-claimer to read app-catalog-listing in tenancy
Allow group oci-a1-claimer to use virtual-network-family in compartment <network-compartment>
```

依據：
- [Details for the Core Services](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/corepolicyreference.htm)
- [Details for IAM with Identity Domains](https://docs.oracle.com/en-us/iaas/Content/Identity/policyreference/iampolicyreference.htm)
- [Common Policies](https://docs.oracle.com/en-us/iaas/Content/Identity/Concepts/commonpolicies.htm)
- [Policy Syntax](https://docs.oracle.com/en-us/iaas/Content/Identity/Concepts/policysyntax.htm)

## 測試

```bash
python -m unittest discover -s tests -v
```

以 mock 取代 OCI client，不呼叫真實 API。涵蓋 AD 輪替、429 退避、額度停止、用量檢查、home region 檢查、成功路徑、建立後步驟失敗與續做、結果不明時的冪等保護、網路中斷恢復、結束碼、設定解析、檔案權限與 log 遮蔽。

## 已知限制

- 用量檢查只看 `COMPARTMENT_ID` 與 `USAGE_EXTRA_COMPARTMENT_IDS` 指定的 compartment；免費額度以整個 tenancy 計算。
- `STOPPED` 狀態的實例也計入用量（保守做法）。
- 結果不明期間若刪除 `pending_launch.json` 或修改規格，冪等保護失效。
- 「結果不明後以同一 token 重送、得到容量不足」視為原請求未建立，這是依 retry token 語意的推論，官方文件未逐字說明。
- 建立成功後若寫入 `state.json` 失敗（例如磁碟已滿），會以結束碼 4 退出並通知，但下次啟動不會知道該實例存在。
- 續做模式下若實例已被終止，會持續以結束碼 4 結束，確認後需手動刪除 `state.json`。
- 指派公網 IP 是否需要額外的 `public-ips` 權限、平台映像檔是否只需 `read instance-images` 即可列出，尚未實測。
- Windows 上不檢查檔案權限，存取權限由資料夾 ACL 決定。

## 授權

MIT
