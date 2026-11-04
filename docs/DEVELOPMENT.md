# 開發說明

程式運作、狀態檔與錯誤處理的設計，以及測試與相依套件管理。使用方式見 [README](../README.zh-TW.md)。

## 運作流程

括號內為結束碼，意義見 [README 的「結束碼與結果檔」](../README.zh-TW.md#結束碼與結果檔)。

```
啟動
 ├─ state.json 存在？
 │    ├─ completed → 已完成，結束（0）
 │    ├─ launched  → 續做模式：只補做建立後步驟，不重新申請
 │    └─ 損毀      → 需人工確認，結束（4）
 ├─ 檢查 SSH 公鑰、home region、可用域、子網、映像檔
 ├─ pending_launch.json 存在？ → 先以原 AD、原 token、原申請內容重送，拿到明確結果才繼續
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

## 狀態檔

都放在 `STATE_DIR`，以原子方式寫入，權限 `0600`。

| 檔案 | 內容 |
|---|---|
| `state.json` | 建立成功當下立即寫入。存在時不會再申請；要再申請一台請先刪除 |
| `result.json` | 最終結果：instance OCID、AD、公網 / 私有 IP |
| `pending_launch.json` | 進行中申請的 AD、retry token 與申請參數（映像檔、OCPU、記憶體、開機磁碟）；每次送出申請前寫入，拿到明確結果後刪除 |
| `claimer.lock` | 空的鎖檔；同一個 `STATE_DIR` 同時只允許一個程序申請 |

## 錯誤分類

以 HTTP status 與 OCI error code 為主，訊息字串為輔（[API Errors](https://docs.oracle.com/en-us/iaas/Content/API/References/apierrors.htm)）：

| 條件 | 分類 |
|---|---|
| 429 | 限流 |
| `LimitExceeded` / `QuotaExceeded` | 額度不足 |
| 5xx 且訊息含 `capacity` | 容量不足 |
| 其他 5xx；409 `IncorrectState` / `ExternalServerIncorrectState` / `LockConflict` | 暫時性錯誤 |
| 其他 4xx | 設定錯誤 |
| `oci.exceptions.RequestException` / `ConnectTimeout`、`ConnectionError`、`TimeoutError` | 網路錯誤 |

容量不足不在官方錯誤表內，實際回應為 `500 / InternalError / "Out of host capacity."`。訊息格式若改變，會被當成暫時性錯誤退避重試，不會中斷。

## 冪等設計

- `launch_instance` 一律帶自行產生的 `opc_retry_token`，並在呼叫時傳 `NoneRetryStrategy()`，避免 SDK 自動重試 429 干擾長退避。
- 每次呼叫 `launch_instance` 前，先把 AD、token 與申請參數寫入 `pending_launch.json`。呼叫因任何原因中斷（網路錯誤、非預期例外、程序被終止），下次啟動都會以同一個 AD、token 與參數重送：若已建立，伺服器會回傳同一台實例。
- 結果不明期間遇到 429、5xx、可重試的 409 只退避，不換 token。409 `InvalidatedRetryToken` 代表先前的請求可能已建立實例，因此以結束碼 4 停止。
- retry token 有效 24 小時；結果不明超過 23 小時（啟動時或重送期間）就以結束碼 4 停止並要求人工確認。
- 同一個 `STATE_DIR` 同時只允許一個程序申請，第二個以結束碼 2 退出。不同 `STATE_DIR` 之間（例如本機執行與 systemd 服務）無法互相保護，不要同時執行。
- 所有 client 明確停用 SDK 斷路器，避免連續的容量不足 500 讓後續呼叫被擋下。

## 實作上的限制

- 「結果不明後以同一 token 重送、得到容量不足」視為原請求未建立，這是依 retry token 語意的推論，官方文件未逐字說明。
- 建立成功後若寫入 `state.json` 失敗（例如磁碟已滿），會以結束碼 4 退出並通知；`pending_launch.json` 會保留，下次啟動以同一個 token 重送即可找回該實例。

## 測試

```bash
python -m unittest discover -s tests -v
```

push 到 `main` 與每個 pull request 會由 GitHub Actions 在 Python 3.10、3.12、3.14 執行測試，並以 `shellcheck` 檢查 `setup.sh`。以 mock 取代 OCI client，不呼叫真實 API。涵蓋 AD 輪替、429 退避、額度停止、用量檢查、home region 檢查、成功路徑、建立後步驟失敗與續做、結果不明時的冪等保護、重複申請防護（申請前寫入 pending、token 過期、狀態目錄鎖）、網路中斷恢復、結束碼、設定解析、檔案權限、log 遮蔽與定期回報。

## 相依套件

- `requirements.in` 列出直接相依（`oci`）；`requirements.txt` 由它產生，固定所有間接相依的版本並附 hash，下載的檔案與 hash 不符時安裝會失敗。
- 升級時修改 `requirements.in` 後重新產生：`pip install pip-tools && pip-compile --generate-hashes --strip-extras --allow-unsafe -o requirements.txt requirements.in`。
- Dependabot 每週檢查 Python 相依套件與 GitHub Actions 的更新，並自動發 pull request。
