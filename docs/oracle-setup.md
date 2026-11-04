# Oracle Cloud 帳號與主控台準備

從註冊 Oracle Cloud 到取得本工具所需資料（`oci_config`、`oci_api_key.pem`、`ssh.pub` 與子網 OCID）的完整流程。選單名稱以繁體中文主控台為準，括號內為英文介面名稱；Oracle 改版後位置可能略有不同。

完成後回到 [README 的「快速開始」](../README.zh-TW.md#快速開始)。

## 準備

- **Email**：一個 Email 只能建立一個帳號。
- **手機號碼**：需可接聽電話，Oracle 可能來電確認。
- **信用卡**：只接受 **Visa、Mastercard、American Express**，**JCB 不可用**。具備 Visa / Mastercard 標誌、不需 PIN 碼的簽帳金融卡也可以。卡片需開啟海外網路交易。註冊時只做暫時的驗證預扣，不升級成付費帳號不會收費。
- **規定**：每人只能有一個免費帳號，建立多個帳號會被停用或終止（[Free Tier FAQ](https://www.oracle.com/cloud/free/faq/)）。

## 1. 註冊

到 [signup.cloud.oracle.com](https://signup.cloud.oracle.com/)。

### 帳戶資訊

| 欄位 | 填法 |
|---|---|
| 帳單國家／地區 | 台灣 |
| 名字／姓氏 | 與信用卡一致的英文姓名 |
| 替代名稱 | 必填；填中文全名，或與英文姓名相同 |
| 密碼 | 8–40 字元，含大寫、小寫、數字、特殊字元；不可含姓名、Email、空格、`` ` ``、`~` |
| Customer type | **Individual** |
| 雲端帳戶名稱 | 自訂，登入時要用，記下來 |
| 原建立區域（Home Region） | **Japan East (Tokyo)**，見下方說明 |

**原建立區域註冊後永遠無法更改。** Always Free 的運算實例只能建在這個區域。台灣沒有 OCI 區域，從台灣使用建議選東京或大阪；南韓北部（春川）無法建立 Always Free A1 實例（[Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)）。這個選項只決定機房位置，與你的地址無關。

### 地址資訊

地址欄位請用**英文**填寫，中文城市名稱會被判定無效。可用 [中華郵政中文地址英譯](https://www.post.gov.tw/post/internet/Postal/index.jsp?ID=207) 轉換；信用卡帳單上有英文地址時以它為準。

| 欄位 | 範例 |
|---|---|
| 地址第 1 行 | `No. 1, Sec. 1, Zhongxiao E. Rd., Zhongzheng Dist.` |
| 市 | `Taipei City` |
| 省 | `Taiwan` |
| 郵遞區號 | `100` |
| 電話號碼 | +886 後接手機號碼並**去掉開頭的 0**，例如 `912345678` |

### 付款資訊

Card Type 依卡片正面的標誌選 Visa、Mastercard 或 Amex。完成後等待開通，會收到歡迎信並自動進入主控台。

## 2. 保護管理員帳號

登入後主控台會先顯示「公告」（Announcements）頁，是 Oracle 的通知中心，可直接略過。

**兩步驟驗證**：右上角頭像 → **我的設定檔** → **安全性** 頁籤

1. 「兩步驟驗證」區塊設定 **行動 App**：用 Oracle Mobile Authenticator、Google Authenticator 等 App 掃 QR code。
2. 往下到「繞過碼」區塊產生繞過碼，存進密碼管理器。手機遺失時靠它登入。

## 3. 確認額度

**☰ → 治理與管理 → 租用戶管理 → 限額、配額和用量**

按「編輯篩選條件」：

| 篩選 | 值 |
|---|---|
| 服務 | Compute |
| 範圍 | **ap-tokyo-1**（整個區域，不是 `xxxx:AP-TOKYO-1-AD-1`） |
| 區間 | 帳戶名稱（根） |

A1 的限額以區域計算（限額名稱含 `regional`），範圍選成單一可用域時不會出現。找這兩行：

- Cores for Standard.A1 based VM and BM Instances
- Memory for Standard.A1 based VM and BM Instances

這裡顯示的是**服務限額**，試用期間可能是 16 OCPU / 96 GB，**不等於免費額度**。免費額度以 [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) 為準；試用期結束前 A1 總量必須降到免費額度以內，否則實例會被停用並刪除。`config.env` 的 `OCPUS`、`MEMORY_GB`、`FREE_OCPU_LIMIT`、`FREE_MEMORY_GB_LIMIT` 依免費額度填寫。

## 4. 建立網路並取得子網 OCID

**☰ → 網路 → 總覽 → 啟動 VCN 精靈**

1. 選 **建立具有網際網路連線的 VCN** → 啟動 VCN 精靈。
2. 名稱 `a1-vcn`，區間選帳戶名稱（根），其餘維持預設 → 下一步 → 建立。

精靈會一次建立 VCN、公用與專用子網路、網際網路閘道、NAT 閘道、服務閘道、路由表與安全清單。不要用「虛擬雲端網路」列表頁的「建立 VCN」，那只會建立沒有子網與閘道的空網路。

**取得子網 OCID**：VCN 詳細頁 → **子網路** 頁籤 → 點 **公用子網路-a1-vcn** → OCID 旁按「複製」。這就是 `config.env` 的 `SUBNET_ID`（`ocid1.subnet.oc1.ap-tokyo-1...`）。

## 5. 建立專用 IAM 使用者

本工具使用專用使用者的 API Key，不使用管理員的。

**☰ → 識別與安全 → 網域 → Default → 使用者管理 頁籤**

1. **群組** 區塊 → **建立群組**：名稱 `oci-a1-claimer`，描述 `oci-a1-claimer API access`。
2. **使用者** 區塊 → **建立**：
   - 名字／姓氏：例如 `oci` / `a1-claimer`
   - 使用者名稱／電子郵件：Gmail 可用「+」別名，例如 `you+a1@gmail.com`，信會寄到原信箱
   - 群組只勾 `oci-a1-claimer`，**不要**勾 Administrators
3. 收到啟用信後完成啟用。
4. 使用者詳細頁 → **Groups** 頁籤，確認只在 `oci-a1-claimer` 一個群組。

## 6. 建立 Policy

**☰ → 識別與安全 → 原則**，左側區間選帳戶名稱（根）→ **建立原則**

- 名稱：`oci-a1-claimer-policy`
- 描述：`oci-a1-claimer least privilege`
- 打開 **顯示手動編輯器**，貼上：

```text
Allow group oci-a1-claimer to inspect tenancies in tenancy
Allow group oci-a1-claimer to inspect compartments in tenancy
Allow group oci-a1-claimer to read app-catalog-listing in tenancy
Allow group oci-a1-claimer to manage instances in tenancy
Allow group oci-a1-claimer to read instance-images in tenancy
Allow group oci-a1-claimer to inspect vnic-attachments in tenancy
Allow group oci-a1-claimer to use vnics in tenancy
Allow group oci-a1-claimer to use subnets in tenancy
Allow group oci-a1-claimer to use network-security-groups in tenancy
```

群組位於 Default 網域時可省略網域名稱（[Policy Subjects](https://docs.oracle.com/en-us/iaas/Content/Identity/policysyntax/subject.htm)）。以上範圍為整個租用戶；要進一步縮小，可另建區間放 VCN 與實例，把 `tenancy` 換成 `compartment <名稱>`。各語句用途見 [README 的「最小權限 IAM」](../README.zh-TW.md#最小權限-iam)。

## 7. 產生 API Key

**網域 → Default → 使用者管理 → 點專用使用者**（不是你的管理員帳號）→ **API 金鑰** 頁籤 → **新增 API 金鑰**

1. 選 **產生 API 金鑰組** → **下載私密金鑰**。私鑰只能在此時下載一次。
2. 按 **新增**，畫面顯示 **組態檔預覽**：

   ```ini
   [DEFAULT]
   user=ocid1.user.oc1..xxxx
   fingerprint=xx:xx:...
   tenancy=ocid1.tenancy.oc1..xxxx
   region=ap-tokyo-1
   key_file=<path to your private keyfile> # TODO
   ```

3. 整段複製保存。`key_file` 那行照原樣保留，`setup.sh` 會自動改成正確路徑。

下載的私鑰等同這個帳號的密碼，不要傳給任何人或截圖。

## 8. 交給本工具的資料

| 本工具需要 | 來源 |
|---|---|
| `secrets/oci_config` | 第 7 步的組態檔預覽 |
| `secrets/oci_api_key.pem` | 第 7 步下載的私鑰 |
| `secrets/ssh.pub` | 自己產生：`ssh-keygen -t ed25519 -f ~/.ssh/oci_a1` 的 `~/.ssh/oci_a1.pub` |
| `config.env` 的 `SUBNET_ID` | 第 4 步的公用子網 OCID |
| `config.env` 的規格與額度 | 第 3 步 |

## 名詞

| 名詞 | 說明 |
|---|---|
| OCID | 每個資源的唯一識別碼，例如 `ocid1.subnet.oc1...`。單獨取得無法存取資源，但分享截圖時仍建議遮蔽 |
| Home Region | 註冊時選定的原建立區域，無法更改；Always Free 運算實例只能建在這裡 |
| 可用域（AD） | 區域內的資料中心。名稱前綴（例如 `Abcd:`）每個帳號不同；東京只有一個可用域 |
| 區間（Compartment） | 資源的分組，最上層是帳戶名稱（根） |
| 服務限額 | 帳號最多可建立的數量，不等於免費額度 |
